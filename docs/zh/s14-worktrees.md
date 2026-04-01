# s14: Worktree Isolation

s01 > s02 > s03 > s04 > s05 | s06 > s07 > s08 > s09 > s10 | s11 > s12 > s13 > **[ s14 ]**

> "The best sandbox is a real one -- an entire git worktree where the agent can break things without touching your branch."

## 问题

Agent 改代码的时候，直接改的是你当前检出的分支。它引入个 bug，你的工作树就脏了。想同时试两种方案？做不到，因为都写同一批文件。手动建的 worktree 又丢了本地设置、hooks 和依赖的 symlink。

## 解决方案

Claude Code 一条命令搞定完整 worktree：slug 校验、`git worktree add`、复制设置、配置 hooks、symlink 依赖。退出时你选保留或删除，脏 worktree 有安全门挡着不让误删。

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

## 工作原理

### 1. Slug 校验

动文件系统之前先校验 slug，防止路径穿越。`..` 这种段直接拒绝；斜杠在文件系统路径里展平成 `+`。

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

### 2. 创建 + 恢复

manager 用 `git worktree add -B` 创建新 worktree。`-B` 标志的作用是：如果分支已存在就强制重置，省得重试报错。如果 worktree 目录已经有合法的 HEAD，直接复用。

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

### 3. 创建后设置

三步让 worktree 真正能用：复制设置（权限规则跟过来）、设 `core.hooksPath`（git hooks 能跑）、symlink `node_modules`（不用重装依赖）。

```python
def _post_creation_setup(self, repo_root, worktree_path):
    # 1. Copy .claude/settings.local.json
    shutil.copy2(src_settings, dst_settings)
    # 2. Configure hooks path
    git("config", "core.hooksPath", hooks_dir, cwd=worktree_path)
    # 3. Symlink node_modules
    os.symlink(src, dst, target_is_directory=True)
```

### 4. 变更检测

删除之前，manager 数一下未提交的文件和新增的 commit。

```python
def has_changes(self):
    files = len(git("status", "--porcelain").stdout.splitlines())
    commits = int(git("rev-list", "--count",
                      f"{base_sha}..HEAD").stdout)
    return (files, commits)
```

### 5. 退出: 保留或删除

keep 把 worktree 留在磁盘上。remove 清理干净，但如果 worktree 有未提交的工作，不传 `discard_changes=True` 就会拒绝。

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

## 变更内容

| 组件 | s13 | s14 |
|------|-----|-----|
| 隔离性 | Agent 直接改你的分支 | 专用 worktree，有自己的分支 |
| 设置 | 新 worktree 里丢了 | 从 `.claude/settings.local.json` 复制 |
| Git hooks | 没配置 | `core.hooksPath` 指向主仓库的 hooks |
| 依赖 | 得重装 | 从主仓库 symlink 过来 |
| 退出选项 | 无 | keep（保留）或 remove（干净删除） |
| 数据安全 | 容易丢活 | 安全门挡住脏 worktree 的删除 |
| 恢复 | 每次从头来 | 检测到已有 worktree 就直接复用 |
| Slug 安全 | 无 | 校验: 禁止路径穿越，长度限制 |

## 试一试

```bash
cd learn-the-actual-claude-code
# Run inside any git repo
python agents/s14_worktrees.py
```

留意这些输出：

- Slug 校验: `my-feature` 通过，`../escape` 被拒
- `.claude/worktrees/` 下出现新目录，带专用分支
- 设置复制完毕，hooks 配好，node_modules 做了 symlink（如果有的话）
- 创建一个文件后变更检测器报 1 个未提交文件
- 不传 `discard_changes` 调 `remove()` 会被拒，报错信息很明确
- `remove(discard_changes=True)` 成功，清理干净

试着在 worktree 里提交一次，看 `has_changes()` 怎么同时报告未提交文件和新 commit。
