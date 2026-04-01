# Source Analysis -- Worktree Isolation

## 1. Slug Validation (`src/utils/worktree.ts` lines 48-87)

```typescript
const VALID_WORKTREE_SLUG_SEGMENT = /^[a-zA-Z0-9._-]+$/
const MAX_WORKTREE_SLUG_LENGTH = 64
```

`validateWorktreeSlug` is the first line of defense. Since the slug is joined
into `.claude/worktrees/<slug>` via `path.join`, an attacker-controlled slug
like `../../../etc` would escape the worktrees directory.

The function enforces three rules:
- Total length at most 64 characters.
- Each `/`-separated segment is checked against the allowlist regex. This
  permits `user/feature` style nesting while rejecting `.` or `..` segments,
  empty segments (from leading/trailing slashes), and characters like `:` or `\`
  that could form drive-spec paths on Windows.
- The check is **synchronous** and runs before any side effects (git commands,
  hooks, chdir), so a bad slug never touches the filesystem.

After validation, `flattenSlug` replaces `/` with `+` for both the branch name
and the directory path. This avoids D/F conflicts in git refs
(`worktree-user` file vs `worktree-user/feature` directory) and prevents nested
worktree directories from being accidentally deleted when a parent is removed.

```typescript
function flattenSlug(slug: string): string {
  return slug.replaceAll('/', '+')
}
// worktree-<flat_slug> is the branch name
// .claude/worktrees/<flat_slug> is the directory
```

## 2. Worktree Creation (`getOrCreateWorktree`, lines 235-375)

### Fast resume path (lines 247-255)

Before doing any work, the function reads the `.git` pointer file inside the
worktree directory directly via `readWorktreeHeadSha`. This avoids spawning a
subprocess (`git rev-parse HEAD` costs ~15ms spawn overhead). If the HEAD SHA
is found, the worktree already exists and is returned immediately.

### New worktree path (lines 257-374)

1. **Determine base branch.** Tries `origin/<defaultBranch>` by resolving the
   packed/loose ref directly with `resolveRef`. If found, skips `git fetch`
   entirely (saves 6-8s in large repos). Otherwise runs `git fetch origin
   <defaultBranch>` with `GIT_TERMINAL_PROMPT=0` and `GIT_ASKPASS=''` to prevent
   credential hangs.

2. **Sparse checkout.** If `settings.worktree.sparsePaths` is configured, adds
   `--no-checkout` to the worktree add command, then runs `git sparse-checkout
   set --cone` followed by `git checkout HEAD`. If either fails, tears down the
   worktree immediately to prevent a broken fast-resume on the next run.

3. **`git worktree add -B`**. Uses `-B` (not `-b`) to force-reset any orphan
   branch left behind by a previously removed worktree. This saves a
   `git branch -D` subprocess on every create.

```
git worktree add [-B] worktree-<slug> .claude/worktrees/<slug> origin/<default>
```

### PR support (lines 264-276)

If `options.prNumber` is set, fetches `pull/<N>/head` from origin and uses
`FETCH_HEAD` as the base. This powers `--worktree #123` and PR URL arguments.

## 3. Post-Creation Setup (`performPostCreationSetup`, lines 510-624)

This runs only for newly created worktrees (not resumed ones).

### Settings propagation (lines 516-534)
Copies `settings.local.json` (which may contain secrets/API keys) from the main
repo's `.claude/` directory to the worktree's `.claude/` directory. Uses
`copyFile` (not symlink) so the worktree has its own mutable copy.

### Git hooks configuration (lines 536-578)
Looks for `.husky` or `.git/hooks` in the main repo. If found, sets
`core.hooksPath` to the main repo's hooks directory. An optimization skips the
`git config` subprocess (~14ms) if the config already has the right value by
reading it directly from the git config file via `parseGitConfigValue`.

### Directory symlinking (lines 580-585)
Reads `settings.worktree.symlinkDirectories` (e.g., `['node_modules']`). For
each, creates a symlink from `<worktree>/<dir>` to `<repo>/<dir>`. Handles
ENOENT (source doesn't exist yet) and EEXIST (already symlinked) silently.
Validates each directory name for path traversal.

### .worktreeinclude file copying (lines 587-588)
Copies gitignored files matched by `.worktreeinclude` patterns (gitignore
syntax). Uses `git ls-files --others --ignored --directory` for a fast single
pass that collapses fully-ignored directories, then expands only directories
where a pattern explicitly targets a sub-path.

### Commit attribution hook (lines 603-623)
If the `COMMIT_ATTRIBUTION` feature flag is on, installs a
`prepare-commit-msg` hook in the worktree's hooks directory. Done
asynchronously (`void import(...).then(...)`) to avoid blocking creation.

## 4. EnterWorktreeTool (`src/tools/EnterWorktreeTool/EnterWorktreeTool.ts`)

### Input schema
```typescript
z.strictObject({
  name: z.string().superRefine(validateWorktreeSlug).optional()
})
```
The Zod schema delegates validation to `validateWorktreeSlug` via `superRefine`,
so invalid slugs are rejected at the schema level before `call()` runs.

### call() flow (lines 77-119)

1. **Guard:** Checks `getCurrentWorktreeSession()` is null (can't nest).
2. **Resolve to canonical root:** If already inside a worktree, resolves to the
   main repo root via `findCanonicalGitRoot` so the new worktree lands in the
   right `.claude/worktrees/`.
3. **Generate slug:** Falls back to `getPlanSlug()` if no name provided.
4. **Create:** Calls `createWorktreeForSession(sessionId, slug)` which calls
   `validateWorktreeSlug`, then hooks or `getOrCreateWorktree` +
   `performPostCreationSetup`.
5. **Switch session:** `process.chdir`, `setCwd`, `setOriginalCwd`,
   `saveWorktreeState`.
6. **Invalidate caches:** `clearSystemPromptSections()`,
   `clearMemoryFileCaches()`, `getPlansDirectory.cache.clear()`.

### Session state mutation

The tool mutates multiple pieces of global state:
- `process.cwd()` -- actual OS CWD
- Shell CWD -- what BashTool uses
- `originalCwd` -- bootstrap state for path resolution
- `currentWorktreeSession` -- module-level in worktree.ts
- `projectConfig.activeWorktreeSession` -- persisted to disk for resume

## 5. ExitWorktreeTool (`src/tools/ExitWorktreeTool/ExitWorktreeTool.ts`)

### Scope guard (`validateInput`, lines 174-223)

The tool is scoped **only** to worktrees created by `EnterWorktree` in the
current session (checked via `getCurrentWorktreeSession()`). Manual
`git worktree add` worktrees and previous-session worktrees are untouched.

### Uncommitted change detection (`countWorktreeChanges`, lines 79-113)

```
git -C <path> status --porcelain        -> uncommitted files
git -C <path> rev-list --count <base>..HEAD  -> new commits
```

Returns `null` (fail-closed) when:
- Git commands exit non-zero (corrupt index, lock file).
- `originalHeadCommit` is undefined (hook-based worktree wrapping git).

When action is `remove` and `discard_changes` is not set:
- If changes are detected, returns a validation error listing what would be lost.
- If state cannot be determined (null), also refuses.

### call() flow (lines 227-321)

**keep path:**
1. `keepWorktree()` -- `process.chdir(originalCwd)`, nulls session, updates
   config. The worktree directory and branch stay on disk.
2. `restoreSessionToOriginalCwd()` -- resets `setCwd`, `setOriginalCwd`,
   conditionally `setProjectRoot`, clears all CWD-dependent caches.
3. Returns message with worktree path and optional tmux reattach command.

**remove path:**
1. If tmux session exists, kills it.
2. `cleanupWorktree()` -- `process.chdir(originalCwd)`, runs
   `git worktree remove --force`, nulls session, updates config, sleeps 100ms
   for git lock release, then `git branch -D <worktreeBranch>`.
3. `restoreSessionToOriginalCwd()` -- same cache clearing.
4. Returns message noting what was discarded.

### CWD cache clearing (`restoreSessionToOriginalCwd`, lines 122-146)

```typescript
setCwd(originalCwd)
setOriginalCwd(originalCwd)
if (projectRootIsWorktree) {
  setProjectRoot(originalCwd)
  updateHooksConfigSnapshot()
}
saveWorktreeState(null)
clearSystemPromptSections()
clearMemoryFileCaches()
getPlansDirectory.cache.clear?.()
```

This is the inverse of the mutations in `EnterWorktreeTool.call()`. The
`projectRootIsWorktree` check distinguishes `--worktree` startup (which sets
projectRoot to the worktree) from mid-session `EnterWorktree` (which does not).

## 6. WorktreeExitDialog (`src/components/WorktreeExitDialog.tsx`)

Interactive React/Ink component shown when a user exits the session while still
inside a worktree. Checks for uncommitted changes and new commits. If the
worktree is clean (zero files, zero commits), removes silently. Otherwise
presents a `Select` dialog with options:

- **Keep worktree** (and optionally keep/kill tmux session)
- **Remove worktree** (and kill tmux session)

The component calls the same `keepWorktree()` / `cleanupWorktree()` functions
as `ExitWorktreeTool`.

## 7. Stale Worktree Cleanup (`cleanupStaleAgentWorktrees`, lines 1058-1136)

Ephemeral worktrees from agents (`agent-a<7hex>`), workflows (`wf_<id>-<idx>`),
and bridges (`bridge-<id>`) can leak when the parent process is killed. A
periodic sweep:

1. Lists `.claude/worktrees/` directory.
2. Filters to entries matching `EPHEMERAL_WORKTREE_PATTERNS`.
3. Skips the current session's worktree.
4. Skips entries newer than the cutoff date (30 days).
5. Checks `git status --porcelain -uno` (no tracked changes) and
   `git rev-list HEAD --not --remotes` (no unpushed commits).
6. Removes via `git worktree remove --force` + `git branch -D`.
7. Runs `git worktree prune` at the end.

## 8. Hook-Based Fallback

Throughout the codebase, the pattern is:
```typescript
if (hasWorktreeCreateHook()) {
  // delegate to user-configured hook
} else {
  // fall back to git worktree
}
```

This allows non-git VCS (Perforce, Mercurial, etc.) to participate in worktree
isolation by implementing `WorktreeCreate` and `WorktreeRemove` hooks in
`settings.json`. The hook receives the slug and returns a worktree path. On
removal, the `WorktreeRemove` hook is called with the path.

## Architecture Summary

```
User says "worktree"
        |
        v
EnterWorktreeTool.call()
        |
        +-- validateWorktreeSlug(slug)         [security gate]
        +-- createWorktreeForSession()
        |       +-- hook path OR git path
        |       |       +-- getOrCreateWorktree()
        |       |       |       +-- fast resume (readWorktreeHeadSha)
        |       |       |       +-- git fetch + git worktree add -B
        |       |       +-- performPostCreationSetup()
        |       |               +-- copy settings.local.json
        |       |               +-- configure core.hooksPath
        |       |               +-- symlink directories
        |       |               +-- copy .worktreeinclude files
        |       +-- save session to project config
        +-- process.chdir + setCwd + setOriginalCwd
        +-- clear all CWD-dependent caches

User says "exit worktree"
        |
        v
ExitWorktreeTool
        |
        +-- validateInput()
        |       +-- scope guard (getCurrentWorktreeSession)
        |       +-- change detection (countWorktreeChanges)
        +-- call()
                +-- keep: keepWorktree() + restoreSessionToOriginalCwd()
                +-- remove: killTmux + cleanupWorktree() + restoreSessionToOriginalCwd()
```
