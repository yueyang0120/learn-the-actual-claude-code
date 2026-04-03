# 第 3 章: 工具编排

第 2 章定义了工具是什么。本章描述当单次模型回复包含多个 tool call 时, 它们如何被调度, 执行和收尾。编排层位于 agent 循环 (第 1 章) 与单个工具实现 (第 2 章) 之间, 将一组 tool_use block 转化为一系列结果。

## 问题

模型在一次回复中可能请求多个 tool call。典型场景: 模型想读取三个文件并执行一次 grep 搜索。这四个操作都是只读且独立的 — 逐个执行是浪费时间。但如果模型在同一回复中还请求了文件写入, 该写入不能与读取并发, 因为读取可能依赖写入之前的文件系统状态。

编排问题是: 给定一个带有每次调用安全元数据的有序 tool call 列表, 将其分批以最大化并发而不违反安全约束, 然后在有界并行度下执行每批, 并在每步运行前后 hook。

第二个关注点是可扩展性。Claude Code 支持 hook — 在工具执行前后运行的用户自定义脚本。hook 可以检查, 修改或阻止 tool call。编排层必须集成 hook 而不与核心调度逻辑纠缠。

第三个关注点是资源管理。无界并行可能耗尽文件描述符, 使磁盘 I/O 饱和, 或同时向本地开发服务器发送过多请求。编排层必须限制并发, 同时仍提供相对于串行执行的实质加速。

## Claude Code 的解法

### 入口

编排逻辑位于 `toolOrchestration.ts` (188 行)。尽管职责复杂, 实现却相当紧凑 — 这说明抽象选择得当。入口是 `runTools()`, 一个接受模型回复中 tool_use block 列表并逐个 yield 结果的 async generator:

```typescript
// src/toolOrchestration.ts — 入口 (概念性)
async function* runTools(
  toolUseBlocks: ToolUseBlock[],
  context: ToolUseContext
): AsyncGenerator<ToolResult> {
  const batches = partitionToolCalls(toolUseBlocks, context)
  for (const batch of batches) {
    yield* executeBatch(batch, context)
  }
}
```

函数先将 tool call 分批, 然后按顺序执行每批, 在结果到达时 yield。`yield*` 委托意味着并发批次中各个工具完成后就立即流出结果, 而非等待整批完成。

### 分批策略

`partitionToolCalls()` 扫描有序的 tool_use block 列表, 根据单一标准分组: 每个 tool call 的 `isConcurrencySafe(input)` 返回值。连续返回 `true` 的调用被归入同一个并发批次。任何返回 `false` 的调用终止当前并发批次 (如果存在), 并创建一个只包含该调用的串行批次。

算法是单次线性扫描, O(n) 复杂度:

```typescript
// src/toolOrchestration.ts — 分批 (概念性)
function partitionToolCalls(
  blocks: ToolUseBlock[],
  context: ToolUseContext
): Batch[] {
  const batches: Batch[] = []
  let currentConcurrent: ToolUseBlock[] = []

  for (const block of blocks) {
    const tool = lookupTool(block.name, context)
    if (tool.isConcurrencySafe(block.input)) {
      currentConcurrent.push(block)
    } else {
      if (currentConcurrent.length > 0) {
        batches.push({ type: 'concurrent', blocks: currentConcurrent })
        currentConcurrent = []
      }
      batches.push({ type: 'serial', blocks: [block] })
    }
  }
  if (currentConcurrent.length > 0) {
    batches.push({ type: 'concurrent', blocks: currentConcurrent })
  }
  return batches
}
```

以下面的 tool call 序列为例:

```
[Grep, FileRead, BashWrite, GlobTool, FileRead]
```

| 工具 | `isConcurrencySafe(input)` | 批次 |
|---|---|---|
| Grep | true | 批次 1 (并发) |
| FileRead | true | 批次 1 (并发) |
| BashWrite | false | 批次 2 (串行) |
| GlobTool | true | 批次 3 (并发) |
| FileRead | true | 批次 3 (并发) |

产出三个批次。批次 1 并行运行 Grep 和 FileRead。批次 2 单独运行 BashWrite。批次 3 并行运行 GlobTool 和 FileRead。批次严格顺序执行。

原始列表中的 tool call 顺序在批次内得到保留。并发批次中工具按列表顺序启动, 但可能按执行时间以任意顺序完成。

注意算法的简洁性: 它是单次线性扫描, 不是图分析。它不尝试识别 tool call 之间的数据依赖或构建最优执行 DAG。用列表中的位置作为依赖的代理。更复杂的算法需要模型不提供的依赖信息。

### 有界并发

并发批次不会无限制地同时运行所有工具。`getMaxToolUseConcurrency()` 返回并发上限 — 默认 10, 可通过环境变量配置。

实现使用 `all()` 工具函数: 一个带信号量的有界 async generator 组合器。它接受一个 async generator 数组 (每个工具一个) 和并发限制, yield 最先完成的 generator 的结果, 确保任何时刻活跃的 generator 不超过 `limit` 个:

```typescript
// src/toolOrchestration.ts — 有界并发 (概念性)
async function* executeBatch(
  batch: ToolUseBlock[],
  context: ToolUseContext
): AsyncGenerator<ToolResult> {
  const maxConcurrency = getMaxToolUseConcurrency()
  const generators = batch.map(block => executeOneTool(block, context))
  yield* all(generators, maxConcurrency)
}
```

信号量模式防止资源耗尽。没有它, 一次请求 20 个并发文件读取的模型回复可能超出文件描述符限制。上限 10 提供了有意义的并行度, 同时保持在典型 OS 资源限制之内。通过环境变量设为 1 可有效禁用并发, 便于调试工具交互。

### 单工具流水线

每次独立的工具调用在 `executeOneTool()` 中通过一个 13 步流水线:

1. **Abort 检查**: 用户已取消操作则直接跳过。
2. **工具查找**: 将工具名解析为工具池中的 `Tool` 实例。未找到时检查 alias 和通过 `searchHint` 模糊匹配。
3. **输入校验**: 根据工具的 `inputSchema` 校验模型输入。`strict` 模式下拒绝多余字段; 非严格模式下静默忽略。
4. **行为分类**: 调用 `isReadOnly(input)` 和 `isConcurrencySafe(input)`。虽然分批时已用过, 但此处重新评估, 因为 hook (第 6 步) 可能修改了输入。
5. **权限检查**: 调用 `checkPermissions(input, context)`。结果为 `deny` 时返回结构化错误; `ask` 时显示权限提示等待用户回复; `allow` 时继续。
6. **PreToolUse hook**: 运行注册的 `PreToolUse` hook。每个 hook 接收工具名, 输入和上下文, 返回三种结果之一 (详见下文)。
7. **执行**: 调用 `tool.call(input, context)`, 消费 async generator。中间 yield (进度事件) 被转发到 UI。
8. **结果 yield**: 将工具结果 yield 回编排层, 转发到 agent 循环以加入对话。
9. **PostToolUse hook**: 运行注册的 `PostToolUse` hook, 接收工具名, 输入, 结果和上下文。可观察但不可修改结果。
10. **PostToolUseFailure hook**: 如果工具执行期间抛出错误, 运行失败专用 hook。
11. **Context modifiers**: 处理工具返回的 `MessageUpdate` 对象 (下文解释)。
12. **遥测**: 记录执行时间, 成功/失败状态, 工具名, 输入大小。
13. **错误包装**: 任何步骤抛出未处理异常时, 将其包装为结构化错误结果。模型收到 "Tool execution failed: [错误消息]" 而非经历崩溃。

### PreToolUse Hook 详解

PreToolUse hook 是控制工具行为的主要扩展点。hook 接收工具名, 输入和上下文, 返回三种结果之一:

- **Allow**: 工具调用正常进行。
- **Block**: 阻止工具调用。hook 提供原因字符串, 作为 tool result 返回给模型, 使模型理解为何被阻止并调整策略。
- **Modify**: hook 返回修改后的输入对象。工具使用修改后的输入执行。例如将相对路径转换为绝对路径, 或为 shell 命令添加默认 flag。

应用场景包括: 阻止对 `*.lock` 文件的所有写入, 重写文件路径以强制沙箱目录, 记录每条 shell 命令 (审计), 注入环境变量, 限速 tool call 等。

Hook 由用户在项目设置中配置, 而非内建于 Claude Code。它们作为外部进程运行 (通常是 shell 脚本或小程序), 提供语言无关的可扩展性, 代价是每次 tool call 的子进程开销。子进程通过 stdin 接收 JSON 格式的 hook 输入, 通过 stdout 返回 JSON 响应。

### MessageUpdate: 声明式上下文修改

某些工具结果需要修改对话状态, 而不仅仅是追加一条结果。例如, 改变当前工作目录的工具需要更新 system prompt 中的环境部分; 安装新 MCP server 的工具需要更新工具池和 MCP 指令。

工具不直接写入对话状态 (那会造成复杂的所有权问题和并发批次中的竞态条件), 而是随结果返回 `MessageUpdate` 对象。`MessageUpdate` 是声明式指令 — "将 CWD 设为 /foo/bar" 或 "将此 MCP 工具添加到池中" — 而非命令式修改。

`MessageUpdate` 支持几种更新类型:

- **System prompt 更新**: 修改环境信息, CLAUDE.md 内容或其他动态 prompt 部分。
- **工具池修改**: 添加或删除工具 (通常由 MCP server 连接或断开触发)。
- **消息修改**: 对对话中较早消息的追溯修改 (罕见, 但上下文管理工具会使用)。

编排层收集同批所有工具结果中的 `MessageUpdate`, 在整批完成后处理。更新按原始列表中的 tool call 索引以确定性顺序应用, 而非按完成顺序 (不确定的)。确定性排序防止微妙的 bug — 最终状态不依赖于哪个工具恰好先完成。

这种方式使工具从自身视角保持无状态, 同时仍能影响更广泛的对话上下文。

## 关键设计决策

**基于每次调用标志的分批, 而非每个工具的标志。** 因为 `isConcurrencySafe` 是输入的函数 (见第 2 章), 分批逻辑准确反映每次特定调用的安全性。一个通常安全但偶尔不安全的工具 (如 Bash) 在每种情况下都得到正确处理, 编排器无需特殊处理个别工具。

**批次间串行, 批次内并发。** 这是保守策略。更激进的方式会分析 tool call 间的数据依赖并构建 DAG, 可能并行运行独立的不安全操作。但模型不提供显式依赖信息, 从工具输入推断依赖 (如检查两个文件写入是否目标相同路径) 既脆弱又不完整。顺序批次方式以排列顺序代理依赖关系, 牺牲部分潜在并行度换取正确性。对于错误结果远比几百毫秒额外延迟代价更高的系统, 这是合理的权衡。

**有界并发与可配置上限。** 默认值 10 是务实的选择。大多数模型回复包含 1-5 个 tool call, 上限很少构成约束。但当它生效时 (如模型一次读取 15 个文件), 上限防止资源耗尽。通过环境变量配置让高级用户可以针对自身硬件调优。

**Hook 作为外部进程。** 以子进程而非进程内回调运行 hook, 意味着 hook 不会崩溃 Claude Code, 不能访问内部状态, 可以用任何语言编写。代价是延迟: 每次 hook 调用付出子进程启动开销 (~5-20ms)。对于典型的每次 tool call 0-2 个 hook, 这是可接受的。

**13 步而非更少。** 流水线看起来可能过度分解 — abort 检查, 校验和分类能否合为一步? 可以, 但分离提供了更清晰的错误消息, 更精确的遥测, 更易于测试。每步有不同的失败模式和恢复动作。合并会模糊哪步失败以及为何失败。

## 实际体验

一个典型的编排场景: 用户要求 Claude Code 重构一个函数。模型回复包含五个 tool call: 三个文件读取 (理解当前代码), 一个文件写入 (重构后的代码), 一个 Bash 调用 (跑测试)。

编排器将它们分为三批: 三个读取并发运行 (~100ms 总计, 而非 ~300ms 串行), 然后写入单独运行 (可能带权限提示), 然后测试单独运行。如果用户配置了一个记录所有文件写入的 PreToolUse hook, 它在写入步骤前触发, 增加约 10ms 开销。配置了在测试失败时通知 Slack 的 PostToolUse hook, 则在 Bash 完成后触发, 但仅当退出码指示失败时。

用户看到读取操作快速连续完成, 写入时短暂停顿 (如果未自动批准则有权限提示), 然后测试输出 streaming 进入。编排是不可见的, 只能从效果中感知: 事情比串行执行更快, 安全约束从未被违反, 用户定义的 hook 在正确时刻运行。

## 总结

- `runTools()` 通过线性扫描将 tool call 分批: 连续的并发安全调用并行运行; 不安全调用形成单元素串行批次。
- 批次内并发受可配置上限 (默认 10) 约束, 使用基于信号量的 `all()` async generator 组合器。
- 每次工具调用通过 13 步流水线, 涵盖 abort 检查, 校验, 权限, hook, 执行, 遥测和错误处理。
- PreToolUse hook 可以阻止, 允许或修改 tool call; PostToolUse 和 PostToolUseFailure hook 观察结果。所有 hook 作为隔离子进程运行。
- `MessageUpdate` 对象允许工具以声明式方式请求上下文修改, 在批次完成后按确定性顺序处理。
