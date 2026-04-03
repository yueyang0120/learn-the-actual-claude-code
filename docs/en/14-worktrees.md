# Chapter 14: Worktree Isolation

When an agent modifies code, it operates directly on the checked-out branch. A failed refactoring leaves the working tree dirty. Two parallel approaches to the same problem cannot coexist because both modify the same files. And manually creating a git worktree loses local settings, hooks configuration, and dependency symlinks, requiring tedious manual setup before the isolated environment is usable. The worktree isolation system solves all three problems: it creates a fully-configured git worktree with validated naming, propagated settings, and a safety-gated exit flow, all through a single tool invocation.

## The Problem

Git worktrees provide filesystem-level isolation. A worktree is a separate checkout of the same repository, with its own working directory and branch, sharing the object store with the main checkout. This makes them lightweight to create and ideal for parallel experimentation.

But raw `git worktree add` is insufficient for an agent environment. The new worktree lacks `.claude/settings.local.json`, so permission rules do not carry over. Git hooks configured via `core.hooksPath` do not propagate. The `node_modules` directory (or equivalent dependency tree) must be reinstalled from scratch. And if the worktree name is derived from user input, path traversal attacks become possible -- a slug like `../../etc` could write outside the expected directory.

The exit flow presents its own challenges. If the agent has uncommitted changes in the worktree, removing it would destroy work. If the agent made commits that have not been pushed or merged, removing the branch deletes them. The system must detect these conditions and refuse removal unless the user explicitly opts in.

Finally, the tool must be session-scoped. The `ExitWorktreeTool` should only operate on worktrees created by `EnterWorktreeTool` during the current session. Worktrees created manually by the user, or by a previous session, must not be touched.

## How Claude Code Solves It

### Slug validation and path safety

Before any filesystem operation, the slug undergoes strict validation. Each segment (split by `/`) must match the pattern `[a-zA-Z0-9._-]` and must not be `.` or `..`. The total slug length is capped at 64 characters.

```typescript
// src/worktree/validation.ts
const VALID_WORKTREE_SLUG_SEGMENT = /^[a-zA-Z0-9._-]+$/;
const MAX_WORKTREE_SLUG_LENGTH = 64;

function validateSlug(slug: string): void {
  if (slug.length > MAX_WORKTREE_SLUG_LENGTH) {
    throw new Error(`Slug exceeds ${MAX_WORKTREE_SLUG_LENGTH} characters`);
  }
  for (const segment of slug.split("/")) {
    if (segment === "." || segment === "..") {
      throw new Error("Path traversal rejected");
    }
    if (!VALID_WORKTREE_SLUG_SEGMENT.test(segment)) {
      throw new Error(`Invalid characters in segment: ${segment}`);
    }
  }
}
```

For the branch name, slashes are flattened to `+` via `flattenSlug()`. A slug of `feature/auth-fix` produces the branch name `worktree-feature+auth-fix`. This avoids creating nested ref directories in `.git/refs/heads/` while preserving readability.

```typescript
// src/worktree/naming.ts
function flattenSlug(slug: string): string {
  return slug.replace(/\//g, "+");
}

function worktreeBranchName(slug: string): string {
  return `worktree-${flattenSlug(slug)}`;
}
```

### Creation with fast resume

The `getOrCreateWorktree()` function first checks whether a worktree with the requested slug already exists. If the directory contains a valid `.git` file (the worktree marker), the existing worktree is resumed without re-creation. This fast resume path handles the common case of re-entering a worktree from a previous session.

```typescript
// src/worktree/create.ts
async function getOrCreateWorktree(
  repoRoot: string,
  slug: string
): Promise<WorktreeInfo> {
  validateSlug(slug);
  const worktreeDir = path.join(repoRoot, ".claude", "worktrees", slug);
  const branch = worktreeBranchName(slug);

  // Fast resume: existing worktree
  if (await isValidWorktree(worktreeDir)) {
    return { path: worktreeDir, branch, resumed: true };
  }

  // Determine base branch
  const baseBranch = await detectBaseBranch(repoRoot);

  // Create new worktree with -B (force-reset branch if exists)
  await git(["worktree", "add", "-B", branch, worktreeDir, baseBranch]);

  // Post-creation setup
  await performPostCreationSetup(repoRoot, worktreeDir);

  return { path: worktreeDir, branch, resumed: false };
}
```

The `-B` flag on `git worktree add` is significant. If the branch already exists (perhaps from a previously removed worktree that left its branch behind), `-B` resets it to the current base. Without `-B`, the command would fail on a branch name collision.

### Post-creation setup

Three operations transform a bare worktree into a usable agent environment. Each addresses a specific gap that raw `git worktree add` leaves.

```typescript
// src/worktree/setup.ts
async function performPostCreationSetup(
  repoRoot: string,
  worktreePath: string
): Promise<void> {
  // 1. Settings propagation: copy .claude/settings.local.json
  //    so permission rules carry over to the isolated environment
  const srcSettings = path.join(repoRoot, ".claude", "settings.local.json");
  const dstSettings = path.join(worktreePath, ".claude", "settings.local.json");
  if (await fileExists(srcSettings)) {
    await fs.mkdir(path.dirname(dstSettings), { recursive: true });
    await fs.copyFile(srcSettings, dstSettings);
  }

  // 2. Git hooks: configure core.hooksPath so commit hooks,
  //    pre-push hooks, etc. work in the worktree
  const hooksDir = await git(["config", "core.hooksPath"], { cwd: repoRoot });
  if (hooksDir) {
    await git(["config", "core.hooksPath", hooksDir], { cwd: worktreePath });
  }

  // 3. Directory symlinking: link node_modules (and similar) to
  //    avoid reinstalling dependencies
  await symlinkDependencies(repoRoot, worktreePath);
}
```

Additional setup steps include copying files listed in `.worktreeinclude` (a project-specific configuration for additional files that should propagate) and installing a commit attribution hook that marks commits made in worktrees.

Sparse checkout is also supported. If the original repository uses sparse checkout, the worktree inherits the same sparse patterns, ensuring that the worktree does not unexpectedly check out the full repository contents.

### EnterWorktreeTool: session entry

The `EnterWorktreeTool` orchestrates the full entry flow: guard checks (not already in a worktree), resolution to the git root directory, slug generation or validation, worktree creation, session CWD switch, and cache invalidation.

```typescript
// src/tools/worktree/EnterWorktreeTool.ts
async function enterWorktree(slug?: string): Promise<ToolResult> {
  // Guard: already in a worktree?
  if (currentSession.worktree) {
    return error("Already in a worktree session");
  }

  // Resolve git root
  const repoRoot = await findGitRoot(process.cwd());

  // Generate slug if not provided
  const resolvedSlug = slug ?? generateRandomSlug();

  // Create or resume worktree
  const info = await getOrCreateWorktree(repoRoot, resolvedSlug);

  // Switch session CWD
  currentSession.worktree = info;
  process.chdir(info.path);

  // Invalidate caches (system prompt, memory files, plans)
  invalidateCwdDependentCaches();

  return success(`Entered worktree: ${info.path}`);
}
```

Cache invalidation is essential. The system prompt (Chapter 4) includes CWD-dependent sections like file listings and project context. After switching to a worktree, these caches must be cleared so they reflect the worktree's contents rather than the original directory's.

### ExitWorktreeTool: scoped exit with safety gates

The exit tool enforces a strict scope guard: it only operates on worktrees created by `EnterWorktreeTool` during the current session. Worktrees from previous sessions, or those created manually, are never touched.

```typescript
// src/tools/worktree/ExitWorktreeTool.ts
async function exitWorktree(
  action: "keep" | "remove",
  discardChanges: boolean = false
): Promise<ToolResult> {
  // Scope guard
  if (!currentSession.worktree) {
    return noop("No active worktree session");
  }

  const info = currentSession.worktree;

  if (action === "keep") {
    // Leave worktree and branch intact on disk
    process.chdir(info.originalCwd);
    currentSession.worktree = null;
    invalidateCwdDependentCaches();
    return success("Exited worktree (kept on disk)");
  }

  // action === "remove"
  // Safety gate: check for uncommitted changes and unmerged commits
  const uncommitted = await countUncommittedFiles(info.path);
  const unmerged = await countUnmergedCommits(info.path, info.baseSha);

  if ((uncommitted > 0 || unmerged > 0) && !discardChanges) {
    return error(
      `Worktree has ${uncommitted} uncommitted file(s) and ` +
      `${unmerged} unmerged commit(s). ` +
      `Pass discardChanges: true to force removal.`
    );
  }

  // Remove worktree and branch
  process.chdir(info.originalCwd);
  await git(["worktree", "remove", "--force", info.path]);
  await git(["branch", "-D", info.branch]);
  currentSession.worktree = null;
  invalidateCwdDependentCaches();
  return success("Worktree removed");
}
```

The two-step safety gate checks both uncommitted files (`git status --porcelain`) and new commits since worktree creation (`git rev-list --count baseSha..HEAD`). Either condition blocks removal unless `discardChanges` is explicitly set. This prevents accidental data loss when a user (or the agent) requests removal of a worktree that contains work in progress.

### Interactive exit dialog and stale cleanup

When a session ends while still inside a worktree, an interactive React/Ink component (`WorktreeExitDialog`) presents the keep-or-remove choice to the user. This ensures that no worktree is silently abandoned or silently destroyed.

A periodic stale worktree cleanup process sweeps the `.claude/worktrees/` directory, identifying worktrees that have not been accessed recently. Old worktrees are removed to prevent disk space accumulation. This cleanup respects the same safety gates: dirty worktrees are left alone.

### Hook-based fallback for non-git VCS

For repositories not managed by git (Mercurial, Perforce, etc.), the worktree system supports a hook-based fallback. The `WorktreeCreate` and `WorktreeRemove` hooks, configured in settings, provide VCS-agnostic isolation. If these hooks are defined, `EnterWorktreeTool` delegates to them instead of using git commands, enabling the same isolation workflow across different version control systems.

## Key Design Decisions

**`git worktree add -B` instead of plain `-b`.** The `-b` flag fails if the branch already exists. This is a common situation: a previous worktree was removed but its branch was not cleaned up, or the user explicitly kept the branch. The `-B` flag force-resets the branch, making creation idempotent.

**Symlink for dependencies instead of copying or reinstalling.** Copying `node_modules` (often hundreds of megabytes) wastes disk space and time. Reinstalling requires network access and can take minutes. Symlinking provides instant access to the existing dependency tree at zero cost. The tradeoff is that changes to dependencies in the worktree affect the original, but this is acceptable since dependency changes are rare during isolated work.

**Session-scoped exit guard instead of global worktree management.** Allowing `ExitWorktreeTool` to remove any worktree would be dangerous: a user might have manually created worktrees for other purposes. Scoping the tool to worktrees created in the current session prevents accidental destruction of unrelated work.

**Slug validation with explicit traversal rejection rather than sanitization.** Sanitizing a path (stripping `..`, replacing invalid characters) can produce surprising results. Rejecting invalid slugs with a clear error message forces the user or agent to provide a valid name, eliminating ambiguity about what the resulting path will be.

## In Practice

A developer asks Claude Code to "try two approaches to fixing the memory leak -- one using WeakRef and one using manual cleanup." The agent enters a worktree with slug `weakref-approach`, implements the WeakRef solution, runs tests, and exits with `action: "keep"`. It then enters a second worktree with slug `manual-cleanup`, implements the alternative, and runs tests. Both worktrees remain on disk with their own branches, and the developer can compare the results.

If the developer later decides the WeakRef approach is better, the agent can re-enter the `manual-cleanup` worktree (fast resume path) and exit with `action: "remove"` to clean up. If the worktree has uncommitted changes, the removal is blocked until `discardChanges: true` is provided.

The original working directory remains untouched throughout this process. Settings, hooks, and dependencies are available in each worktree without manual setup.

## Summary

- Slug validation with a strict regex (`[a-zA-Z0-9._-]`), path traversal rejection, and a 64-character length cap prevents filesystem attacks while keeping names readable.
- `getOrCreateWorktree()` uses `git worktree add -B` for idempotent creation and a fast resume path for existing worktrees, with base branch detection for correct starting points.
- Post-creation setup propagates settings, configures git hooks, symlinks dependencies, and copies `.worktreeinclude` files, making the worktree immediately usable.
- Exit flow enforces a session scope guard and a two-condition safety gate (uncommitted files, unmerged commits) that blocks removal unless explicitly overridden.
- A hook-based fallback (`WorktreeCreate`/`WorktreeRemove`) extends the isolation pattern to non-git version control systems.
