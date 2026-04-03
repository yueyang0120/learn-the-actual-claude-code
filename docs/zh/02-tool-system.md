# 第 2 章: 工具系统

第 1 章的 agent 循环决定何时调用工具。本章描述工具是什么 — 它的接口, 行为标志, 注册方式, 以及如何组装成模型可见的工具池。工具系统是 Claude Code 运行时与其数十种能力之间的契约。

## 问题

编程助手需要大量工具: 文件读取, 文件写入, shell 执行, 搜索, LSP 查询等。每种工具有不同的安全特征。读文件是无害的; 执行 `rm -rf /` 是灾难性的。有些工具可以并发执行, 有些必须串行。有些始终可用, 有些取决于 feature flag 或外部服务。

朴素的做法 — 一个 switch 语句, 每个工具名一个 case — 不可扩展。添加新工具需要同时修改分发逻辑, 权限逻辑, 并发逻辑和 prompt 组装逻辑。面对 20+ 内建工具加上不限数量的 MCP 工具, 这种方式几个月内就会变得不可维护。

系统需要一个统一接口, 将所有这些关注点封装在每个工具内部, 使核心循环对任何单个工具的具体行为保持无知。

还有一层微妙之处: 同一个工具对不同输入可以有不同的安全特征。Bash 工具执行 `ls` 是只读的; 执行 `rm` 是破坏性的。静态的类型级标注无法捕捉这一点。安全分类必须是输入的函数, 在调用时而非注册时求值。

## Claude Code 的解法

### Tool 接口

Claude Code 中的每个工具都实现定义在 `Tool.ts` (792 行) 中的泛型 `Tool` 接口。这是代码库中最重要的类型定义之一, 建立了每个工具 — 内建的或外部的 — 必须满足的契约。

接口包含超过 30 个字段, 概念上最重要的是:

```typescript
// src/Tool.ts — 简化接口
interface Tool {
  name: string
  inputSchema: object
  isReadOnly(input): boolean
  isConcurrencySafe(input): boolean
  call(input, context: ToolUseContext): AsyncGenerator<ToolResult>
  checkPermissions(input, context): PermissionResult
  prompt(): string
}
```

几个设计要点值得关注。

**`isReadOnly(input)` 和 `isConcurrencySafe(input)` 是函数, 不是布尔值。** 它们接收工具输入, 返回针对该特定调用的分类。这正是 Bash 工具在命令为 `ls` 时报告自身为只读, 而命令为 `rm` 时报告为读写的机制。编排层 (第 3 章) 在分发时调用这些函数, 以决定某次工具调用能否与其他调用并行运行。

Bash 这类工具的实现会检查命令字符串来做这个判断。以 `cat`, `ls`, `head`, `grep` 开头的命令被分类为只读; 包含 `>`, `>>`, `rm`, `mv`, `chmod` 的命令被分类为读写。分类本质上是启发式的 — 无法以完美精度解析任意 shell 管道 — 但偏向保守: 无法识别的命令被视为读写。相比构建完整 shell 解析器的复杂性和脆弱性, 启发式方法在常见情况下正确, 在边界情况下安全。这是务实的选择: 完整的 shell 解析器既复杂又脆弱, 而启发式方法正确处理常见情况并在边界情况下安全回退。

**`call()` 是 AsyncGenerator。** 与 agent 循环一样, 工具执行使用 generator 而非 promise。工具可以在 yield 最终结果之前, yield 中间进度事件 (对长时间运行的 shell 命令很有用)。generator 协议还允许运行时通过 `.return()` 在执行中途中止工具。对于一个无限运行的 shell 命令, 这提供了干净的取消机制, 无需工具内部实现显式超时逻辑。generator 还使 UI 能在工具运行时显示部分输出 — 当测试套件逐行产出时, 用户看到增量结果, 而非等套件结束后才一次性看到全部。

**`prompt()` 返回字符串。** 每个工具向 system prompt (第 4 章) 贡献自己的描述段落, 向模型说明其能力和使用约定。这使得工具相关的 prompt 文本与工具代码共置, 而非集中在单个 prompt 文件中。当工具行为变化时, prompt 文本在同一文件中更新, 确保文档与实现同步。

prompt 文本不只是描述; 它常包含塑造模型行为的指令。例如, FileRead 工具的 prompt 可能指示模型对大文件请求特定行范围而非读取整个文件, 直接影响模型如何使用工具以及消耗多少 context 预算。

**`checkPermissions(input, context)` 与 `call()` 分离。** 权限检查在执行前发生, 可返回 `allow`, `deny` 或 `ask` (提示用户)。将权限检查与执行分离, 意味着权限系统可以独立测试, 审计和覆盖。编排层也可以在执行任何工具之前批量检查权限, 从而一次性提示用户批准多个 tool call, 而非反复中断执行。

权限结果还可以携带元数据: 当结果为 `ask` 时, 它包含一个可读描述, 说明工具意图做什么 ("向 /src/app.tsx 写入 45 行"), 显示在权限提示中。这个描述由工具自身生成, 而非编排层, 因为只有工具知道如何以人类语言解释自己的输入。

### 更多接口字段

除核心字段外, `Tool` 接口还包含约二十多个额外字段, 分为身份字段, 行为标志和集成标记。最值得关注的:

- `aliases`: 模型可以使用的替代名称。处理模型生成合理但非规范工具名 (如 `ReadFile` 而非 `FileRead`) 的情况。别名在模糊匹配之前检查, 是处理已知名称变体的首选机制。
- `searchHint`: 当模型生成完全无法识别的工具名时, 用于模糊匹配的文本。运行时计算生成名称与每个工具的 name 和 searchHint 之间的编辑距离, 如果在可配置阈值内则返回最接近的匹配。这种优雅降级意味着 tool call 很少因轻微命名错误而失败。
- `shouldDefer`: 是否应延迟到后续阶段执行。某些工具 (如修改对话本身或更新 system prompt 的工具) 必须在同批其他工具完成后执行, 因为它们的效果依赖于其他工具的结果。
- `isMcp` 和 `isLsp`: 布尔标志, 指示工具是否由 MCP 或 LSP 支撑。影响错误处理 (MCP 工具可能有值得重试的瞬态 server 错误), 超时行为 (LSP 操作有不同于本地工具的延迟特征), 以及 UI 显示方式 (外部工具显示其 server 来源)。
- `isDestructive`: 静态标志, 标记始终具有破坏性的工具。与 `isReadOnly` (依赖输入, 默认 false) 不同, `isDestructive` 是固定属性。被标记的工具触发超出常规权限系统的额外确认提示, 即使在 auto-approve 模式下也是如此。
- `strict`: 是否对输入 schema 校验使用严格模式。启用时, 工具拒绝不完全匹配 schema 的输入, 确保类型安全。禁用时, 容忍额外字段 — 对 MCP 工具有用, 因为 server 和 client 之间的版本不匹配可能导致 schema 合规性不完美。

### Fail-Closed 默认值

工具通过 `buildTool()` 工厂函数构造, 为每个行为标志提供默认值:

```typescript
// src/Tool.ts — buildTool 默认值 (概念性)
function buildTool(partial: Partial<Tool>): Tool {
  return {
    isEnabled: () => false,           // 默认禁用
    isReadOnly: () => false,          // 默认有副作用
    isConcurrencySafe: () => false,   // 默认并发不安全
    isDestructive: false,
    isMcp: false,
    isLsp: false,
    strict: false,
    ...partial,
  }
}
```

这是 fail-closed 设计。如果工具作者忘记设置某个标志, 工具将被禁用, 被视为有副作用, 并被串行执行。

失败模式是过度谨慎, 而非过度宽松。

考虑反面: 如果 `isEnabled` 默认为 `true`, 定义不完整的工具可能在无安全检查的情况下运行。如果 `isConcurrencySafe` 默认为 `true`, 有意外副作用的工具可能在并行运行时破坏状态。如果 `isReadOnly` 默认为 `true`, 破坏性工具可能完全绕过权限检查。每种场景都比保守默认值更糟糕。

fail-closed 方案也意味着当新行为标志加入接口时, 系统自动安全: 不设置新标志的现有工具获得保守默认值, 无需更新数十个工具定义。

这一设计对 MCP 工具尤为重要 — MCP 工具由外部 server 定义, 可能有不完整或不正确的元数据。一个未声明自身为读写的 MCP server 工具将被默认视为读写 — 正确的保守假设。fail-closed 默认值充当了整个 MCP 生态系统的安全网。

`buildTool()` 模式还提供了一个定义默认值的唯一位置, 使审计字段被省略时的行为变得直接。对 `buildTool` 的搜索能找到代码库中每个工具定义; 检查工厂函数即可揭示任何被省略字段的默认行为。

### ToolUseContext 对象

每次 tool call 都收到一个 `ToolUseContext` — 一个约 40 个字段的大型上下文对象, 贯穿整个工具执行流水线:

- **Options**: 配置信息 — 当前工作目录, 模型参数, feature flag, 权限模式, 配置的 hook, MCP server 列表, 输出格式偏好。
- **State**: 当前消息数组, abort signal, turn 计数, session ID, 以及本次会话中已读写的文件集合。
- **UI Callbacks**: 更新终端显示的函数 — 进度指示器, 权限提示, streaming 输出渲染器。以回调注入而非直接 import, 这将工具执行与特定 UI 框架解耦, 使同一套工具实现能同时工作在交互式 CLI 和 headless/API 模式下。
- **Tracking**: 记录执行时间, token 用量和错误率的遥测 hook。开源构建中为空操作, 内部构建中激活, 通过第 1 章描述的构建时 feature gate 机制实现。
- **Metadata**: 会话信息, 用户身份 (用于权限作用域), 环境 (OS, shell, git 状态), 以及对话的 UUID。

将这些打包为单个类型化对象而非分别传参, 使得添加新的上下文字段不需要修改每个工具的函数签名 — 在新上下文频繁添加的演进代码库中, 这是显著优势。`ToolUseContext` 在每个 agent 循环 turn 中构造一次, 在该 turn 的所有工具调用间以只读方式共享。工具不应修改它; 如需将状态变更传回循环, 使用第 3 章描述的 `MessageUpdate` 机制。

上下文对象的一个值得注意的方面是它包含当前消息数组 — 这意味着工具可以在需要时检查对话历史。例如, FileWrite 工具可能检查文件是否最近被读取过 (通过扫描消息中先前的 FileRead 结果), 以便在模型未读取当前内容就写入时发出警告。这种内省能力强大但使用节制, 避免工具与对话结构产生耦合。

### 工具注册与组装

工具在 `tools.ts` (389 行) 中注册。`getAllBaseTools()` 返回内建工具列表, 条件注册受 feature flag 控制:

```typescript
// src/tools.ts — 概念结构
function getAllBaseTools(): Tool[] {
  const tools = [
    bashTool, fileReadTool, fileWriteTool,
    globTool, grepTool, lspTool,
    // ... 约 20 个内建工具
  ]
  if (feature('NOTEBOOK_EDIT')) {
    tools.push(notebookEditTool)
  }
  if (feature('TASK_TOOL')) {
    tools.push(taskTool)
  }
  return tools
}
```

`feature()` 函数在构建时通过 Bun bundler 解析。被禁用的 feature flag 导致整个条件分支 — 包括工具的 import — 从构建产物中被 dead-code elimination 移除。外部构建在物理上不包含内部专用工具, 而非仅通过运行时检查隐藏。这个区别对安全性有意义: 运行时检查可以被有经验的用户绕过; dead-code elimination 不能。对包体积也有意义: 不可达的工具及其依赖不包含在发布的二进制文件中。

最终的工具池由 `assembleToolPool()` 组装, 依次执行四步:

1. **收集内建工具** — 来自 `getAllBaseTools()`, 约 20 个。这是 Claude Code 出厂附带的基础工具集。

2. **添加 MCP 工具** — 来自已连接 server, 命名格式为 `mcp__<server>__<tool>` (如 `mcp__github__create_issue`)。双下划线分隔符不太可能出现在正常名称中, 确保解析无歧义。
3. **按名称去重** — 内建工具优先于同名 MCP 工具, 防止 MCP server 影子替换核心工具。这是安全措施: 恶意 MCP server 无法用木马版本替换 `Bash` 工具。

4. **按名称字母序排列** — 确保 prompt cache 稳定性 (下文解释)。

字母序排列不是装饰性的。工具列表被包含在 system prompt (第 4 章) 中, 稳定的排序确保相同工具集的 prompt token 序列在请求间完全一致, 最大化 API prompt caching 层的命中率。如果工具处于插入顺序或 hash map 迭代顺序, prompt 在工具集未变时也会不同, 每次不同的排列都会使 cache 失效, 迫使 API 重新处理数千个相同内容的 token。字母序排列以零运行时成本消除了这一 cache 失效源。

## 关键设计决策

**依赖输入的行为标志。** 将 `isReadOnly` 和 `isConcurrencySafe` 做成输入的函数 — 而非工具的静态属性 — 是工具系统中最重要的设计选择。

它使单个 `Bash` 工具能服务于所有 shell 命令, 同时为编排器提供每次调用的精确安全元数据。替代方案 — 分离的 `BashRead` 和 `BashWrite` 工具 — 会倍增工具数量并复杂化模型的工具选择。这也意味着要在工具选择时强迫模型预测命令是否有副作用 — 而这种分类任务由确定性启发式处理比模型判断更可靠。模型的工作是决定做什么; 工具的工作是分类该操作有多安全。

**`buildTool()` 的 fail-closed 默认值。** 让安全默认值成为缺省默认值, 意味着定义不完整的工具不会意外获得提升的权限。这在可以动态添加第三方 MCP 工具的系统中尤为重要 — 一个格式错误的 MCP 工具定义不应通过缺失字段获得意外能力。

当新行为标志加入接口时, 不设置新标志的现有工具自动获得保守默认值, 无需更新数十个工具定义。

**工具拥有自己的 prompt 文本。** 将 `prompt()` 与工具实现共置, 意味着工具行为变化时, 模型的文档在同一个 commit 中变化。集中的 prompt 文件在工具被不同贡献者独立修改时, 不可避免地与现实脱节。共置模式自然扩展: 添加新工具自动添加其文档; 删除工具自动删除其文档。无需协调。

**单个大型上下文对象。** `ToolUseContext` 方案以可发现性 (需要查看类型定义才知道有什么可用) 换取可扩展性 (新字段不需要签名变更)。在一个新上下文频繁加入的快速演进代码库中, 可扩展性胜出。

类型系统确保工具访问不存在的字段会在编译时失败, 缓解了可发现性的问题。替代方案 — 逐个传参 — 会创建 10+ 参数的函数, 可读性和维护性都更差。

## 实际体验

当模型发出 `tool_use` block — 比如 `{ name: "Bash", input: { command: "ls -la" } }` — 运行时按名称查找 `Bash` 工具, 调用 `isReadOnly({ command: "ls -la" })` (返回 `true`), 调用 `isConcurrencySafe({ command: "ls -la" })` (返回 `true`), 检查权限, 然后调用 `call()`。因为这次调用是只读且并发安全的, 它可以与同批中其他安全的 tool call 并行运行。

紧接着的 `{ name: "Bash", input: { command: "npm install" } }` 两个标志都返回 `false`, 强制串行执行并弹出权限提示。用户看到确认对话框; 只有批准后命令才执行。

如果模型生成了无法识别的工具名 — 比如 `ReadFile` 而非 `FileRead` — 运行时先检查 alias, 再通过 `searchHint` 回退到模糊匹配。大多数情况下找到正确工具, 执行透明进行。如果在可配置阈值内未找到匹配, 结构化错误返回给模型, 模型通常在下一轮自我修正。

工具系统的统一性也简化了调试。每次 tool call 走相同的路径: 查找, 分类, 检查权限, 执行。遥测事件形状相同, 权限提示格式相同, 错误消息结构相同。理解一次 tool call 就意味着理解所有 tool call — 编排层中没有隐藏的特殊情况。

对工具作者 (无论是编写内建工具还是 MCP server 工具) 而言, 接口提供了清晰指引: 实现所需方法, 准确设置行为标志, 运行时处理其余一切 — 调度, 权限, streaming, 错误恢复和 prompt 组装。

## 总结

- 每个工具实现统一的 `Tool` 接口, 约 30 个字段涵盖身份, schema, 行为, 执行, 权限和 prompt 贡献。接口定义在 `Tool.ts` (792 行)。
- `isReadOnly` 和 `isConcurrencySafe` 是工具输入的函数, 使同一工具 (尤其是 Bash) 在不同调用中可以有不同的安全分类。
  分类是启发式的, 偏向保守。
- `buildTool()` 提供 fail-closed 默认值: 工具默认禁用, 有副作用, 串行执行, 除非显式声明。这对元数据可能不完整的 MCP 工具尤为关键。
- `tools.ts` (389 行) 中的工具注册受构建时 feature gate 控制; 禁用的工具在构建产物中物理上不存在。
- `assembleToolPool()` 合并内建和 MCP 工具, 去重 (内建优先防止影子替换), 按字母序排列以保证 prompt cache 稳定性。
- `ToolUseContext` (约 40 个字段) 将所有运行时上下文打包为单个类型化对象, 实现可扩展性而不破坏工具签名。
