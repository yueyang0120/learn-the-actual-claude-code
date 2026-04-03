# 第 14 章: 工作树隔离

Agent 修改代码时, 直接操作的是当前检出的分支。一次失败的重构会弄脏工作树。两个并行方案无法共存, 因为都修改同一批文件。手动创建 git worktree 又会丢失本地设置、hooks 配置和依赖 symlink, 需要繁琐的手动设置才能让隔离环境可用。上一章团队系统将执行分散到多个 agent; 本章的 worktree 隔离则为每个执行流提供独立的文件系统空间, 通过单次工具调用完成完整配置。

## 问题

Git worktree 提供文件系统级隔离。Worktree 是同一仓库的独立 checkout, 拥有自己的工作目录和分支, 与主 checkout 共享 object store。创建轻量, 非常适合并行实验。

但原始的 `git worktree add` 对 agent 环境不够用。新 worktree 缺少 `.claude/settings.local.json`, 权限规则不会带过来。通过 `core.hooksPath` 配置的 git hooks 不会传播。`node_modules` 目录 (或等效的依赖树) 必须从头安装。而如果 worktree 名来自用户输入, 路径穿越攻击成为可能 -- 类似 `../../etc` 的 slug 可能写入预期目录之外。

退出流有自己的挑战。如果 agent 在 worktree 中有未提交的更改, 删除会销毁工作。如果 agent 做了未推送或未合并的 commit, 删除分支会丢失它们。系统必须检测这些状况, 除非用户显式同意否则拒绝删除。

工具还必须是会话作用域的。`ExitWorktreeTool` 只应操作当前会话中由 `EnterWorktreeTool` 创建的 worktree。用户手动创建的, 或前一个会话创建的 worktree 不能被触碰。

## Claude Code 的解法

### Slug 校验与路径安全

在任何文件系统操作之前, slug 经过严格校验。以 `/` 分割的每个段必须匹配模式 `[a-zA-Z0-9._-]`, 且不得为 `.` 或 `..`。总长度上限 64 字符。

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

分支名中, 斜杠通过 `flattenSlug()` 展平为 `+`。Slug `feature/auth-fix` 产生分支名 `worktree-feature+auth-fix`。这避免在 `.git/refs/heads/` 中创建嵌套目录, 同时保持可读性。

```typescript
// src/worktree/naming.ts
function flattenSlug(slug: string): string {
  return slug.replace(/\//g, "+");
}

function worktreeBranchName(slug: string): string {
  return `worktree-${flattenSlug(slug)}`;
}
```

### 创建与快速恢复

`getOrCreateWorktree()` 函数首先检查请求的 slug 对应的 worktree 是否已存在。如果目录包含合法的 `.git` 文件 (worktree 标记), 直接复用现有 worktree 而不重新创建。这个快速恢复路径处理从前一会话重新进入 worktree 的常见情况。

```typescript
// src/worktree/create.ts
async function getOrCreateWorktree(
  repoRoot: string,
  slug: string
): Promise<WorktreeInfo> {
  validateSlug(slug);
  const worktreeDir = path.join(repoRoot, ".claude", "worktrees", slug);
  const branch = worktreeBranchName(slug);

  // 快速恢复: 复用已有 worktree
  if (await isValidWorktree(worktreeDir)) {
    return { path: worktreeDir, branch, resumed: true };
  }

  // 确定基础分支
  const baseBranch = await detectBaseBranch(repoRoot);

  // 用 -B 创建新 worktree (分支已存在则强制重置)
  await git(["worktree", "add", "-B", branch, worktreeDir, baseBranch]);

  // 创建后设置
  await performPostCreationSetup(repoRoot, worktreeDir);

  return { path: worktreeDir, branch, resumed: false };
}
```

`-B` 标志很重要。如果分支已存在 (可能来自之前删除的 worktree 留下的残余), `-B` 将其重置到当前基础。没有 `-B`, 命令会因分支名冲突而失败。

### 创建后设置

三项操作将裸 worktree 转变为可用的 agent 环境。每项操作解决原始 `git worktree add` 留下的一个空白。

```typescript
// src/worktree/setup.ts
async function performPostCreationSetup(
  repoRoot: string,
  worktreePath: string
): Promise<void> {
  // 1. 设置传播: 复制 .claude/settings.local.json
  //    使权限规则带入隔离环境
  const srcSettings = path.join(repoRoot, ".claude", "settings.local.json");
  const dstSettings = path.join(worktreePath, ".claude", "settings.local.json");
  if (await fileExists(srcSettings)) {
    await fs.mkdir(path.dirname(dstSettings), { recursive: true });
    await fs.copyFile(srcSettings, dstSettings);
  }

  // 2. Git hooks: 配置 core.hooksPath 使 commit hooks,
  //    pre-push hooks 等在 worktree 中工作
  const hooksDir = await git(["config", "core.hooksPath"], { cwd: repoRoot });
  if (hooksDir) {
    await git(["config", "core.hooksPath", hooksDir], { cwd: worktreePath });
  }

  // 3. 目录 symlink: 链接 node_modules (及类似目录)
  //    避免重新安装依赖
  await symlinkDependencies(repoRoot, worktreePath);
}
```

额外的设置步骤包括复制 `.worktreeinclude` 中列出的文件 (项目级配置, 指定需要传播的额外文件) 和安装一个 commit 归属 hook 标记在 worktree 中产生的 commit。

Sparse checkout 也受支持。如果原始仓库使用 sparse checkout, worktree 继承相同的 sparse 模式, 确保不会意外 checkout 完整仓库内容。

### EnterWorktreeTool: 会话入口

`EnterWorktreeTool` 编排完整的入口流程: 守卫检查 (是否已在 worktree 中), 解析到 git 根目录, slug 生成或校验, worktree 创建, 会话 CWD 切换, cache 失效。

```typescript
// src/tools/worktree/EnterWorktreeTool.ts
async function enterWorktree(slug?: string): Promise<ToolResult> {
  // 守卫: 是否已在 worktree 中?
  if (currentSession.worktree) {
    return error("Already in a worktree session");
  }

  // 解析 git 根目录
  const repoRoot = await findGitRoot(process.cwd());

  // 未提供 slug 则生成随机值
  const resolvedSlug = slug ?? generateRandomSlug();

  // 创建或恢复 worktree
  const info = await getOrCreateWorktree(repoRoot, resolvedSlug);

  // 切换会话 CWD
  currentSession.worktree = info;
  process.chdir(info.path);

  // 失效 cache (system prompt, memory 文件, plan)
  invalidateCwdDependentCaches();

  return success(`Entered worktree: ${info.path}`);
}
```

Cache 失效不可或缺。System prompt (第 4 章) 包含依赖 CWD 的节 -- 文件列表和项目 context。切换到 worktree 后, 这些 cache 必须清除, 否则反映的是原始目录而非 worktree 的内容。

### ExitWorktreeTool: 作用域退出与安全门

退出工具强制执行严格的作用域守卫: 只操作当前会话中由 `EnterWorktreeTool` 创建的 worktree。前一会话的, 或手动创建的 worktree 永远不会被触碰。

```typescript
// src/tools/worktree/ExitWorktreeTool.ts
async function exitWorktree(
  action: "keep" | "remove",
  discardChanges: boolean = false
): Promise<ToolResult> {
  // 作用域守卫
  if (!currentSession.worktree) {
    return noop("No active worktree session");
  }

  const info = currentSession.worktree;

  if (action === "keep") {
    // worktree 和分支保留在磁盘上
    process.chdir(info.originalCwd);
    currentSession.worktree = null;
    invalidateCwdDependentCaches();
    return success("Exited worktree (kept on disk)");
  }

  // action === "remove"
  // 安全门: 检查未提交的更改和未合并的 commit
  const uncommitted = await countUncommittedFiles(info.path);
  const unmerged = await countUnmergedCommits(info.path, info.baseSha);

  if ((uncommitted > 0 || unmerged > 0) && !discardChanges) {
    return error(
      `Worktree has ${uncommitted} uncommitted file(s) and ` +
      `${unmerged} unmerged commit(s). ` +
      `Pass discardChanges: true to force removal.`
    );
  }

  // 删除 worktree 和分支
  process.chdir(info.originalCwd);
  await git(["worktree", "remove", "--force", info.path]);
  await git(["branch", "-D", info.branch]);
  currentSession.worktree = null;
  invalidateCwdDependentCaches();
  return success("Worktree removed");
}
```

两步安全门同时检查未提交文件 (`git status --porcelain`) 和自 worktree 创建以来的新 commit (`git rev-list --count baseSha..HEAD`)。任一条件都会阻止删除, 除非 `discardChanges` 被显式设置。这防止了当 worktree 包含进行中的工作时, 用户 (或 agent) 请求删除导致的意外数据丢失。

### 交互式退出对话与过期清理

会话在仍处于 worktree 中时结束, 一个交互式 React/Ink 组件 (`WorktreeExitDialog`) 向用户呈现保留或删除的选择。确保没有 worktree 被悄然遗弃或悄然销毁。

周期性的过期 worktree 清理进程扫描 `.claude/worktrees/` 目录, 识别近期未访问的 worktree。旧 worktree 被移除以防止磁盘空间累积。清理遵守同样的安全门: 脏 worktree 不予处理。

### 非 git VCS 的 hook 回退

对于不由 git 管理的仓库 (Mercurial, Perforce 等), worktree 系统支持基于 hook 的回退。在设置中配置 `WorktreeCreate` 和 `WorktreeRemove` hook, 提供与 VCS 无关的隔离。如果定义了这些 hook, `EnterWorktreeTool` 委托给它们而非使用 git 命令, 使相同的隔离工作流跨不同版本控制系统可用。

## 关键设计决策

**`git worktree add -B` 而非 `-b`。** `-b` 标志在分支已存在时失败。这是常见场景: 之前的 worktree 被删除但分支没有清理, 或者用户显式保留了分支。`-B` 标志强制重置分支, 使创建操作幂等。

**依赖 symlink 而非复制或重装。** 复制 `node_modules` (通常几百兆) 浪费磁盘空间和时间。重装需要网络访问, 可能耗时数分钟。Symlink 以零成本即时访问已有的依赖树。代价是 worktree 中对依赖的修改会影响原始目录, 但这可以接受, 因为隔离工作期间依赖变更很少发生。

**会话作用域的退出守卫而非全局 worktree 管理。** 如果允许 `ExitWorktreeTool` 删除任意 worktree, 会很危险: 用户可能为其他目的手动创建了 worktree。将工具作用域限定为当前会话中创建的 worktree, 防止意外销毁无关工作。

**Slug 校验使用显式拒绝而非净化。** 净化路径 (剥离 `..`, 替换非法字符) 可能产生意外结果。用清晰的错误消息拒绝非法 slug, 强制用户或 agent 提供合法名称, 消除对最终路径的歧义。

## 实际体验

开发者要求 Claude Code "用两种方案修复内存泄漏 -- 一种用 WeakRef, 一种用手动清理"。Agent 进入 slug 为 `weakref-approach` 的 worktree, 实现 WeakRef 方案, 运行测试, 以 `action: "keep"` 退出。然后进入第二个 slug 为 `manual-cleanup` 的 worktree, 实现替代方案, 运行测试。两个 worktree 保留在磁盘上, 各有自己的分支, 开发者可以对比结果。

如果开发者后来决定 WeakRef 方案更好, agent 可以重新进入 `manual-cleanup` worktree (快速恢复路径) 并以 `action: "remove"` 退出来清理。如果 worktree 有未提交的更改, 删除会被阻止, 直到提供 `discardChanges: true`。

整个过程中原始工作目录保持不动。设置、hooks 和依赖在每个 worktree 中可用, 无需手动配置。

## 总结

- Slug 校验通过严格正则 (`[a-zA-Z0-9._-]`), 路径穿越拒绝和 64 字符长度上限防止文件系统攻击, 同时保持名称可读。
- `getOrCreateWorktree()` 使用 `git worktree add -B` 实现幂等创建, 快速恢复路径处理已有 worktree, 基础分支检测确保正确的起点。
- 创建后设置传播设置, 配置 git hooks, symlink 依赖, 复制 `.worktreeinclude` 文件, 使 worktree 立即可用。
- 退出流强制执行会话作用域守卫和双条件安全门 (未提交文件, 未合并 commit), 除非显式覆盖否则阻止删除。
- 基于 hook 的回退 (`WorktreeCreate`/`WorktreeRemove`) 将隔离模式扩展到非 git 版本控制系统。
