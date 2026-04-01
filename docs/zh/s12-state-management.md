# Session 12 -- 状态管理

s01 > s02 > s03 > s04 > s05 | s06 > s07 > s08 > s09 > s10 | s11 > **s12** > s13 > s14

> "State is easy. State that 14 subsystems can read and write without corrupting each other -- that is hard."
> -- "状态本身很简单。让 14 个子系统能同时读写而不互相破坏 -- 那才是难点。"
>
> *Harness 层: Claude Code 使用受 Zustand 启发的 Store，支持函数式更新、onChange 副作用反应器、8+ 种区分类型的消息，以及在每次 API 调用前剥离内部消息的标准化管道。*

---

## 问题

一个处理工具调用、压缩、权限、MCP 连接、团队协调和任务管理的 CLI agent 会积累大量状态。简单的做法 -- 分散的全局变量或扁平字典 -- 很快就会崩溃：

1. **谁拥有数据？** 如果权限系统、UI 渲染器和 MCP 管理器都修改同一个对象，bug 将无法追踪。
2. **如何响应变化？** 当用户切换权限模式时，多个子系统需要知道（元数据同步、配置持久化、UI 刷新）。轮询浪费资源；回调脆弱易碎。
3. **什么该发送给 API？** 内部消息列表包含系统消息、进度更新、墓碑标记和仅用于显示的虚拟消息。Anthropic API 期望的是干净的 `user`/`assistant` 交替格式。在每次调用前，必须对消息流进行过滤、合并和修复。

---

## 解决方案

Claude Code 将所有内容集中到一个由 Zustand 风格的 `Store<T>` 管理的 `AppState`（约 100 个字段）中。Store 强制使用函数式更新，并在状态变化时触发 `onChange` 反应器来处理副作用。消息使用区分联合类型系统。标准化管道将内部消息数组转换为干净的 API 格式。

```
+-----------------------------+
|        Store<AppState>      |
|  .getState()                |
|  .setState(updater)  -----> onChange(new, old)
|  .subscribe(listener)       |        |
+--------+--------------------+        v
         |                    +------------------+
         |                    | Side-effect      |
         v                    | reactor:         |
+--------+---------+          |  - persist model |
| AppState (~100)  |          |  - sync perms    |
|                  |          |  - update config  |
| [immutable]      |          +------------------+
|  settings        |
|  verbose         |
|  permissions     |
|  thinking        |
|                  |
| [mutable]        |
|  tasks {}        |
|  mcp {}          |
|  plugins {}      |
|  team_context    |
|  inbox           |
+------------------+
```

消息系统与 Store 并列：

```
  Internal messages         normalizeMessagesForAPI()         API messages
+--------------------+    +----------------------------+    +----------------+
| user               |    | 1. Filter: drop system,    |    | {role: "user"} |
| assistant          | -> |    progress, tombstone,     | -> | {role: "asst"} |
| system (14 subtypes)|   |    virtual, attachment      |    | {role: "user"} |
| progress           |    | 2. Transform: strip think  |    | {role: "asst"} |
| tombstone          |    | 3. Merge: adjacent same    |    +----------------+
| tool_use_summary   |    | 4. Pair: tool_use/result   |
| attachment         |    +----------------------------+
+--------------------+
```

---

## 工作原理

### 1. Store -- 带恒等性短路的函数式更新

Store 非常精简（真实源码约 35 行），但强制执行一个关键不变量：`setState` 接受一个**更新函数**，而不是原始值。这与 React 的 `useState` 更新器模式一致，确保了原子性状态转换：

```python
class Store(Generic[T]):
    def __init__(
        self,
        initial_state: T,
        on_change: Callable[[T, T], None] | None = None,
    ):
        self._state: T = initial_state
        self._listeners: list[Callable[[], None]] = []
        self._on_change = on_change

    def get_state(self) -> T:
        return self._state

    def set_state(self, updater: Callable[[T], T]) -> None:
        prev = self._state
        next_state = updater(prev)
        if next_state is prev:  # identity check (like Object.is)
            return
        self._state = next_state
        if self._on_change:
            self._on_change(next_state, prev)
        for listener in list(self._listeners):
            listener()
```

恒等性检查（`next_state is prev`）是性能诀窍。如果更新函数返回的是同一个对象，则不会触发任何监听器，不会运行任何副作用。这可以在实际没有变化时防止雪崩式的重渲染。

来源: `src/state/store.ts`

### 2. AppState -- 不可变外壳，可变逃逸舱

真实的 `AppState` 大约有 100 个字段，分为两个分区。不可变分区使用 TypeScript 的 `DeepImmutable<T>` 包装器。可变分区涵盖了自行管理复杂生命周期的子系统（MCP、tasks、plugins）：

```python
@dataclass
class AppState:
    # --- Immutable partition ---
    settings: dict = field(default_factory=dict)
    verbose: bool = False
    main_loop_model: str | None = None
    tool_permission_mode: str = "default"
    thinking_enabled: bool = True
    fast_mode: bool = False
    repl_bridge_enabled: bool = False
    repl_bridge_connected: bool = False

    # --- Mutable partition ---
    tasks: dict = field(default_factory=dict)
    mcp: dict = field(default_factory=lambda: {
        "clients": [], "tools": [], "commands": [],
        "resources": {}, "plugin_reconnect_key": 0,
    })
    plugins: dict = field(default_factory=lambda: {
        "enabled": [], "disabled": [], "commands": [],
        "errors": [], "needs_refresh": False,
    })
    team_context: dict | None = None
    inbox: dict = field(default_factory=lambda: {"messages": []})
```

来源: `src/state/AppStateStore.ts`

### 3. onChange 反应器

当状态发生转换时，反应器检查发生了什么变化，并触发有针对性的副作用。这用一个单一的、可审计的函数替代了分散的 `useEffect` hook：

```python
def create_on_change_reactor(log: SideEffectLog) -> Callable[[AppState, AppState], None]:
    def on_change(new_state: AppState, old_state: AppState) -> None:
        if new_state.tool_permission_mode != old_state.tool_permission_mode:
            log.entries.append(
                f"[reactor] permission mode: "
                f"{old_state.tool_permission_mode} -> {new_state.tool_permission_mode}"
            )
        if new_state.main_loop_model != old_state.main_loop_model:
            log.entries.append(
                f"[reactor] model: "
                f"{old_state.main_loop_model} -> {new_state.main_loop_model}"
            )
        if new_state.verbose != old_state.verbose:
            log.entries.append(
                f"[reactor] verbose: {old_state.verbose} -> {new_state.verbose}"
            )
    return on_change
```

在真实源码中，这些反应包括：将模型选择持久化到用户设置、将权限模式同步到 CCR 元数据、在设置变化时清除认证缓存，以及重新应用环境变量。

来源: `src/state/onChangeAppState.ts`

### 4. 消息类型 -- 8+ 种区分变体

内部消息数组不是简单的 `{role, content}` 字典列表。它是一个包含至少 8 种类型的区分联合，每种类型携带不同的元数据：

```python
class MessageType(str, Enum):
    USER = "user"
    ASSISTANT = "assistant"
    SYSTEM = "system"
    ATTACHMENT = "attachment"
    PROGRESS = "progress"
    TOMBSTONE = "tombstone"
    TOOL_USE_SUMMARY = "tool_use_summary"

class SystemSubtype(str, Enum):
    INFORMATIONAL = "informational"
    API_ERROR = "api_error"
    TURN_DURATION = "turn_duration"
    COMPACT_BOUNDARY = "compact_boundary"
    MICROCOMPACT_BOUNDARY = "microcompact_boundary"
    MEMORY_SAVED = "memory_saved"
    STOP_HOOK_SUMMARY = "stop_hook_summary"
    LOCAL_COMMAND = "local_command"
    PERMISSION_RETRY = "permission_retry"
    SCHEDULED_TASK_FIRE = "scheduled_task_fire"
    AWAY_SUMMARY = "away_summary"
    BRIDGE_STATUS = "bridge_status"
    AGENTS_KILLED = "agents_killed"
    API_METRICS = "api_metrics"
```

仅 `system` 类型就有 **14 种子类型**。每种子类型在标准化过程中的处理方式不同 -- `local_command` 被转换为 user 消息，`compact_boundary` 被过滤掉，`turn_duration` 仅用于显示。

来源: `src/types/message.ts`

### 5. normalizeMessagesForAPI -- 四阶段管道

这是连接丰富的内部消息流与 Anthropic API 严格格式要求之间的关键桥梁。它运行四个阶段：

```python
def normalize_messages_for_api(messages: list[Message]) -> list[dict]:
    # Phase 1: Filter -- remove system/progress/tombstone/attachment/virtual
    api_messages: list[Message] = []
    for msg in messages:
        if msg.type == MessageType.SYSTEM:
            if msg.subtype == SystemSubtype.LOCAL_COMMAND:
                # Convert local_command to user message
                api_messages.append(Message(
                    type=MessageType.USER, content=msg.content,
                    is_meta=True, uuid=msg.uuid,
                ))
            continue
        if msg.type in (MessageType.PROGRESS, MessageType.TOMBSTONE,
                        MessageType.TOOL_USE_SUMMARY, MessageType.ATTACHMENT):
            continue
        if msg.is_virtual:
            continue
        api_messages.append(msg)

    # Phase 3: Transform -- strip thinking blocks, handle empty content
    result: list[dict] = []
    for msg in api_messages:
        role = "user" if msg.type == MessageType.USER else "assistant"
        content = msg.content
        if role == "assistant":
            if isinstance(content, list):
                filtered = [b for b in content
                           if b.get("type") in ("text", "tool_use")]
                content = filtered if filtered else NO_CONTENT_SENTINEL
            elif not content:
                content = NO_CONTENT_SENTINEL
        result.append({"role": role, "content": content})

    # Phase 4: Merge adjacent same-role messages
    merged: list[dict] = []
    for msg_dict in result:
        if merged and merged[-1]["role"] == msg_dict["role"]:
            # Combine content (handles str+str, list+list, mixed)
            ...
        else:
            merged.append(msg_dict)

    # Ensure first message is user role
    if merged and merged[0]["role"] != "user":
        merged.insert(0, {"role": "user", "content": "[system initialized]"})

    return merged
```

标准化完成后，`ensureToolResultPairing` 作为安全网运行：它剥离引用了不存在的 `tool_use` ID 的孤立 `tool_result` 块，并为未配对的 `tool_use` 块插入合成的错误结果。

来源: `src/utils/messages.ts`

### 6. Selectors -- 派生状态

Claude Code 不会存储派生数据，而是通过 selectors 按需计算：

```python
def get_active_agent_for_input(state: AppState) -> dict:
    """
    Returns discriminated union:
    - { type: 'leader' }
    - { type: 'viewed', task }
    - { type: 'named_agent', task }
    """
    viewed = get_viewed_teammate_task(state)
    if viewed:
        return {"type": "viewed", "task": viewed}
    return {"type": "leader"}
```

这个 selector 决定用户输入被路由到哪里 -- 主 agent 还是正在查看的队友。

来源: `src/state/selectors.ts`

---

## 变化对比

| 组件 | 之前 | 之后 |
|------|------|------|
| 状态位置 | 分散在各个模块中 | 单一 `Store<AppState>`，约 100 个字段 |
| 更新模式 | 直接修改 | 函数式更新器: `setState(prev => ...)` |
| 变化检测 | 手动 diff 或无 | 恒等性检查 (`Object.is`) + `onChange` 反应器 |
| 副作用 | 到处都是临时回调 | 集中的反应器，比较新旧状态 |
| 消息类型 | 仅 `user` 和 `assistant` | 8+ 种类型，包含 14 种 system 子类型 |
| API 准备 | 消息原样发送 | 四阶段标准化: 过滤、转换、合并、配对 |
| 空响应 | 崩溃或 undefined | 哨兵值: `"[no content - assistant responded with only tool calls]"` |
| 角色交替 | 听天由命 | 强制执行: 合并相邻同角色消息，必要时注入合成 `user` |

---

## 试一试

```bash
# Run the state management demo
python agents/s12_state_management.py
```

输出中需要关注的要点：

1. **Store 生命周期** -- 函数式更新触发 `onChange` 反应器，恒等性短路跳过无效更新
2. **渲染计数** -- subscribe/unsubscribe 正确工作；`unsub()` 后渲染停止
3. **消息多样性** -- 14 条消息涵盖 7+ 种类型
4. **标准化** -- 14 条内部消息压缩为约 6 条干净的 API 消息
5. **角色交替** -- 最终输出严格交替 `user`/`assistant`，以 `user` 开头
6. **Selectors** -- 当选中一个队友任务时，输入路由从 `leader` 变为 `viewed`

尝试添加一个新的 system 子类型（例如 `RATE_LIMIT_WARNING`），并追踪它如何在标准化管道中流转 -- 你会看到它被自动过滤掉。
