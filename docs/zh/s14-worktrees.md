# Session 14 -- Worktree 隔离

s01 > s02 > s03 > s04 > s05 | s06 > s07 > s08 > s09 > s10 | s11 > s12 > s13 > **s14**

> "The best sandbox is a real one -- an entire git worktree where the agent can break things without touching your branch."
> -- "最好的沙箱就是真正的沙箱 -- 一个完整的 git worktree，agent 可以在其中随意破坏而不影响你的分支。"
>
> *Harness 层: Claude Code 即时创建隔离的 git worktree，复制设置、配置 hooks、符号链接大型目录，并追踪变更，让你可以用一条命令决定保留或丢弃结果。*

---

## 问题

当 agent 修改代码时，它直接在你当前检出的分支上工作。这意味着：

1. **没有隔离** -- 如果 agent 引入了一个 bug，你的工作树就变脏了。还原意味着手动执行 `git checkout` 或 `git stash` 操作。
2. **无法并行探索** -- 你不能让 agent 同时尝试两种不同的方案，因为两者都会写入相同的文件。
3. **配置丢失** -- 即使你手动创建了一个 worktree，它也不会有你的本地设置、hooks 或依赖符号链接。Agent 从一个裸检出开始。

你需要的是一条命令：创建一个完全配置好的 worktree，将会话切换进去，并在结束时给你一个干净的保留或删除决策。

---

## 解决方案

Claude Code 的 worktree 系统处理完整的生命周期：slug 验证、git worktree 创建、创建后设置（设置、hooks、符号链接）、变更检测，以及两条退出路径（保留或带安全门的删除）。

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

## 工作原理

### 1. Slug 验证 -- 防止路径穿越

在任何操作触及文件系统之前，slug 会被验证。这是一个安全边界 -- 恶意的 slug（如 `../../etc`）可能逃逸出 worktree 目录：

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

slug 中允许斜杠（例如 `user/feature`），但在文件系统路径和 git 分支名中会被展平为 `+`，以避免目录嵌套和 git ref D/F 冲突：

```python
def flatten_slug(slug: str) -> str:
    """Replace / with + to avoid nested dirs and git ref D/F conflicts."""
    return slug.replace("/", "+")

def worktree_branch_name(slug: str) -> str:
    return f"worktree-{flatten_slug(slug)}"
```

来源: `worktree.ts`、`EnterWorktreeTool`

### 2. Worktree 创建与恢复支持

`create` 方法同时处理全新创建和恢复已有 worktree 的情况：

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

`-B` 标志很重要：如果分支已存在，它会强制重置该分支，避免重试时出现"分支已存在"的错误。

来源: `worktree.ts`

### 3. 创建后设置 -- 三个步骤

`git worktree add` 成功后，三个设置步骤使 worktree 真正可用：

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

这三个步骤的重要性：

- **设置复制**: 没有它，worktree 会使用默认设置，丢失你的权限规则和模型偏好。
- **Hooks 路径**: Git hooks 存放在主 `.git/hooks` 目录中。Worktree 共享 git 数据库但不共享配置，因此 `core.hooksPath` 必须显式设置。
- **node_modules 符号链接**: 在每个 worktree 中重新安装依赖需要数分钟并浪费磁盘空间。符号链接使它们立即可用。

来源: `worktree.ts`

### 4. 变更检测

在允许删除之前，管理器会检查未提交的文件和会丢失的新提交：

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

`rev-list --count base..HEAD` 统计 worktree 创建以来的提交数量。这可以捕获你在 worktree 中提交了工作但忘记的情况。

来源: `worktree.ts`、`ExitWorktreeTool`

### 5. 退出 -- 保留或删除

两条退出路径服务于不同的工作流：

**保留** -- 将 worktree 留在磁盘上以备后用：

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

**删除** -- 完全清理，带安全门：

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

安全门是关键的用户体验细节。在脏 worktree 上调用 `remove()` 而不传 `discard_changes=True` 会抛出 `RuntimeError`，明确列出将会丢失的内容。真实的 Claude Code 会将此展示给用户，并在继续之前请求确认。

来源: `ExitWorktreeTool`

---

## 变化对比

| 组件 | 之前 | 之后 |
|------|------|------|
| 隔离性 | Agent 直接在你的分支上工作 | 拥有自己分支的专用 worktree |
| 分支命名 | 手动 | 自动生成: `worktree-{slug}` |
| Slug 安全性 | 无 | 已验证: 无路径穿越，长度限制，字符集限制 |
| 设置 | 在新 worktree 中丢失 | 从主仓库的 `.claude/settings.local.json` 复制 |
| Git hooks | 未配置 | `core.hooksPath` 设置为主仓库的 hooks 目录 |
| 依赖 | 必须重新安装 | 从主仓库符号链接 (`node_modules` 等) |
| 退出选项 | 手动清理 | 两条路径: `keep`（保留）或 `remove`（干净删除） |
| 数据安全 | 容易丢失工作 | 变更检测阻止删除脏 worktree |
| 恢复 | 每次重新开始 | 自动检测并复用已有 worktree |
| CWD 管理 | 必须记得切回目录 | 自动: 退出时恢复 `original_cwd` |

---

## 试一试

```bash
# Run the worktree isolation demo (must be inside a git repository)
cd /path/to/any/git/repo
python /path/to/agents/s14_worktrees.py
```

输出中需要关注的要点：

1. **Slug 验证** -- 合法的 slug 通过，`../escape` 和超长字符串被拒绝
2. **Worktree 创建** -- 在 `.claude/worktrees/` 下出现一个新目录，带有专用分支
3. **创建后设置** -- 设置已复制，hooks 已配置，node_modules 已符号链接（如果存在）
4. **变更检测** -- 创建一个文件后显示 1 个未提交文件
5. **安全门** -- 不传 `discard_changes` 尝试 `remove()` 会被拒绝并给出清晰的错误信息
6. **强制删除** -- `remove(discard_changes=True)` 成功并完成清理
7. **保留路径** -- 创建第二个 worktree 并保留，CWD 恢复到原始目录

尝试修改 demo，在 worktree 内部进行一次提交，然后观察 `has_changes()` 如何同时报告未提交文件和新提交。
