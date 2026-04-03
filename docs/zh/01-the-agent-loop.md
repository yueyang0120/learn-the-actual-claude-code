# 第 1 章: Agent 循环

Claude Code 的核心是一个循环。用户输入消息, 模型回复 — 可能调用工具 — 循环决定继续还是停止。后续章节描述的所有功能 (工具, 权限, 上下文管理) 都挂载在这个循环上。理解循环是理解一切的前提。

## 问题

一个 AI 编程助手不能只回答问题。它必须执行操作 — 读文件, 跑命令, 编辑代码 — 并将这些操作串联成多轮交互, 直到任务完成。这要求一个运行时来管理可变的对话状态, 分发 tool call, 处理错误, 并判断何时继续、何时停止。

朴素的实现很直接: 发消息给 API, 拿到回复, 检查是否有 tool call, 执行, 追加结果, 重复。但生产环境的现实远比这复杂:

- **启动延迟敏感。** 开发者能感知到 300ms 的延迟。一个启动迟缓的 CLI 工具不会被采纳, 无论它功能多强。
- **对话无限增长。** 复杂的重构任务可能产生数百次 tool call 和数千行输出。不做主动的上下文管理, 对话将在单个会话内就超出模型的 context window。
- **模型可能在生成中途触及 output token 上限。** 生成长文件或详细解释时, 回复可能被截断。循环必须检测到这一点并无缝续接, 不丢失已生成的内容。
- **API 限流产生 413 错误。** 当对话超出 context window 时需要优雅地重试, 而非向用户暴露失败。
- **可并行的 tool call 不应互相阻塞。** 三个独立的文件读取串行执行, 浪费的时间在多轮会话中会不断累积。
- **工具调用有时安全, 有时不安全。** 同一个 Bash 工具, `ls` 是只读的, `rm -rf` 是破坏性的。循环必须与工具系统协作, 在运行时判断安全性。

Claude Code 在一个精心设计的 agent 循环中解决了所有这些问题。

## Claude Code 的解法

实现分布在三个文件: `cli.tsx` (302 行) 作为入口, `QueryEngine.ts` (1,295 行) 管理对话状态, `query.ts` (1,729 行) 承载循环本体。后者是循环相关最大的单个文件, 体量反映了循环必须处理的边界情况之多。以下按执行路径, 从 CLI 启动追踪到循环的稳态运行。

### 快速启动

循环运行之前, CLI 必须先启动。Claude Code 对启动时间极度敏感 — 对于每天调用数百次的活跃开发者, 这一点至关重要。入口文件 `cli.tsx` 在执行任何重量级 import 之前, 先检查简单标志:

```typescript
// src/entrypoints/cli.tsx ~L33-42
if (args.length === 1 && (args[0] === '--version' || args[0] === '-v')) {
    console.log(`${MACRO.VERSION} (Claude Code)`);
    return;
}
```

这条快速路径在 5ms 内退出。没有它, 静态 import 树 (会触发配置加载, 遥测初始化, 能力检测等副作用 I/O) 将带来约 300ms 的延迟 — 仅仅为了打印一个版本号。`--help`, `--print-config` 等命令同样在主初始化路径之前退出。

在 `main.tsx` (4,683 行) 中, 带有副作用的 import (启动网络检查, 文件读取, 能力探测等 I/O 的模块) 被刻意放在静态 import 之前。Bun 按顺序执行 import, 因此先导入 I/O 模块意味着网络请求和文件操作在后续模块解析时已经在并行执行。这不是偶然, 而是刻意的性能优化。

启动阶段还会并行执行能力检测 — 判断哪些工具可用 (`git` 是否安装? `rg` 是否可用?), 配置了哪些 MCP server, 哪些权限已被预批准。由于与其他初始化工作并行, 这些检测的延迟大部分被隐藏了。

这些优化的净效果是 Claude Code 从调用那一刻起就感觉响应迅速。版本检查是瞬时的; 交互式启动在数百毫秒内完成; 第一个 API 调用可以在所有初始化完成之前就被发出。

### 对话的所有者

循环的状态由 `QueryEngine` 持有, 定义在 `QueryEngine.ts` (1,295 行)。

这个类是核心协调器: 持有可变对话 (消息数组), 管理 system prompt, 跟踪 token 用量, 对外暴露一个提交新用户 turn 的入口:

```typescript
// src/QueryEngine.ts ~L209-212
async *submitMessage(
  prompt: string | ContentBlockParam[],
  options?: { uuid?: string; isMeta?: boolean },
): AsyncGenerator<SDKMessage, void, unknown> {
```

`submitMessage` 是 `AsyncGenerator`。这是关键设计: 它不返回整个 turn 完成后才 resolve 的 promise, 而是在事件到达时逐个 yield。调用方 (CLI 的 React 渲染层) 可以增量处理 streaming token, tool_use block 和状态更新。

generator 协议还天然提供 backpressure — 如果 UI 处理跟不上, generator 自动暂停。取消同样自然: 对 generator 调用 `.return()` 即可干净地退出循环, 释放资源, 不需要额外的取消令牌。用户按 Ctrl+C 时, `.return()` 沿调用链传播, 中止进行中的 API 请求和工具执行。

`QueryEngine` 还管理对话的生命周期: 将对话持久化到磁盘以支持会话恢复, 跟踪哪些消息已被 compact, 维护 tool_use ID 到结果的映射。这种状态管理的复杂性是 `QueryEngine.ts` 以 1,295 行成为代码库中最大文件之一的原因。

### 双层嵌套 Generator

在 `QueryEngine` 内部, 真正的循环逻辑位于 `query.ts` (1,729 行)。这是循环相关的最大文件, 体量反映了必须处理的边界情况之多: 重试, compaction, 恢复, hook, streaming, 错误包装全部在此。实现使用两个嵌套 generator:

```typescript
// src/query.ts ~L219-239
export async function* query(params: QueryParams): AsyncGenerator<StreamEvent | Message | ...> {
  const terminal = yield* queryLoop(params, consumedCommandUuids)
  return terminal
}
```

外层 `query()` 处理一次性的设置和清理 — 参数校验, 初始消息准备, 建立已消费命令 UUID 集合。内层 `queryLoop()` 是运行实际循环迭代的状态机。

这种分离使初始化逻辑与每轮逻辑清晰分开。内层循环可以独立重启 (例如, context compaction 重组消息数组后) 而不重新执行初始化。`yield*` 委托从外层透明转发所有事件给调用方, 因此双 generator 结构对外不可见 — 调用方只与 `query()` 交互。

### State 对象

每次循环迭代操作一个显式的 state 对象:

```typescript
// src/query.ts ~L241-280
let state: State = {
    messages: params.messages,
    toolUseContext: params.toolUseContext,
    autoCompactTracking: undefined,
    turnCount: 1,
    transition: undefined,
}
```

`transition` 字段记录循环为何继续。可能的值包括 `"tool_results"` (常规情况: 工具产生了结果), `"reactive_compact_retry"` (上下文过大, 已执行 compaction), `"recovery"` (模型触及 output token 上限)。这是一条审计轨迹 — 调试异常循环行为时, transition 序列讲述了发生了什么以及为什么。

每次循环决定继续时, 在 continue 站点构造新的 state 而非就地修改。代码中有 9 个这样的 continue 站点 — 对应每种 transition 类型 — 各自构造带有相应 `transition` 值的新 `State`。这一模式使得一次迭代的状态不可能意外泄漏到下一次。

`toolUseContext` 字段承载贯穿工具执行的上下文对象 (第 2 章描述)。`autoCompactTracking` 字段监控跨 turn 的 token 使用量, 以决定何时触发主动 compaction — 它同时跟踪 token 消耗的运行总量和上次 compaction 以来的 turn 数, 允许系统基于绝对大小或增长速率进行 compact。

`turnCount` 跟踪迭代次数以强制最大 turn 限制, 防止消耗无限 API 调用的失控循环。最大值可配置, 但有合理的默认值, 允许复杂的多步任务同时防止无进展的模型反复调用工具的无限循环。

### 主循环

`queryLoop` 内部的 `while(true)` 循环, 每次迭代遵循固定的四步序列:

**第一步: 预处理流水线。** 消息数组经过五个逐步递进的 compaction 阶段, 每个阶段比前一个更激进地削减 token 数:

- `applyToolResultBudget` — 截断超出单条 token 预算的工具结果。大输出 (如对 10,000 行文件的 `cat`) 被裁剪到可配置上限, 并追加通知让模型知道内容被省略。
- `snipCompact` — 删除被标记为可裁剪的消息内容。消息可被早期处理阶段或知道自身输出将变得过时的工具标记为可裁剪。
- `microcompact` — 轻量压缩: 去除冗余空白, 合并重复空行, 裁剪尾部空白。
- `contextCollapse` — 合并同角色相邻消息, 删除内容已被完全裁剪的消息。这减少了消息数量, 而消息在 API 的每消息开销中有小但非零的成本。
- `autocompact` — 如果总 token 数超过阈值 (context window 大小减去 `AUTOCOMPACT_BUFFER_TOKENS`), 使用轻量级模型调用总结旧消息, 替换原始消息。总结模型调用很快 (通常不到 1 秒), 因为它操作一个聚焦的任务, 输出预算很小。

前四个阶段是确定性的字符串操作, 耗时可忽略 (合计不到 1ms)。第五阶段有条件触发, 涉及模型调用, 仅在 token 数超过阈值时运行。

这条流水线在每次 API 调用前运行, 而非仅在溢出时运行。原因在关键设计决策部分解释。这五个阶段共同确保上下文窗口被持续管理, 而非被动应对。

**第二步: Streaming API 调用。** 请求带着处理过的消息数组, system prompt 和工具定义发送到 Anthropic API。当回复的 chunk 通过网络到达时, streaming 层实时解析, 在完整的 `tool_use` content block 可用时立即提取。`StreamingToolExecutor` 是关键组件: 它在模型仍在生成文本时就开始执行工具, 将网络延迟与工具执行重叠。如果模型先发出一个文件读取的 tool_use block 再跟着文本, 文件读取立即启动, 不等模型生成完毕。

对于包含三个 tool call 和解释性文本的回复, 这种流水线化相比等待完整回复再启动任何工具, 可以节省数百毫秒。Streaming 层还处理部分 JSON: tool_use block 增量到达, 解析器积累片段直到完整 block 可用。这是必要的, 因为 API 逐 token 流式传输, 一个 tool_use block 的输入 JSON 可能跨越许多 token。

**第三步: 工具分发。** 已完成的 tool_use block 被发送到工具编排层 (第 3 章), 处理权限检查, 并发分批, 执行前后 hook。结果 — 每个 tool call 一条, 包含工具输出和元数据 — 作为 tool_result 消息追加到对话中。

工具结果成为下一次迭代要发送的对话的一部分。这就是模型 "看到" 其 tool call 输出的方式: 工具结果以消息形式出现在对话中, 按 API 的 tool-result 协议格式化。如果工具失败, 错误消息也被格式化为 tool result, 给模型调整策略所需的信息。

**第四步: 继续决策。** 循环评估一张决策表, 决定下一步:

| 条件 | 动作 |
|---|---|
| 回复中无 tool_use block | `completed` — 返回用户 |
| 有工具结果 | `next_turn` — 追加结果, 继续循环 |
| API 返回 413 | `reactive_compact_retry` — 激进 compact 后重试 |
| 模型触及 max output tokens | `recovery` — 用恢复 prompt 继续 (最多 3 次) |
| Stop hook 触发 | `blocking` — 暂停等待用户输入 |
| Turn 计数超限 | 返回用户, 附带截断通知 |

条件按优先级评估: 413 错误优先于工具结果 (因为请求已失败, 必须重试), max turn 限制优先于一切 (因为它是防止失控循环的硬安全边界)。

stop hook 的 `blocking` transition 值得一提: 这是用户定义的 hook (第 3 章) 暂停循环的方式。stop hook 可能在模型试图进行不可逆更改时触发, 给用户审查的机会。用户批准后, 循环从同一 state 恢复。

max output tokens 恢复受 `MAX_OUTPUT_TOKENS_RECOVERY_LIMIT = 3` 约束。三次连续触及上限后循环放弃, 不再浪费 token。真正的长回复几乎总在三次续接内完成, 而退化情况 (模型自我重复) 很少在三次后自行修正。

autocompact 系统维持 `AUTOCOMPACT_BUFFER_TOKENS = 13_000` 的缓冲, 确保模型始终有空间产生有意义的回复。这个缓冲同时考虑模型的输出和同一 turn 中可能追加的工具结果的开销。缓冲大小经过校准, 能容纳典型模型回复 (1,000-4,000 token) 加上若干中等大小的工具结果 (文件内容, 命令输出), 并留有余量。

reactive compact 路径专门处理 413 错误。常规 compaction 是保守的 (保留上下文质量), 但偶尔大量工具输出会突破缓冲。

reactive 路径执行更激进的 compaction — 可能总结或删除常规路径会保留的整段对话 — 以信息损失换取继续运行。对用户而言, 重试是不可见的, 仅表现为略长的停顿。

## 关键设计决策

**AsyncGenerator 而非 Promise。** Generator 协议在单个抽象中提供 streaming, backpressure 和取消 (通过 `.return()`)。基于 promise 的设计需要单独的 event emitter 处理 streaming, 显式的取消令牌, 以及手动的 backpressure 管理 — 为相同的能力带来更多活动部件。`yield*` 委托让外层 generator 透明转发内层事件, 无需中间缓冲或事件重新发射。

**Continue 站点的不可变 state。** 每次继续都构造新的 state 对象, 而非就地修改。这消除了一整类 bug — 例如未递增的 `turnCount`, 或保留了 compaction 前数据的 `autoCompactTracking`。代价是每轮多几次对象分配, 相比 API 调用延迟 (通常 500ms-2s) 可忽略不计。这一模式也使代码更易读: 在任何 continue 站点, 下次迭代的完整 state 集中在一处可见, 而非分散在函数各处的修改中。

**每轮都运行预处理, 而非仅在溢出时。** 持续管理上下文大小, 避免突然超出 context window 后触发昂贵的紧急 compaction。持续 compaction 也为模型产生更可预测的 token 预算, 因为上下文大小保持在更窄的范围内, 而非在接近满和刚 compact 之间振荡。轻量阶段开销可忽略, 仅 `autocompact` 涉及模型调用且仅在超阈值时触发。

**构建时 feature gate。** `feature('FLAG_NAME')` 在构建时通过 Bun bundler 解析。被禁用的特性在外部构建中被 dead-code elimination 完全移除 — 没有运行时条件开销, 不会意外暴露内部功能。这比运行时 feature flag 提供更强的保证, 后者可以被有经验的用户切换或检查。开源构建的 Claude Code 在物理上与内部构建是不同的程序, 而非同一程序配以不同设置。这对 agent 循环有具体影响: 内部构建可能有额外的 transition 类型, 不同的 compaction 策略, 或实验性的继续策略, 这些在公开构建中完全不存在。

**Streaming 工具执行。** 在模型完成生成之前就开始执行工具, 在多工具 turn 中节省可观延迟。风险 — 模型可能在回复后续部分 "改变主意", 导致提前启动的工具变得不必要 — 被 API 的 tool_use 协议缓解: tool_use block 一旦发出就不可撤回。`StreamingToolExecutor` 利用了这一保证: 只要从 stream 中解析出完整的 tool_use block, 就可以安全地开始执行。替代方案 — 等待完整回复后再启动任何工具 — 会为每个多工具 turn 额外增加模型剩余生成时间的延迟。

## 实际体验

开发者在 Claude Code 中输入消息后, 回复在数百毫秒内开始 streaming。如果模型决定读取文件并运行命令, 这些 tool call 在从 stream 中解析出来时就出现 — 文件读取可能在模型还没生成后续文本时就已完成。

循环自主运行 — 读取, 编辑, 跑测试 — 直到模型产生一个没有 tool call 的回复, 循环才停止, 用户看到最终答案。

如果对话变长 (复杂重构任务中很常见), 早期的工具结果被静默 compact 以释放上下文空间。用户不会看到这一过程; 唯一可见的效果是, 尽管对话可能跨越数十轮和数百次 tool call, 模型依然正常工作。

如果上下文大到常规 compaction 不够用, reactive 路径捕获 413 错误并在重试前执行紧急 compaction — 用户可能注意到稍长的停顿, 但对话继续而非失败。

一个具体的例子: 开发者要求 Claude Code "将所有测试文件更新为使用新的断言库"。模型读取项目配置, 识别出 12 个测试文件, 逐一读取, 逐一编辑, 然后运行测试套件。这可能需要 30+ 次 tool call, 跨越 5-6 次循环迭代。预处理流水线在整个过程中保持上下文可管理: 早期的文件读取结果在编辑完成后被 compact, 因为模型不再需要原始文件内容。`StreamingToolExecutor` 在模型还在生成当前文件编辑时就开始读取下一个文件。整个操作的完成时间远少于串行执行且无上下文管理的情况。

## 总结

- Agent 循环是 `while(true)` 状态机, 嵌套在两层 async generator 内, 由 `QueryEngine` 持有。`QueryEngine` 同时管理对话持久化和 token 跟踪。
- 每次迭代运行五阶段预处理流水线 (`applyToolResultBudget` 到 `autocompact`), 执行 streaming API 调用, 通过编排层分发工具, 并评估含六种结果的继续决策表。
- `StreamingToolExecutor` 利用 API 对已发出 tool_use block 不可撤回的保证, 在模型生成完毕前就启动工具执行。
- State 在 9 个 continue 站点重新构造, `transition` 字段记录原因, 提供完整的循环决策审计轨迹。
- 上下文 compaction 通过轻量阶段在每轮主动运行, 以 413 错误时的紧急 compaction 作为兜底。`AUTOCOMPACT_BUFFER_TOKENS = 13_000` 确保模型始终有回复空间。
- 快速启动通过早期退出的快速路径, 副作用 import 排序实现的并行 I/O, 以及并发能力检测来实现。
