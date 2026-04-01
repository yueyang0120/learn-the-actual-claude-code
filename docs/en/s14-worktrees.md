# s14: Worktree Isolation

s01 > s02 > s03 > s04 > s05 | s06 > s07 > s08 > s09 > s10 | s11 > s12 > s13 > **[ s14 ]**

> "The best sandbox is a real one -- an entire git worktree where the agent can break things without touching your branch."

## Problem

When an agent modifies code, it works directly on your checked-out branch. If it introduces a bug, your working tree is dirty. You cannot try two approaches in parallel because both write to the same files. And a manually created worktree loses your local settings, hooks, and dependency symlinks.

## Solution

Claude Code creates a fully-configured worktree with one command: slug validation, `git worktree add`, settings copy, hooks config, symlinks. On exit you pick keep or remove, with a safety gate that blocks removal of dirty worktrees.

```
  /your-repo/                      /your-repo/.claude/worktrees/my-feature/
  (original)                       (isolated worktree)
  +----------------+               +----------------+
  | .git/          |  git worktree | .git (-> main)  |
  | src/           |  add -B       | src/            |
  | .claude/       | ------------> | .claude/        |
  |   settings     |  + setup      |   settings      | <-- copied
  | node_modules/  |               | node_modules/   | <-- symlinked
  +----------------+               +----------------+
                                          |
                                   on exit: keep or remove
                                          |
                              +-----------+-----------+
                              |                       |
                         keep (branch            remove (force
                         + dir remain)           + branch delete)
```

## How It Works

### 1. Slug validation

Before anything touches the filesystem, the slug is validated to prevent path traversal. Segments like `..` are rejected; slashes are flattened to `+` for the filesystem path.

```python
# agents/s14_worktrees.py (simplified)

VALID_SLUG_SEGMENT = re.compile(r"^[a-zA-Z0-9._-]+$")

def validate_slug(slug: str):
    for segment in slug.split("/"):
        if segment in (".", ".."):
            raise ValueError("path traversal rejected")
        if not VALID_SLUG_SEGMENT.match(segment):
            raise ValueError("invalid characters")

def worktree_branch_name(slug: str) -> str:
    return f"worktree-{slug.replace('/', '+')}"
```

### 2. Create with resume

The manager creates a new worktree with `git worktree add -B`. The `-B` flag force-resets the branch if it already exists. If the worktree directory already has a valid HEAD, it is reused instead.

```python
def create(self, slug):
    validate_slug(slug)
    # Fast resume: reuse existing worktree
    if os.path.exists(os.path.join(worktree_dir, ".git")):
        return self._resume(worktree_dir)
    # Fresh creation
    git("worktree", "add", "-B", branch, worktree_dir, base_ref)
    self._post_creation_setup(repo_root, worktree_dir)
```

### 3. Post-creation setup

Three steps make the worktree usable: copy settings (so your permission rules carry over), set `core.hooksPath` (so git hooks work), and symlink `node_modules` (so you skip reinstalling deps).

```python
def _post_creation_setup(self, repo_root, worktree_path):
    # 1. Copy .claude/settings.local.json
    shutil.copy2(src_settings, dst_settings)
    # 2. Configure hooks path
    git("config", "core.hooksPath", hooks_dir, cwd=worktree_path)
    # 3. Symlink node_modules
    os.symlink(src, dst, target_is_directory=True)
```

### 4. Change detection

Before allowing removal, the manager counts uncommitted files and new commits since creation.

```python
def has_changes(self):
    files = len(git("status", "--porcelain").stdout.splitlines())
    commits = int(git("rev-list", "--count",
                      f"{base_sha}..HEAD").stdout)
    return (files, commits)
```

### 5. Exit: keep or remove

Keep leaves the worktree on disk. Remove cleans up, but refuses if the worktree has uncommitted work unless you pass `discard_changes=True`.

```python
def remove(self, discard_changes=False):
    if not discard_changes:
        files, commits = self.has_changes()
        if files or commits:
            raise RuntimeError(
                f"Worktree has {files} file(s), {commits} commit(s). "
                "Pass discard_changes=True to force."
            )
    git("worktree", "remove", "--force", worktree_path)
    git("branch", "-D", branch)
```

## What Changed

| Component | Before (s13) | After (s14) |
|-----------|-------------|-------------|
| Isolation | Agent works on your branch | Dedicated worktree with its own branch |
| Settings | Lost in new worktree | Copied from `.claude/settings.local.json` |
| Git hooks | Not configured | `core.hooksPath` set to main repo's hooks |
| Dependencies | Must reinstall | Symlinked from main repo |
| Exit options | N/A | Keep (preserve) or remove (clean delete) |
| Data safety | Easy to lose work | Safety gate blocks dirty worktree removal |
| Resume | Start over each time | Existing worktree detected and reused |
| Slug safety | N/A | Validated: no path traversal, length-capped |

## Try It

```bash
cd learn-the-actual-claude-code
# Run inside any git repo
python agents/s14_worktrees.py
```

Watch for:

- Slug validation: `my-feature` passes, `../escape` is rejected
- A new directory appears under `.claude/worktrees/` with a dedicated branch
- Settings copied, hooks configured, node_modules symlinked (if present)
- Creating a file triggers the change detector (1 uncommitted file)
- `remove()` without `discard_changes` is refused with a clear error
- `remove(discard_changes=True)` succeeds and cleans up

Try making a commit inside the worktree, then check how `has_changes()` reports both uncommitted files and new commits.
