# 第 06 章: 上下文压缩

长对话不断积累 token, 终将逼近模型的上下文窗口上限。压缩子系统 (`autoCompact.ts`, 351 行) 通过四个递进层级回收空间 -- 从零成本的 tool result 裁剪到完整的 LLM 摘要。第 05 章讨论了权限系统如何授权和执行 tool call; 本章处理的是执行之后的问题: 这些 tool result 消耗的上下文空间, 最终必须被回收。

## 问题

200K token 的上下文窗口看起来很大, 但一次活跃的编程会话可以轻松消耗掉。每个 tool result 可能就是几千 token -- 文件读取, grep 输出, bash 返回值。30-40 轮交互之后, 对话就逼近窗口边界了。

简单截断旧消息会丢失模型维持连贯行为所需的信息。用 LLM 一次性摘要整段对话成本高昂, 而且本身也可能失败。更关键的是, Anthropic API 使用 prefix caching -- 对早期消息的任何修改都会使已缓存的前缀失效, 导致下次请求全量重读。

工程挑战是: 渐进式回收 token, 保留近期上下文和关键状态, 同时尊重缓存层, 并优雅地处理失败。

## Claude Code 的解法

### 阈值计算

系统从两个基础常量推导出分层阈值:

```typescript
// src/services/compact/autoCompact.ts
const MAX_OUTPUT_TOKENS_FOR_SUMMARY = 20_000
export const AUTOCOMPACT_BUFFER_TOKENS = 13_000
export const WARNING_THRESHOLD_BUFFER_TOKENS = 20_000
export const MANUAL_COMPACT_BUFFER_TOKENS = 3_000
```

有效上下文窗口 = 模型原始窗口 - 压缩摘要输出的预留空间:

```typescript
export function getEffectiveContextWindowSize(model: string): number {
  const reservedTokensForSummary = Math.min(
    getMaxOutputTokensForModel(model),
    MAX_OUTPUT_TOKENS_FOR_SUMMARY,       // 20,000
  )
  let contextWindow = getContextWindowForModel(model, getSdkBetas())
  return contextWindow - reservedTokensForSummary
}
```

自动压缩阈值在此基础上进一步减去 13,000 token 缓冲:

```typescript
export function getAutoCompactThreshold(model: string): number {
  const effectiveContextWindow = getEffectiveContextWindowSize(model)
  return effectiveContextWindow - AUTOCOMPACT_BUFFER_TOKENS  // -13,000
}
```

以 200K 窗口, 16K 最大输出的模型为例: 有效窗口 = 200,000 - 16,000 = 184,000; 自动压缩阈值 = 184,000 - 13,000 = 171,000。Warning 在 auto-compact 阈值以下 20K 处触发。Blocking limit 在有效窗口以下仅 3K 处, 此时只有手动 `/compact` 能解围。

### 第 1 层: Micro-Compact

Micro-compact 是最轻量的层级。每次 API 请求前执行, 不调用 LLM。根据 prefix cache 的冷热状态, 有两种工作模式。

**冷缓存模式。** 用户长时间空闲后返回, cache 已过期, 服务端无论如何要重读整个前缀。此时直接把旧 tool result 替换为 stub:

```typescript
// src/services/compact/microCompact.ts
const compactableIds = collectCompactableToolIds(messages)
const keepRecent = Math.max(1, config.keepRecent)
const keepSet = new Set(compactableIds.slice(-keepRecent))
const clearSet = new Set(compactableIds.filter(id => !keepSet.has(id)))
// 替换内容: '[Old tool result content cleared]'
```

只有 `COMPACTABLE_TOOLS` 中的工具才会被处理: Read, Bash, Grep, Glob, WebSearch, WebFetch, Edit, Write。至少保留一个最近的结果。

**热缓存模式。** cache 仍然有效时, 修改 messages 数组会使缓存失效。此路径改为排队一个 `cache_edits` 指令, 由 API 层执行删除:

```typescript
const cacheEdits = mod.createCacheEditsBlock(state, toolsToDelete)
pendingCacheEdits = cacheEdits
// messages 数组保持不变 -- 编辑在 API 层应用
return { messages, compactionInfo: { pendingCacheEdits: { ... } } }
```

这一区分对系统其余部分不可见, 但对 API 成本至关重要。

### 第 2 层: Session Memory Compact

如果 session memory 系统在对话过程中提取了结构化笔记, 这些笔记可以直接充当摘要, 无需任何 LLM 调用。此层在 LLM 摘要之前运行, 成功时直接短路后续流程:

```typescript
// autoCompactIfNeeded():
const sessionMemoryResult = await trySessionMemoryCompaction(
  messages,
  toolUseContext.agentId,
  recompactionInfo.autoCompactThreshold,
)
if (sessionMemoryResult) {
  return { wasCompacted: true, compactionResult: sessionMemoryResult }
}
// 仅当 session memory 不足以处理时, 才降级到 LLM 压缩
```

算法找到已摘要消息和新消息之间的边界, 保留至少 10K token、至少 5 条文本消息、最多 40K token 的近期窗口。一个调整函数确保 tool_use/tool_result 对不会被拆分到边界两侧, 避免产生孤立引用和 API 错误。

### 第 3 层: LLM 摘要

当 session memory compact 不可用或不够用时, 系统请求模型摘要对话。输出上限 20,000 token (基于生产环境 p99.99 = 17,387 token 的实测数据)。这是成本最高、最容易失败的层级。

### 第 4 层: 手动 /compact

用户随时可以执行 `/compact`。触发与第 3 层相同的 LLM 摘要, 但即使自动压缩被禁用或 circuit breaker 跳闸, 此命令仍然可用。

### Token 警告级联

`calculateTokenWarningState()` 函数生成一个五字段状态对象, 驱动渐进式 UI 信号:

```typescript
export function calculateTokenWarningState(
  tokenUsage: number,
  model: string,
): {
  percentLeft: number
  isAboveWarningThreshold: boolean
  isAboveErrorThreshold: boolean
  isAboveAutoCompactThreshold: boolean
  isAtBlockingLimit: boolean
}
```

| 阈值 | 距有效窗口的距离 | 效果 |
|------|-----------------|------|
| Warning | 约 33K 以下 | UI 中黄色指示器 |
| Error | 约 33K 以下 (对称) | UI 中红色指示器 |
| Auto-compact | 13K 以下 | 触发自动压缩 |
| Blocking | 3K 以下 | 阻止进一步输入, 直到手动 `/compact` |

`percentLeft` 的计算以 auto-compact 阈值为分母, 因此百分比反映的是距离自动压缩的接近程度, 而非距绝对窗口上限的距离。

### Circuit Breaker

生产环境中曾发现: 上下文不可恢复地超标的 session 会每轮反复尝试注定失败的压缩。分析显示 1,279 个 session 连续失败 50 次以上, 每天浪费约 250,000 次 API 调用。

Circuit breaker 在连续 3 次失败后停止重试:

```typescript
const MAX_CONSECUTIVE_AUTOCOMPACT_FAILURES = 3

if (
  tracking?.consecutiveFailures !== undefined &&
  tracking.consecutiveFailures >= MAX_CONSECUTIVE_AUTOCOMPACT_FAILURES
) {
  return { wasCompacted: false }
}
```

任何一次成功压缩都会将计数器重置为零。阈值 3 是实用折中: 足够低以快速切断死循环, 足够高以容忍一两次临时错误。

### 追踪状态

`AutoCompactTrackingState` 类型在主查询循环中传递压缩历史:

```typescript
export type AutoCompactTrackingState = {
  compacted: boolean           // 本 session 是否压缩过?
  turnCounter: number          // 上次压缩后经过的轮数
  turnId: string               // 当前轮次的唯一 ID
  consecutiveFailures?: number // circuit breaker 计数器
}
```

`compacted` 标志与 `turnCounter` 的组合为遥测提供数据, 用于诊断再压缩循环 -- 压缩输出本身超过阈值, 在下一轮立即触发再次压缩。这是一个真实存在的问题。

### 压缩后恢复

压缩将对话替换为摘要后, 模型会丧失关键上下文。恢复阶段重新注入:

1. **近期文件** -- 最多 5 个, 50K token 预算, 每个最多 5K
2. **活跃 plan** -- 如果存在 plan 文件
3. **Plan mode 指令** -- 如果用户处于 plan mode
4. **已调用的 skill** -- 25K 预算, 每个最多 5K, 最近优先
5. **延迟加载的 tool schema** -- 压缩前发现的工具定义
6. **Agent 列表和 MCP 指令** -- 从当前状态重新声明
7. **Session start hook** -- 重新执行, 恢复 CLAUDE.md 等上下文

因此压缩后的实际 token 数往往远超摘要本身。恢复的上下文可能增加 20-50K token。

## 关键设计决策

**递进层级而非单一策略。** Micro-compact 零成本, 每轮运行。Session memory compact 避免 LLM 调用。LLM 摘要是最后手段。分层设计在最小化成本的同时最大化响应速度。

**两种 micro-compact 模式区分缓存状态。** 冷缓存路径直接修改 messages (安全, 因为没有缓存可失效)。热缓存路径使用 `cache_edits` 保护前缀。这一区分对系统其余部分不可见, 但对 API 成本影响巨大。

**Circuit breaker 设为 3 次。** 足够低以快速切断失控循环, 足够高以容忍偶发 API 错误。成功后重置, 所以间歇性失败不会跨健康压缩累积。

**压缩后显式恢复。** 不依赖摘要质量来保留所有信息, 而是显式重新注入已知的关键上下文。token 开销更高, 但可靠性远超依赖摘要质量。

## 实际体验

典型编程会话中, micro-compact 在对话增长时静默裁剪旧 tool result, 用户不会感知。上下文接近 warning 阈值时, UI 出现黄色指示器。跨过 auto-compact 阈值后, 压缩在轮次间自动运行 -- 先尝试 session memory, 再降级到 LLM 摘要。用户看到短暂的 "Compacting conversation..." 提示。

压缩后, 模型收到摘要加恢复的文件、plan 和 skill。对话无缝继续, 但对很早轮次的引用可能丢失。如果用户察觉上下文退化, 可以手动运行 `/compact` 强制刷新摘要。

极端情况下 -- 例如单个 tool result 超过有效窗口 -- circuit breaker 在 3 次失败后跳闸。UI 显示错误级警告, 用户需要手动介入, 通常是开启新对话。

## 总结

- 四个递进压缩层级 (micro-compact, session memory, LLM 摘要, 手动 /compact) 在最小化成本的同时将 session 保持在 token 窗口内。
- Micro-compact 根据 cache 温度分两种模式: 冷缓存直接修改, 热缓存使用 API 层 `cache_edits`。
- 阈值计算从模型的上下文窗口和输出 token 预留推导出 warning, auto-compact 和 blocking 三个界限。
- Circuit breaker (连续 3 次失败) 阻止失控压缩循环, 该问题曾在生产环境中每天浪费 250K 次 API 调用。
- 压缩后恢复阶段重新注入近期文件, 活跃 plan, skill 和 hook, 使模型保留关键运行上下文。
