# 第 07 章: Skills

Skill 是可复用的 prompt 模板, 扩展 Claude Code 的能力而无需修改核心代码。实现为带 YAML frontmatter 的 markdown 文件, skill 采用两层加载架构: system prompt 中每个 skill 仅占约 100 token 的摘要, 完整内容 (通常 2,000+ token) 延迟到模型实际调用时才加载。本章与第 06 章的上下文压缩直接关联 -- 驱动压缩的 token 预算意识, 同样驱动了 skill 系统的两层设计。

## 问题

用户和组织希望为 Claude Code 注入领域特定的工作流 -- 部署流程, 代码审查清单, 调试协议。每个工作流内容丰富: 指令, 步骤, 约束, 示例, 轻松达到数千 token。

把所有 skill 的完整内容塞进 system prompt 是浪费。在每个 token 都有成本的上下文窗口中 (参见第 06 章), 20 个 skill 各 2,000 token 就是 40,000 token -- 有效窗口的约 20% -- 即使当前轮次一个都没用到。

另一个极端是完全隐藏 skill 直到用户显式调用, 但这样模型无法自主发现或建议相关 skill。需要一条中间路线: 足够的信息用于发现, 完整内容按需加载。

## Claude Code 的解法

### 多源发现

Skill 从多个目录并行发现, 然后去重:

```typescript
// src/skills/loadSkillsDir.ts -- getSkillDirCommands (简化)
const [managedSkills, userSkills, projectSkillsNested, ...] =
  await Promise.all([
    loadSkillsFromSkillsDir(managedSkillsDir, 'policySettings'),
    loadSkillsFromSkillsDir(userSkillsDir, 'userSettings'),
    Promise.all(projectSkillsDirs.map(
      dir => loadSkillsFromSkillsDir(dir, 'projectSettings')
    )),
    // ... 其他目录, 兼容旧格式, 内置 skill
  ])
```

| 来源 | 路径模式 | source 值 |
|------|---------|----------|
| Managed (策略) | `<managed-path>/.claude/skills/` | `policySettings` |
| 用户全局 | `~/.claude/skills/` | `userSettings` |
| 项目 | `.claude/skills/` (向上查找到 home) | `projectSettings` |
| 内置 | 编译进二进制 | `bundled` |
| MCP | MCP server prompt resources | `mcp` |

每个 skill 是一个目录, 内含 `SKILL.md` 文件。去重使用 `realpath` 解析符号链接, 同一文件通过多条路径可达时只加载一次。

### YAML Frontmatter

每个 `SKILL.md` 以 YAML frontmatter 开头, 声明元数据:

```markdown
---
name: Deploy Helper
description: Assists with deployment workflows
when_to_use: When the user asks about deploying or releasing
allowed-tools: Bash, Read, Write
model: sonnet
context: fork
paths: "src/deploy/**, scripts/deploy*"
arguments: [environment, version]
---

You are a deployment assistant. Help the user deploy to
the specified environment.

## Steps
1. Check the current branch and status
2. Run pre-deploy checks
3. Execute deployment for $ARGUMENTS
```

`FrontmatterData` 类型包含 20 多个字段: `name`, `description`, `when_to_use`, `allowed-tools`, `model`, `context` (inline 或 fork), `paths` (条件激活 glob), `shell`, `hooks`, `effort` 等。`parseSkillFrontmatterFields()` 处理字符串到数组的转换, 兼容旧字段名, 并解析模型标识。

### 第 1 层: System Prompt 摘要

构建 system prompt 时, 仅注入 skill 的 name, description 和 `when_to_use` 字段。预算为上下文窗口的 1%:

```typescript
// src/tools/SkillTool/prompt.ts
export const SKILL_BUDGET_CONTEXT_PERCENT = 0.01
export const MAX_LISTING_DESC_CHARS = 250
```

`formatCommandsWithinBudget()` 在预算紧张时实施优雅降级:

```typescript
export function formatCommandsWithinBudget(commands, contextWindowTokens?) {
  const budget = getCharBudget(contextWindowTokens)

  // 先尝试完整描述
  const fullEntries = commands.map(
    cmd => `- ${cmd.name}: ${getCommandDescription(cmd)}`
  )
  if (totalChars(fullEntries) <= budget) return fullEntries.join('\n')

  // 内置 skill 永远不被截断
  // 计算非内置 skill 的最大描述长度
  const maxDescLen = Math.floor(availableForDescs / restCommands.length)

  if (maxDescLen < 20) {
    // 极端情况: 非内置 skill 只显示名称, 内置 skill 保留描述
    return commands.map((cmd, i) =>
      isBundled(i) ? fullEntries[i] : `- ${cmd.name}`
    ).join('\n')
  }
  // 正常情况: 截断非内置 skill 的描述以适配预算
}
```

结果是 system prompt 中一个紧凑的列表, 每个 skill 约 100 token -- 足以让模型识别何时应调用, 而不消耗有意义的上下文空间。

### 第 2 层: 按需加载完整内容

模型决定使用某个 skill 时, 调用 `Skill` tool。此时才加载完整 markdown 内容:

```typescript
// src/skills/loadSkillsDir.ts -- getPromptForCommand 方法
async getPromptForCommand(args, toolUseContext) {
  let finalContent = markdownContent

  // 用实际参数替换 $ARGUMENTS
  finalContent = substituteArguments(
    finalContent, args, true, argumentNames
  )

  // 替换 ${CLAUDE_SKILL_DIR} 为 skill 所在目录
  if (baseDir) {
    finalContent = finalContent.replace(
      /\$\{CLAUDE_SKILL_DIR\}/g, skillDir
    )
  }

  // 执行内联 shell 命令 -- MCP skill 不允许
  if (loadedFrom !== 'mcp') {
    finalContent = await executeShellCommandsInPrompt(
      finalContent, toolUseContext, ...
    )
  }

  return [{ type: 'text', text: finalContent }]
}
```

`$ARGUMENTS` 替换使 skill 可接受参数。`${CLAUDE_SKILL_DIR}` 变量使 skill 可引用自身目录下的文件。内联 shell 命令 (反引号前缀 `!`) 在加载时执行, 但 MCP 来源的 skill 禁用此功能, 作为安全边界。

### 执行分支: inline vs. fork

`Skill` tool 的执行路径由 frontmatter 的 `context` 字段决定:

- **Inline 执行** (默认): skill 的 prompt 作为新的 user message 展开进当前对话。`contextModifier` 调整 tool 权限上下文, 可选地覆盖模型或 effort 级别。
- **Fork 执行** (`context: fork`): skill 在隔离的子 agent 中运行 (参见第 08 章), 拥有独立 token 预算。适用于长时间运行或工具密集型的工作流, 避免污染主对话。

### 条件 Skill

带 `paths` frontmatter 字段的 skill 不在启动时加载。它们存储在 `conditionalSkills` map 中, 仅当模型访问匹配 glob 模式的文件时才激活:

```typescript
for (const skill of deduplicatedSkills) {
  if (skill.paths && skill.paths.length > 0
      && !activatedConditionalSkillNames.has(skill.name)) {
    conditionalSkills.set(skill.name, skill)  // 存储, 不返回
  } else {
    unconditionalSkills.push(skill)           // 立即返回
  }
}
```

这防止领域特定 skill (如由 `paths: "k8s/**"` 触发的 Kubernetes 部署 skill) 在变得相关之前消耗第 1 层预算。

### MCP Prompt 作为 Skill

当 `MCP_SKILLS` feature flag 启用时, MCP server 的 prompt resource 被转换为 skill 命令。一个 write-once 注册表 (`mcpSkillBuilders.ts`) 打破 MCP client 和 skill loader 之间的依赖循环。MCP skill 与本地和内置 skill 按名称去重后合并。关键安全边界: MCP skill 不能执行内联 shell 命令, 因为其内容来自远程服务器。

## 关键设计决策

**第 1 层预算为 1%。** 足够小以在多数 session 中可以忽略不计, 又足够大以列出数十个 skill 及其描述。内置 skill 豁免截断, 确保核心功能始终有完整描述。

**内置 skill 永不截断。** 优雅降级策略在触及内置 skill 之前先牺牲用户定义的 skill 描述。极端情况下, 用户 skill 只显示名称, 内置 skill 保留完整描述。

**Fork vs. inline 由 skill 作者决定。** 作者在 frontmatter 中声明 `context: fork`, 系统不做猜测。常见情况 (inline, 零开销) 保持快速。

**MCP skill 禁止执行 shell。** MCP prompt 内容来自潜在不可信的远程服务器, `executeShellCommandsInPrompt` 函数通过 `loadedFrom !== 'mcp'` 门控。这是深度防御措施, 阻止 prompt injection 升级为代码执行。

## 实际体验

Claude Code 启动时从所有配置目录发现 skill, 解析 frontmatter, 将每个 skill 的单行摘要注入 system prompt。模型在每轮都能看到这些摘要, 在相关时决定是否调用。

用户输入 "deploy to staging" 时, 模型从第 1 层列表中识别 `deploy-helper` skill, 调用 `Skill` tool。完整 markdown 内容加载, `$ARGUMENTS` 被替换为 "staging", 指令展开进对话。如果 skill 指定了 `context: fork`, 子 agent 在隔离环境中处理工作流并返回摘要。

用户可通过在 `.claude/skills/` 下创建目录和 `SKILL.md` 文件来添加自定义 skill。企业管理员可通过策略路径分发 managed skill。两层架构确保即使 skill 库很大也不会因 system prompt 膨胀而降低模型性能。

## 总结

- Skill 是带 YAML frontmatter 的 markdown 文件, 从 managed, user, project, bundled 和 MCP 五个来源并行发现。
- 两层加载架构将第 1 层 (system prompt) 控制在每个 skill 约 100 token, 完整内容延迟到第 2 层 (按需调用) 加载。
- `formatCommandsWithinBudget()` 在 1% 上下文窗口预算内实施优雅降级: 截断描述, 然后仅显示名称, 内置 skill 始终豁免。
- Skill 默认 inline 执行, `context: fork` 时在隔离子 agent 中运行; MCP 来源的 skill 禁止执行内联 shell 命令。
- 带 `paths` glob 的条件 skill 保持休眠, 直到模型访问匹配文件时才激活, 避免浪费第 1 层预算。
