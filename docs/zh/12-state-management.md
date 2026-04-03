# 第 12 章: 状态管理

前面各章描述的每个子系统 -- 工具、权限、MCP 连接、任务、hooks -- 都在生产和消费状态。权限模式变更, 模型选择持久化, 队友加入, 投机执行启动。没有集中管理, 这些状态就以可变全局量的形式散落在各模块中, 导致更新竞争、副作用遗漏, 以及内部消息列表与 API 期望格式之间的偏离。本章考察 Claude Code 如何将所有可变状态集中到单一 store, 并将内部消息流标准化以供 API 消费。

## 问题

CLI agent 从多个来源积累状态。权限系统跟踪当前模式和已积累的规则。MCP 层维护多个 server 的连接状态。任务系统持有后台工作项注册表。UI 需要知道当前选择了哪个模型、哪个视图处于活跃状态、投机执行是否正在进行。

如果每个子系统各自管理状态, 会产生三个问题。第一, 没有单一位置可以观察到什么发生了变化 -- 调试需要追踪散布各处的变更点。第二, 副作用 (持久化模型选择、同步权限) 必须在每个变更点分别触发, 造成重复和不一致。第三, 对话消息的内部表示远比 API 接受的丰富: system 消息、进度指示器、tombstone、tool-use 摘要都必须在每次 API 调用前被剥离或转换。

标准化问题尤为微妙。Claude API 强制约束: 消息必须在 user 和 assistant 角色之间交替, 第一条消息必须是 user 角色, `tool_use` block 必须与 `tool_result` block 配对。内部消息列表有意违反所有这些约束, 因为它同时服务于 UI 和调试。

## Claude Code 的解法

### Store 原语

基础是一个泛型 `Store<T>` 类 -- 大约 35 行代码。它持有单个状态值, 接受函数式 updater (从不接受原始值), 用 `Object.is` 执行恒等性检查以跳过空操作更新, 并通知 `onChange` hook 和一组订阅者。

```typescript
// src/state/Store.ts
class Store<T> {
  private state: T;
  private listeners: Set<() => void> = new Set();
  private onChange?: (next: T, prev: T) => void;

  constructor(initial: T, onChange?: (next: T, prev: T) => void) {
    this.state = initial;
    this.onChange = onChange;
  }

  getState(): T {
    return this.state;
  }

  setState(updater: (prev: T) => T): void {
    const prev = this.state;
    const next = updater(prev);
    if (Object.is(next, prev)) return; // 恒等性检查
    this.state = next;
    this.onChange?.(next, prev);
    for (const fn of this.listeners) fn();
  }

  subscribe(listener: () => void): () => void {
    this.listeners.add(listener);
    return () => this.listeners.delete(listener);
  }
}
```

恒等性检查是核心不变量。如果 updater 函数返回完全相同的对象引用, 没有 listener 被触发, 没有副作用发生。因此可以安全地投机性调用 `setState` -- 传入一个可能产生也可能不产生新状态的 updater -- 而无需担心虚假通知。

### AppState: 分区字段

`AppState` 类型包含约 100 个字段, 组织为 10 个逻辑分区。部分分区在初始化后不可变 (配置、权限), 其余在运行时频繁变更 (UI 状态、任务注册表、投机执行标志)。

```typescript
// src/state/AppState.ts (代表性结构)
interface AppState {
  // 分区 1: 核心配置 (初始化后不可变)
  readonly settings: Settings;
  readonly mainLoopModel: string;

  // 分区 2: 权限 (初始化后不可变)
  readonly permissionMode: PermissionMode;
  readonly approvedTools: Set<string>;

  // 分区 3: UI (频繁变更)
  viewState: ViewState;
  inputMode: InputMode;

  // 分区 4: Bridge
  bridgeState: BridgeState;

  // 分区 5: MCP
  mcpConnections: Map<string, McpConnectionState>;

  // 分区 6: Plugin
  loadedPlugins: PluginState[];

  // 分区 7: 任务
  taskRegistry: Map<string, TaskState>;

  // 分区 8: 团队
  teamState: TeamState | null;

  // 分区 9: 投机执行
  speculationRef: SpeculationState | null;

  // 分区 10: Feature flag
  featureFlags: Record<string, boolean>;
}
```

不可变/可变的划分是约定而非运行时强制。分区 1 和 2 在 bootstrap 阶段设定后不再修改。分区 3 到 10 在会话期间通过 `setState` 更新。分区结构帮助开发者按关注点定位状态, 而非在 100 个字段的平坦列表中搜索。

### onChange 反应器

`Store` 构造函数接受一个 `onChange` 回调, 在每次状态转换时触发。这个单一函数取代散落各处的回调, 成为所有副作用的唯一可审计站点。

```typescript
// src/state/onChangeAppState.ts
function onChangeAppState(next: AppState, prev: AppState): void {
  // 同步权限模式到环境
  if (next.permissionMode !== prev.permissionMode) {
    syncPermissionMode(next.permissionMode);
  }

  // 持久化模型选择到磁盘
  if (next.mainLoopModel !== prev.mainLoopModel) {
    persistModelChoice(next.mainLoopModel);
  }

  // 持久化视图状态以供会话恢复
  if (next.viewState !== prev.viewState) {
    persistViewState(next.viewState);
  }
}
```

每个副作用都是 `next` 和 `prev` 之间的字段级比较。这个模式扩展性良好: 添加新副作用只需添加一个比较块, 不会干扰已有逻辑。

### React 集成

UI 层使用 React (通过 Ink 进行终端渲染)。Store 通过 provider 和 selector hook 集成, 遵循与 Zustand 等库相同的模式。

```typescript
// src/state/AppStateProvider.tsx
const AppStateContext = createContext<Store<AppState>>(null);

function useAppState<S>(selector: (state: AppState) => S): S {
  const store = useContext(AppStateContext);
  return useSyncExternalStore(
    store.subscribe,
    () => selector(store.getState())
  );
}

function useSetAppState(): (updater: (prev: AppState) => AppState) => void {
  const store = useContext(AppStateContext);
  return store.setState.bind(store);
}
```

`selector` 函数对性能至关重要。只需要 `viewState` 的组件传入 `(s) => s.viewState`, 仅在该字段变化时重新渲染 (前提是 selector 返回新引用)。完全不使用状态的组件永远不会因状态变化而重新渲染。

### 消息类型层次

内部消息列表不是简单的 `{role, content}` 对象数组。它是一个包含六种主要类型的区分联合, 其中 `SystemMessage` 进一步分为 14 种以上子类型。

```typescript
// src/messages/types.ts
type Message =
  | UserMessage
  | AssistantMessage
  | SystemMessage          // 14+ 子类型: init, compact_boundary,
  | ProgressMessage        //   tool_status, local_command, ...
  | TombstoneMessage
  | ToolUseSummaryMessage;

interface SystemMessage {
  type: "system";
  subtype:
    | "init"
    | "compact_boundary"
    | "tool_status"
    | "local_command"
    | "permission_grant"
    // ... 14+ 子类型
  ;
  content: ContentBlock[];
}
```

丰富的类型层次服务于 UI (progress 消息驱动 spinner), 调试 (tombstone 标记已移除内容), 以及会话管理 (compact boundary 分隔压缩前后的 context)。这些类型都不是合法的 API 消息。

### 标准化管道

每次 API 调用前, `normalizeMessagesForAPI()` 将内部消息流转换为 Claude API 要求的格式。管道分五个阶段。

```typescript
// src/messages/normalizeMessagesForAPI.ts
function normalizeMessagesForAPI(messages: Message[]): ApiMessage[] {
  // 阶段 1: 过滤 — 移除 system, progress, tombstone 消息。
  //         将 local_command 转换为 user 消息。
  let filtered = messages.filter(m =>
    m.type === "user" || m.type === "assistant"
  );

  // 阶段 2: 重排 — 确保 tool_result 跟在其 tool_use 之后。
  filtered = reorderToolPairs(filtered);

  // 阶段 3: 转换 — 剥离 thinking block, 为空内容插入哨兵文本。
  filtered = filtered.map(transformContent);

  // 阶段 4: 合并 — 合并相邻的同角色消息。
  const merged = mergeAdjacentSameRole(filtered);

  // 阶段 5: 确保第一条消息是 user 角色。
  if (merged[0]?.role !== "user") {
    merged.unshift({ role: "user", content: "[system initialized]" });
  }

  return merged;
}
```

阶段 4 处理一种常见情况: 过滤掉 system 消息后, 可能出现两条连续的 user 消息。API 拒绝这种情况, 因此管道将它们合并。阶段 5 处理过滤后第一条存活消息为 assistant 的边界情况, 因为 API 要求对话以 user 轮次开始。

### Bootstrap 状态与投机执行

两个额外的状态系统与 AppState 共存。Bootstrap state 持有启动期间收集的配置 (CLI 参数、环境检测、设置文件加载), 在初始化期间桥接到 AppState。Speculation state 使用可变 ref 而非 Store 模式, 因为投机执行需要高频更新, 不适合承担变更检测和 listener 通知的开销。

```typescript
// src/state/speculation.ts
interface SpeculationState {
  active: boolean;
  pendingMessages: Message[];
  checkpoint: AppState;  // 拒绝时回滚到此快照
}
```

Speculation checkpoint 在投机执行开始时捕获 AppState 的快照。如果投机被拒绝, 系统回滚到该 checkpoint, 而非尝试逐个撤销变更。

## 关键设计决策

**函数式 updater 而非直接赋值。** 要求 `setState(prev => ({...prev, field: newValue}))` 而非 `state.field = newValue`, 确保每次变更都流经单一入口。这使 onChange 反应器可靠 -- 它始终能看到完整的前后状态。

**Object.is 恒等性检查而非深度比较。** 对 100 个字段的状态对象做深度相等比较代价高昂。恒等性比较是单次指针检查。代价是 updater 必须返回新的对象引用来表示变更, 这是不可变更新模式的标准做法。

**单一 onChange 反应器而非逐字段 watcher。** 逐字段 watcher 系统 (如 Vue 的 `watch`) 粒度更细, 但更难审计。单一反应器函数将所有副作用展示在一个文件中, 便于验证某个状态变更是否触发了正确的下游动作。

**五阶段标准化而非维护并行的 API-ready 列表。** 维护两个同步的消息列表 (内部和 API-ready) 有偏离风险。从单一真相源按需标准化消除了这个风险, 代价是重复计算。管道足够快, 相对于它前面的 API 调用, 开销可以忽略。

**投机执行用可变 ref 而非 Store。** 投机执行涉及单个渲染周期内的高频状态更新。每次更新都通过 Store 的恒等性检查、onChange 反应器和 listener 通知会引入不必要的开销。可变 ref 绕过这些机制, 前提是投机状态是短暂的, 不需要副作用追踪。

## 实际体验

用户通过 CLI 切换模型时, UI 调用 `setState(prev => ({...prev, mainLoopModel: "claude-sonnet-4-20250514"}))`。Store 的恒等性检查检测到新引用。onChange 反应器触发, 比较新旧 `mainLoopModel` 值。因为不同, 调用 `persistModelChoice()` 将选择写入磁盘。通过 `useAppState(s => s.mainLoopModel)` 订阅的 React 组件重新渲染以反映变更。

Agent loop 准备 API 调用时, 将内部消息列表传入 `normalizeMessagesForAPI()`。包含 40 条内部消息 (含进度更新、system init 消息、compact boundary 和 tombstone) 的对话, 产出 12 条干净的 API 消息, 严格在 user 和 assistant 角色之间交替。

## 总结

- 泛型 `Store<T>` 以函数式 updater、恒等性检查和 onChange hook 将所有状态管理集中在约 35 行代码中。
- AppState 包含约 100 个字段, 分布在 10 个逻辑分区中, 不可变配置与频繁变更的运行时状态有清晰分界。
- 单一 onChange 反应器取代分散的副作用回调, 使所有状态驱动的持久化和同步在一个位置可审计。
- 内部消息层次 (6 种主类型, 14+ 种 system 子类型) 服务于 UI、调试和会话管理, 然后通过五阶段标准化管道折叠为干净的 API 格式。
- 投机执行状态使用可变 ref 以获取性能, checkpoint 快照支持拒绝时的完整回滚。
