#!/usr/bin/env python3
"""
Session 14 -- Worktree Isolation (reimplementation)

Standalone Python reimplementation of Claude Code's worktree isolation
subsystem.  Demonstrates slug validation, git-worktree creation, settings
symlinking, and the keep/remove lifecycle with uncommitted-change detection.

Run inside any git repository:
    python3 reimplementation.py
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

VALID_SLUG_SEGMENT = re.compile(r"^[a-zA-Z0-9._-]+$")
MAX_SLUG_LENGTH = 64
WORKTREES_REL = os.path.join(".claude", "worktrees")
SETTINGS_LOCAL_REL = os.path.join(".claude", "settings.local.json")


# ---------------------------------------------------------------------------
# Slug validation
# ---------------------------------------------------------------------------

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


def flatten_slug(slug: str) -> str:
    """Replace / with + to avoid nested dirs and git ref D/F conflicts."""
    return slug.replace("/", "+")


def worktree_branch_name(slug: str) -> str:
    return f"worktree-{flatten_slug(slug)}"


# ---------------------------------------------------------------------------
# Git helpers
# ---------------------------------------------------------------------------

def git(*args: str, cwd: Optional[str] = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
    )


def find_git_root(start: str = ".") -> Optional[str]:
    result = git("rev-parse", "--show-toplevel", cwd=start)
    if result.returncode != 0:
        return None
    return result.stdout.strip()


def get_default_branch() -> str:
    result = git("symbolic-ref", "refs/remotes/origin/HEAD")
    if result.returncode == 0:
        ref = result.stdout.strip()  # e.g. refs/remotes/origin/main
        return ref.rsplit("/", 1)[-1]
    # Fallback: try common names
    for name in ("main", "master"):
        check = git("rev-parse", "--verify", f"origin/{name}")
        if check.returncode == 0:
            return name
    return "main"


# ---------------------------------------------------------------------------
# WorktreeSession
# ---------------------------------------------------------------------------

@dataclass
class WorktreeSession:
    original_cwd: str
    worktree_path: str
    worktree_name: str
    worktree_branch: Optional[str] = None
    original_head_commit: Optional[str] = None


# ---------------------------------------------------------------------------
# WorktreeManager
# ---------------------------------------------------------------------------

@dataclass
class WorktreeManager:
    """Manages worktree creation, settings propagation, and teardown."""

    session: Optional[WorktreeSession] = field(default=None, init=False)

    # -- creation ----------------------------------------------------------

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

        # Fast resume: if the worktree directory already has a HEAD, reuse it.
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

        # Determine base branch
        default_branch = get_default_branch()
        origin_ref = f"origin/{default_branch}"
        check = git("rev-parse", "--verify", origin_ref, cwd=repo_root)
        if check.returncode != 0:
            # Try fetching
            fetch = git(
                "fetch", "origin", default_branch,
                cwd=repo_root,
            )
            if fetch.returncode != 0:
                origin_ref = "HEAD"

        # Resolve base SHA
        sha_result = git("rev-parse", origin_ref, cwd=repo_root)
        if sha_result.returncode != 0:
            raise RuntimeError(f"Cannot resolve base ref {origin_ref}")
        base_sha = sha_result.stdout.strip()

        # Create the worktree with -B (force-reset orphan branches)
        os.makedirs(os.path.join(repo_root, WORKTREES_REL), exist_ok=True)
        add_result = git(
            "worktree", "add", "-B", branch, worktree_dir, origin_ref,
            cwd=repo_root,
        )
        if add_result.returncode != 0:
            raise RuntimeError(
                f"git worktree add failed: {add_result.stderr.strip()}"
            )
        print(f"[create] Worktree at {worktree_dir} on branch {branch}")

        # Post-creation setup
        self._post_creation_setup(repo_root, worktree_dir)

        self.session = WorktreeSession(
            original_cwd=original_cwd,
            worktree_path=worktree_dir,
            worktree_name=slug,
            worktree_branch=branch,
            original_head_commit=base_sha,
        )
        os.chdir(worktree_dir)
        return self.session

    def _post_creation_setup(self, repo_root: str, worktree_path: str) -> None:
        """Propagate settings and hooks to the new worktree."""
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
                git(
                    "config", "core.hooksPath", hooks_candidate,
                    cwd=worktree_path,
                )
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

    # -- change detection --------------------------------------------------

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

    # -- exit: keep --------------------------------------------------------

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
        print(f"[keep] {msg}")
        return msg

    # -- exit: remove ------------------------------------------------------

    def remove(self, discard_changes: bool = False) -> str:
        """Exit and delete the worktree.  Refuses if dirty unless forced."""
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
        rm = git(
            "worktree", "remove", "--force", info.worktree_path,
            cwd=info.original_cwd,
        )
        if rm.returncode != 0:
            print(f"[warn] worktree remove failed: {rm.stderr.strip()}")

        # Delete the temporary branch
        if info.worktree_branch:
            git("branch", "-D", info.worktree_branch, cwd=info.original_cwd)

        msg = f"Worktree removed at {info.worktree_path}"
        self.session = None
        print(f"[remove] {msg}")
        return msg


# ---------------------------------------------------------------------------
# Demo
# ---------------------------------------------------------------------------

def demo() -> None:
    """Interactive demo: create a worktree, make a file, then exit."""
    print("=" * 60)
    print("Worktree Isolation -- Python Reimplementation Demo")
    print("=" * 60)

    repo_root = find_git_root()
    if repo_root is None:
        print("ERROR: Run this script inside a git repository.")
        sys.exit(1)
    print(f"Repository root: {repo_root}\n")

    mgr = WorktreeManager()

    # --- slug validation examples ---
    print("-- Slug validation examples --")
    for test_slug in ["my-feature", "user/feat", "../escape", "a" * 65, "ok.1"]:
        try:
            validate_slug(test_slug)
            print(f"  '{test_slug}' -> VALID")
        except ValueError as exc:
            print(f"  '{test_slug}' -> REJECTED: {exc}")
    print()

    # --- create worktree ---
    slug = "demo-session-14"
    print(f"-- Creating worktree '{slug}' --")
    session = mgr.create(slug)
    print(f"  CWD is now: {os.getcwd()}")
    print(f"  Branch: {session.worktree_branch}")
    print(f"  Base commit: {session.original_head_commit[:12]}...")
    print()

    # --- simulate work ---
    demo_file = os.path.join(session.worktree_path, "demo_artifact.txt")
    Path(demo_file).write_text("Created by session 14 demo\n")
    print(f"-- Created {demo_file} --")
    files, commits = mgr.has_changes()
    print(f"  Uncommitted files: {files}, New commits: {commits}")
    print()

    # --- try remove without discard (should fail) ---
    print("-- Attempting remove without discard_changes --")
    try:
        mgr.remove(discard_changes=False)
    except RuntimeError as exc:
        print(f"  Correctly refused: {exc}")
    print()

    # --- remove with discard ---
    print("-- Removing with discard_changes=True --")
    result = mgr.remove(discard_changes=True)
    print(f"  Result: {result}")
    print(f"  CWD restored to: {os.getcwd()}")
    print()

    # --- create again and keep ---
    print(f"-- Creating worktree '{slug}' again, then keeping it --")
    session2 = mgr.create(slug)
    print(f"  CWD: {os.getcwd()}")
    result2 = mgr.keep()
    print(f"  Result: {result2}")
    print(f"  CWD restored to: {os.getcwd()}")
    print()

    # --- final cleanup (remove the kept worktree manually) ---
    print("-- Final cleanup --")
    git("worktree", "remove", "--force", session2.worktree_path, cwd=repo_root)
    git("branch", "-D", session2.worktree_branch, cwd=repo_root)
    print("  Cleaned up demo worktree.")
    print()
    print("Done.")


if __name__ == "__main__":
    demo()
