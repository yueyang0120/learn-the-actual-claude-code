# Session 14 -- Worktree Isolation

## What this covers

Claude Code uses **git worktrees** to give each session an isolated working
directory without cloning the entire repository. This session walks through the
full lifecycle: slug validation, worktree creation via `git worktree add`,
settings and hook propagation, the `EnterWorktree` / `ExitWorktree` tool pair,
the keep-vs-remove exit decision (with uncommitted-change detection), and the
CWD-dependent cache clearing that keeps the rest of the system coherent.

## Key source files

| File | Role |
|---|---|
| `src/utils/worktree.ts` | Core worktree logic: validation, create, keep, cleanup, change detection, tmux integration, stale cleanup |
| `src/tools/EnterWorktreeTool/EnterWorktreeTool.ts` | Tool that creates a worktree and switches the session into it |
| `src/tools/ExitWorktreeTool/ExitWorktreeTool.ts` | Tool that exits a worktree (keep or remove) with safety gates |
| `src/components/WorktreeExitDialog.tsx` | Interactive UI shown on session exit when a worktree is active |
| `src/utils/getWorktreePaths.ts` | Lists all worktrees for a repo (analytics-instrumented) |
| `src/utils/worktreeModeEnabled.ts` | Feature flag (now unconditionally returns true) |

## Core concepts

1. **Slug validation** -- Names are constrained to `[a-zA-Z0-9._-]` per
   `/`-separated segment, max 64 chars. Rejects `..`, empty segments, and
   absolute paths to prevent path-traversal into `.claude/worktrees/<slug>`.

2. **Worktree creation** -- `git worktree add -B worktree-<slug> <path>
   <base>`. Fetches `origin/<default-branch>` as base unless the ref already
   exists locally (skips fetch to avoid credential prompts and 6-8s commit-graph
   scans). Supports `--no-checkout` + sparse-checkout via settings.

3. **Post-creation setup** -- Copies `settings.local.json`, configures
   `core.hooksPath` to point at the main repo's `.husky` or `.git/hooks`,
   symlinks directories (e.g. `node_modules`) to avoid disk bloat, copies
   `.worktreeinclude` files.

4. **Keep vs Remove** -- On exit, the user chooses. "Remove" runs
   `git worktree remove --force` then `git branch -D`. "Keep" just restores CWD.
   If there are uncommitted files or new commits, the tool refuses removal unless
   `discard_changes: true` is explicitly set.

5. **CWD cache clearing** -- After exiting, system-prompt sections, CLAUDE.md
   caches, and plan directories are all invalidated so they recompute for the
   original directory.

6. **Hook-based fallback** -- If `WorktreeCreate` / `WorktreeRemove` hooks are
   configured in settings, the git path is skipped entirely, allowing non-git VCS
   systems to participate.

## Reimplementation

`reimplementation.py` is a standalone Python script (~200 lines) that
demonstrates the core mechanics: slug validation, `git worktree add`,
settings symlinking, and the keep/remove lifecycle with change detection.
Run it in any git repository to try it out.
