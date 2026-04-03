# 第 08 章: 子 Agent

子 agent 允许 Claude Code 将工作分解为隔离的并发执行单元。`AgentTool` (`src/tools/AgentTool/`, 约 1,500 行) 负责调用, `runAgent.ts` 编排从模型解析到上下文组装到清理的完整生命周期。本章承接第 07 章: 指定 `context: fork` 的 skill 以子 agent 方式执行, agent 系统复用了 skill 系统建立的许多模式 -- frontmatter 定义, tool 解析, 权限范围控制。

## 问题

单线程 agent 循环处理简单任务足矣, 但工作天然并行或需要不同能力时就力不从心。代码审查可能需要一个快速只读搜索 agent 和一个推理架构的规划 agent 并发运行。验证步骤不应共享主 agent 的写权限。长时间运行的后台任务不应阻塞交互式会话。

核心挑战在于隔离: 子 agent 需要足够的共享状态 (文件缓存, 权限, 上下文) 来发挥作用, 同时需要足够的独立性使其失败, 权限提示和副作用不会损坏父 agent。此外, Anthropic API 使用 prefix caching, fork 出的子 agent 只有在 system prompt, tool 列表和消息前缀逐字节相同时才能获得 cache hit。

## Claude Code 的解法

### Agent 调用

`Agent` tool 接受结构化输入:

```typescript
{
  description:       string    // 3-5 词任务标签
  prompt:            string    // 交给 agent 的任务
  subagent_type?:    string    // 使用哪个 agent 定义
  model?:            'sonnet' | 'opus' | 'haiku'
  run_in_background?: boolean
}
```

`call()` 方法验证权限 (检查特定 agent 类型的 deny 规则), 从 `options.agentDefinitions.activeAgents` 中选取定义, 组装 tool 池, 然后同步 (向父 agent yield 消息) 或异步 (通过 `runAsyncAgentLifecycle()` 在独立 promise 中) 派发。

### runAgent 编排

`runAgent()` 是 `AsyncGenerator<Message, void>`, 在内部查询循环产生消息时逐条 yield。编排遵循严格的步骤序列:

**模型解析。** 每个 agent 定义声明首选模型。解析函数遵循优先级层次:

```typescript
const resolvedAgentModel = getAgentModel(
  agentDefinition.model,                  // 来自定义, 如 "haiku"
  toolUseContext.options.mainLoopModel,    // 父 agent 的模型
  model,                                  // tool 输入中的覆盖
  permissionMode,
)
```

`"inherit"` 表示使用父 agent 的模型。Explore agent 用 `"haiku"` 追求速度; Plan 和 Verification agent 用 `"inherit"` 保证质量。

**上下文消息组装。** Fork 子 agent 获得父对话的过滤副本。过滤器移除含孤立 `tool_use` block (无对应 `tool_result`) 的 assistant 消息, 防止 API 报错:

```typescript
const contextMessages = forkContextMessages
  ? filterIncompleteToolCalls(forkContextMessages)
  : []
const initialMessages = [...contextMessages, ...promptMessages]
```

非 fork agent 从空上下文开始, 只有 prompt 消息。

**文件状态缓存。** Fork 子 agent 克隆父 agent 的文件状态缓存以保持 cache hit 一致性。非 fork agent 使用空缓存:

```typescript
const agentReadFileState = forkContextMessages !== undefined
  ? cloneFileStateCache(toolUseContext.readFileState)
  : createFileStateCacheWithSizeLimit(READ_FILE_STATE_CACHE_SIZE)
```

**System prompt 构造。** Fork 子 agent 通过 `override.systemPrompt` 接收父 agent 已渲染的 system prompt 原始字节, 确保逐字节 cache 匹配。其他 agent 从定义构建:

```typescript
const agentSystemPrompt = override?.systemPrompt
  ? override.systemPrompt
  : asSystemPrompt(
      await getAgentSystemPrompt(
        agentDefinition, toolUseContext,
        resolvedAgentModel, additionalWorkingDirectories,
        resolvedTools,
      ),
    )
```

**省略 CLAUDE.md。** Explore 和 Plan agent 设置 `omitClaudeMd: true`。这些只读 agent 不需要项目特定的 commit, PR 或 lint 规则。在全球 34M+ Explore 生成量下, 省略 CLAUDE.md 每周节省约 5-15 Gtok。

**权限模式覆盖。** 每个 agent 可运行在不同权限模式下。异步 agent 自动设置 `shouldAvoidPermissionPrompts: true` (无终端可交互)。Fork 子 agent 使用 `permissionMode: 'bubble'` 将提示冒泡到父终端。

**Tool 解析。** Fork 子 agent 使用与父 agent 字节相同的 tool 列表 (保证 cache 一致)。其他 agent 通过 `resolveAgentTools()` 处理通配符展开 (`tools: ['*']`), 移除 `ALL_AGENT_DISALLOWED_TOOLS`, 过滤 `disallowedTools`。

**查询循环与清理。** Agent 进入主循环使用的同一 `query()` 函数。每条消息记录到 sidechain transcript 并 yield 给调用者。`finally` block 彻底清理, 防止生成数百个 agent 时的内存泄漏:

```typescript
finally {
  await mcpCleanup()                         // agent 专属 MCP server
  clearSessionHooks(rootSetAppState, agentId) // frontmatter hook
  cleanupAgentTracking(agentId)              // prompt cache 追踪
  agentToolUseContext.readFileState.clear()   // 释放内存
  initialMessages.length = 0                 // 释放上下文消息
  killShellTasksForAgent(agentId, ...)       // 终止后台 bash 任务
}
```

### CacheSafeParams

Fork 子 agent 共享父 agent 的 API prefix cache, 要求五个组件逐字节相同:

```typescript
export type CacheSafeParams = {
  systemPrompt:        SystemPrompt
  userContext:         { [k: string]: string }
  systemContext:       { [k: string]: string }
  toolUseContext:      ToolUseContext
  forkContextMessages: Message[]
}
```

`saveCacheSafeParams()` / `getLastCacheSafeParams()` 单例模式使轮次后的 fork (prompt suggestion, 后台摘要) 可获取主循环的 cache-safe 参数, 无需显式传递。

### 上下文隔离

`createSubagentContext()` 函数 (`src/utils/forkedAgent.ts`) 为子 agent 构建隔离的 `ToolUseContext`。模式一致: 可变状态 (`readFileState`, `contentReplacementState`) 被克隆以实现隔离。Abort controller 创建新的子级并链接到父级 -- 父级 abort 向下传播, 但子级可以独立 abort。异步 agent 的 `setAppState` 是 no-op (不能修改父 UI), 但 `setAppStateForTasks` 仍指向根 store。只读字段 (`options`, `fileReadingLimits`) 通过引用继承。新的 `queryTracking` 对象以递增深度支持 agent 嵌套的分析。

### Sidechain Transcript 记录

每个子 agent 的消息都持久化到磁盘, 服务于两个目的:

1. **恢复。** 后台 agent 中断后, `resumeAgentBackground()` 通过 `getAgentTranscript()` 读回 transcript 并从断点继续。
2. **调试。** 每个 agent 在 `subagents/` 下获得子目录, 包含元数据 (agent type, worktree path, description)。

消息以 UUID 链接方式增量记录:

```typescript
// 查询循环前记录初始消息
void recordSidechainTranscript(initialMessages, agentId)

// 后续每条消息附带父 UUID 保持顺序
await recordSidechainTranscript(
  [message], agentId, lastRecordedUuid
)
```

`lastRecordedUuid` 链维护父子顺序, 使 transcript 可按序重组。Fork 子 agent 使用 `transcriptSubdir` 将相关 agent 聚类。

### 内置 Agent 类型

四种内置 agent 覆盖不同角色:

**Explore** -- 快速只读代码搜索。外部使用 `haiku` 模型。禁用 Agent, Edit, Write tool。设置 `omitClaudeMd: true`。系统 prompt 强调并行 tool call 以提升吞吐。

**Plan** -- 软件架构和实现规划。继承父模型保证质量。只读, tool 限制同 Explore。输出分步计划和 "Critical Files for Implementation" 部分。

**General-Purpose** -- 多步骤任务默认 worker。完全 tool 访问 (`tools: ['*']`)。无显式模型覆盖。简洁面向行动的 prompt。

**Verification** -- 实现后正确性检查。继承模型, 后台运行 (`background: true`), 对项目文件只读但可写 `/tmp`。使用对抗性测试协议, PASS/FAIL/PARTIAL 判定格式。关键系统提醒每个 user turn 重新注入。

### 自定义 Agent 定义

用户在 `.claude/agents/` 中用 markdown 或 JSON 定义 agent:

```markdown
---
name: my-researcher
description: "Searches codebase for patterns"
tools:
  - Glob
  - Grep
  - Read
model: haiku
maxTurns: 25
background: true
---

You are a research specialist. Your job is to...
```

Markdown body 成为 agent 的 system prompt。定义从多源合并, 优先级与权限规则一致: built-in < plugin < userSettings < projectSettings < flagSettings < policySettings。`getActiveAgentsFromList()` 按 agent type 去重, 保留最高优先级定义。

## 关键设计决策

**可变状态克隆, 不可变状态引用。** 文件状态缓存和内容替换状态 (可变) 被克隆。选项和文件读取限制 (只读) 通过引用继承。防止并发修改 bug, 避免复制大型不可变结构。

**异步 agent 的 setAppState 为 no-op。** 异步 agent 不能安全修改父 UI 状态。但 `setAppStateForTasks` 仍共享, 使任务注册和终止操作到达根 store。

**逐字节相同的前缀保证 cache 一致。** Fork 子 agent 接收父 agent 精确的 system prompt 字节, tool 列表和上下文消息。任何偏差使 API cache 失效, 成本翻倍。

**finally 中彻底清理。** 释放缓存, 清空消息, 终止 shell 任务, 注销 hook, 清理 transcript -- 防御性应对数百 agent 的 session。

## 实际体验

用户请求 "research how authentication works in this codebase" 时, 模型在 haiku 上生成一个 Explore agent, 执行并行搜索, 返回摘要。更复杂的工作可能链式调用 Plan (设计), General-Purpose (实现), Verification (检查) -- 各自在独立上下文中运行。后台 agent 并发不阻塞终端。`.claude/agents/` 中的自定义 agent 让团队将组织工作流编码为可复用定义。

## 总结

- 子 agent 将工作分解为具有独立模型, tool 集, 权限模式和上下文窗口的隔离单元。
- `createSubagentContext()` 克隆可变状态并引用不可变状态, 异步 agent 的 `setAppState` 为 no-op, abort controller 联动传播取消。
- Fork 子 agent 通过 `CacheSafeParams` 维持逐字节相同的 system prompt, tool 列表和上下文消息, 共享父 agent 的 API prefix cache。
- 四种内置 agent (Explore, Plan, General-Purpose, Verification) 覆盖从快速只读搜索到对抗性正确性检查的频谱。
- Sidechain transcript 记录支持中断后台 agent 的恢复和 agent 执行链的调试可见性。
