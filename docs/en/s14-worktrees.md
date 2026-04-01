# Session 14 -- Worktree Isolation

s01 > s02 > s03 > s04 > s05 | s06 > s07 > s08 > s09 > s10 | s11 > s12 > s13 > **s14**

> "The best sandbox is a real one -- an entire git worktree where the agent can break things without touching your branch."
>
> *Harness layer: Claude Code creates isolated git worktrees on the fly, copies settings, configures hooks, symlinks heavy directories, and tracks changes so you can keep or discard the result with a single command.*

---

## Problem

When an agent modifies code, it works directly on your checked-out branch. That means:

1. **No isolation** -- if the agent introduces a bug, your working tree is dirty. Reverting means manual `git checkout` or `git stash` gymnastics.
2. **No parallel exploration** -- you cannot ask the agent to try two different approaches simultaneously because both would write to the same files.
3. **Setup loss** -- even if you create a worktree manually, it will not have your local settings, hooks, or dependency symlinks. The agent starts from a bare checkout.

What you want is a single command that creates a fully-configured worktree, switches the session into it, and gives you a clean keep-or-remove decision at the end.

---

## Solution

Claude Code's worktree system handles the full lifecycle: slug validation, git-worktree creation, post-creation setup (settings, hooks, symlinks), change detection, and a two-path exit (keep or remove with safety gates).

```
  /your-repo/                         /your-repo/.claude/worktrees/my-feature/
  (original working tree)             (isolated worktree)
  +------------------+                +------------------+
  | .git/            |   git worktree | .git  (file ->   |
  | src/             |   add -B       |   main .git)     |
  | .claude/         | -------------> | src/             |
  |   settings.local |   + setup      | .claude/         |
  | node_modules/    |                |   settings.local | <-- copied
  +------------------+                | node_modules/    | <-- symlinked
                                      +------------------+
                                                |
                                        on exit: keep or remove
                                                |
                              +--------+--------+--------+
                              |                          |
                        keep (branch               remove (force
                        + dir remain)              + branch delete)
```

---

## How It Works

### 1. Slug Validation -- Preventing Path Traversal

Before anything touches the filesystem, the slug is validated. This is a security boundary -- a malicious slug like `../../etc` could escape the worktree directory:

```python
VALID_SLUG_SEGMENT = re.compile(r"^[a-zA-Z0-9._-]+$")
MAX_SLUG_LENGTH = 64

def validate_slug(slug: str) -> None:
    """Reject slugs that could escape .claude/worktrees/ via path traversal."""
    if len(slug) > MAX_SLUG_LENGTH:
        raise ValueError(
            f"Slug too long: max {MAX_SLUG_LENGTH} chars, got {len(slug)}"
        )
    for segment in slug.split("/"):
        if segment in (".", ".."):
            raise ValueError(
                f'Invalid slug "{slug}": must not contain "." or ".." segments'
            )
        if not VALID_SLUG_SEGMENT.match(segment):
            raise ValueError(
                f'Invalid slug "{slug}": each segment must match '
                f"[a-zA-Z0-9._-]+ and be non-empty"
            )
```

Slashes in slugs are allowed (e.g., `user/feature`) but are flattened to `+` for the filesystem path and git branch name to avoid directory nesting and git ref D/F conflicts:

```python
def flatten_slug(slug: str) -> str:
    """Replace / with + to avoid nested dirs and git ref D/F conflicts."""
    return slug.replace("/", "+")

def worktree_branch_name(slug: str) -> str:
    return f"worktree-{flatten_slug(slug)}"
```

Source: `worktree.ts`, `EnterWorktreeTool`

### 2. Worktree Creation with Resume Support

The `create` method handles both fresh creation and resuming an existing worktree:

```python
def create(self, slug: str) -> WorktreeSession:
    """Create (or resume) a worktree and switch into it."""
    if self.session is not None:
        raise RuntimeError("Already in a worktree session")

    validate_slug(slug)
    repo_root = find_git_root()
    if repo_root is None:
        raise RuntimeError("Not inside a git repository")

    original_cwd = os.getcwd()
    flat = flatten_slug(slug)
    worktree_dir = os.path.join(repo_root, WORKTREES_REL, flat)
    branch = worktree_branch_name(slug)

    # Fast resume: if the worktree directory already has a HEAD, reuse it
    head_file = os.path.join(worktree_dir, ".git")
    if os.path.exists(head_file):
        head_sha = git("rev-parse", "HEAD", cwd=worktree_dir)
        if head_sha.returncode == 0:
            print(f"[resume] Existing worktree at {worktree_dir}")
            self.session = WorktreeSession(
                original_cwd=original_cwd,
                worktree_path=worktree_dir,
                worktree_name=slug,
                worktree_branch=branch,
                original_head_commit=head_sha.stdout.strip(),
            )
            os.chdir(worktree_dir)
            return self.session

    # Create with -B (force-reset branch if it exists)
    add_result = git(
        "worktree", "add", "-B", branch, worktree_dir, origin_ref,
        cwd=repo_root,
    )
```

The `-B` flag is important: it force-resets the branch if it already exists, avoiding "branch already exists" errors on retry.

Source: `worktree.ts`

### 3. Post-Creation Setup -- The Three Steps

After `git worktree add` succeeds, three setup steps make the worktree actually usable:

```python
def _post_creation_setup(self, repo_root: str, worktree_path: str) -> None:
    # 1. Copy settings.local.json
    src_settings = os.path.join(repo_root, SETTINGS_LOCAL_REL)
    if os.path.isfile(src_settings):
        dst_settings = os.path.join(worktree_path, SETTINGS_LOCAL_REL)
        os.makedirs(os.path.dirname(dst_settings), exist_ok=True)
        shutil.copy2(src_settings, dst_settings)
        print(f"  [setup] Copied settings.local.json")

    # 2. Configure core.hooksPath
    for hooks_candidate in (
        os.path.join(repo_root, ".husky"),
        os.path.join(repo_root, ".git", "hooks"),
    ):
        if os.path.isdir(hooks_candidate):
            git("config", "core.hooksPath", hooks_candidate,
                cwd=worktree_path)
            print(f"  [setup] Hooks path -> {hooks_candidate}")
            break

    # 3. Symlink large directories (node_modules, .venv, etc.)
    for dirname in ("node_modules",):
        src = os.path.join(repo_root, dirname)
        dst = os.path.join(worktree_path, dirname)
        if os.path.isdir(src) and not os.path.exists(dst):
            try:
                os.symlink(src, dst, target_is_directory=True)
                print(f"  [setup] Symlinked {dirname}")
            except OSError as exc:
                print(f"  [setup] Symlink {dirname} failed: {exc}")
```

Why these three steps matter:

- **Settings copy**: Without it, the worktree would use default settings, losing your permission rules and model preferences.
- **Hooks path**: Git hooks live in the main `.git/hooks` directory. Worktrees share the git database but not the config, so `core.hooksPath` must be explicitly set.
- **node_modules symlink**: Reinstalling dependencies in every worktree would take minutes and waste disk space. A symlink makes them instantly available.

Source: `worktree.ts`

### 4. Change Detection

Before allowing removal, the manager checks for uncommitted files and new commits that would be lost:

```python
def has_changes(self) -> tuple[int, int]:
    """Return (uncommitted_files, new_commits) in the active worktree."""
    if self.session is None:
        return (0, 0)
    wt = self.session.worktree_path

    status = git("status", "--porcelain", cwd=wt)
    files = 0
    if status.returncode == 0:
        files = sum(1 for l in status.stdout.splitlines() if l.strip())

    commits = 0
    base = self.session.original_head_commit
    if base:
        rev = git("rev-list", "--count", f"{base}..HEAD", cwd=wt)
        if rev.returncode == 0:
            commits = int(rev.stdout.strip() or "0")

    return (files, commits)
```

`rev-list --count base..HEAD` counts commits made since the worktree was created. This catches the case where you committed work but forgot it was in a worktree.

Source: `worktree.ts`, `ExitWorktreeTool`

### 5. Exit -- Keep or Remove

The two exit paths serve different workflows:

**Keep** -- leave the worktree on disk for later:

```python
def keep(self) -> str:
    """Exit the worktree, leaving it on disk."""
    if self.session is None:
        return "No active worktree session."
    info = self.session
    os.chdir(info.original_cwd)
    msg = (
        f"Worktree kept at {info.worktree_path}"
        + (f" on branch {info.worktree_branch}" if info.worktree_branch else "")
    )
    self.session = None
    return msg
```

**Remove** -- clean up completely, with a safety gate:

```python
def remove(self, discard_changes: bool = False) -> str:
    """Exit and delete the worktree. Refuses if dirty unless forced."""
    if self.session is None:
        return "No active worktree session."
    info = self.session

    # Safety gate
    if not discard_changes:
        files, commits = self.has_changes()
        if files > 0 or commits > 0:
            parts = []
            if files:
                parts.append(f"{files} uncommitted file(s)")
            if commits:
                parts.append(f"{commits} new commit(s)")
            raise RuntimeError(
                f"Worktree has {' and '.join(parts)}. "
                f"Pass discard_changes=True to force removal."
            )

    os.chdir(info.original_cwd)

    # git worktree remove --force
    git("worktree", "remove", "--force", info.worktree_path,
        cwd=info.original_cwd)

    # Delete the temporary branch
    if info.worktree_branch:
        git("branch", "-D", info.worktree_branch, cwd=info.original_cwd)

    self.session = None
    return f"Worktree removed at {info.worktree_path}"
```

The safety gate is the key UX detail. Calling `remove()` without `discard_changes=True` on a dirty worktree raises a `RuntimeError` listing exactly what would be lost. The real Claude Code surfaces this to the user and asks for confirmation before proceeding.

Source: `ExitWorktreeTool`

---

## What Changed

| Component | Before | After |
|-----------|--------|-------|
| Isolation | Agent works on your branch directly | Dedicated worktree with its own branch |
| Branch naming | Manual | Auto-generated: `worktree-{slug}` |
| Slug safety | N/A | Validated: no path traversal, length-capped, charset-restricted |
| Settings | Lost in new worktree | Copied from main `.claude/settings.local.json` |
| Git hooks | Not configured | `core.hooksPath` set to main repo's hooks directory |
| Dependencies | Must reinstall | Symlinked from main repo (`node_modules`, etc.) |
| Exit options | Manual cleanup | Two paths: `keep` (preserve) or `remove` (clean delete) |
| Data safety | Easy to lose work | Change detection blocks removal of dirty worktrees |
| Resume | Start over each time | Existing worktree detected and reused automatically |
| CWD management | Must remember to cd back | Automatic: `original_cwd` restored on exit |

---

## Try It

```bash
# Run the worktree isolation demo (must be inside a git repository)
cd /path/to/any/git/repo
python /path/to/agents/s14_worktrees.py
```

What to watch for in the output:

1. **Slug validation** -- valid slugs pass, `../escape` and oversized strings are rejected
2. **Worktree creation** -- a new directory appears under `.claude/worktrees/` with a dedicated branch
3. **Post-creation setup** -- settings copied, hooks configured, node_modules symlinked (if present)
4. **Change detection** -- creating a file shows 1 uncommitted file
5. **Safety gate** -- attempting `remove()` without `discard_changes` is refused with a clear error
6. **Forced removal** -- `remove(discard_changes=True)` succeeds and cleans up
7. **Keep path** -- a second worktree is created and kept, with CWD restored to the original directory

Try modifying the demo to make a commit inside the worktree, then observe how `has_changes()` reports both uncommitted files and new commits.
