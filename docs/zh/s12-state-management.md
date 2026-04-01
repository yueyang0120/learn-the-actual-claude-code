# s12: State Management

s01 > s02 > s03 > s04 > s05 | s06 > s07 > s08 > s09 > s10 | s11 > **[ s12 ]** s13 > s14

> "State is easy. State that 14 subsystems can read and write without corruption -- that is hard."

## 问题

一个 CLI agent 在运行过程中会不断积累状态 -- 权限、MCP 连接、工具调用、团队协调，全都有。把这些塞进散落各处的全局变量，很快就崩了：谁拥有数据搞不清楚，变化没法响应，而且内部消息列表塞满了系统元数据，这些东西绝对不能发给 API。

## 解决方案

Claude Code 把所有状态集中到一个 `Store<AppState>` 里，用函数式更新，配合 `onChange` 反应器处理副作用。每次调用 API 之前，一条标准化管道把内部消息流清洗成干净的 API 格式。

```
  +---------------------------+
  |     Store<AppState>       |
  |  .getState()              |
  |  .setState(fn) ---------> onChange(new, old)
  |  .subscribe(listener)     |       |
  +---------+-----------------+       v
            |               +-----------------+
            v               | Side-effect     |
  +---------+---------+     | reactor:        |
  | AppState (~100)   |     |  persist model  |
  |  settings         |     |  sync perms     |
  |  permissions      |     |  update config  |
  |  mcp, tasks ...   |     +-----------------+
  +-------------------+

  Internal messages         normalize()          API messages
  +-----------------+    +---------------+    +---------------+
  | user            |    | 1. Filter     |    | {role:"user"} |
  | assistant       | -> | 2. Transform  | -> | {role:"asst"} |
  | system (14 sub) |    | 3. Merge      |    | {role:"user"} |
  | progress        |    | 4. Pair       |    +---------------+
  | tombstone       |    +---------------+
  +-----------------+
```

## 工作原理

### 1. Store

Store 很小，但有一个关键约束：`setState` 接受的是更新函数，不是原始值。如果更新函数返回的是同一个对象，所有监听器都不会触发。

```python
# agents/s12_state_management.py (simplified)

class Store(Generic[T]):
    def __init__(self, initial_state, on_change=None):
        self._state = initial_state
        self._listeners = []
        self._on_change = on_change

    def set_state(self, updater):
        prev = self._state
        next_state = updater(prev)
        if next_state is prev:       # identity bailout
            return
        self._state = next_state
        if self._on_change:
            self._on_change(next_state, prev)
        for fn in self._listeners:
            fn()
```

### 2. onChange 反应器

状态变化时，反应器检查哪些字段改了，然后触发对应的副作用。用一个集中的函数替代散落各处的回调。

```python
def on_change(new_state, old_state):
    if new_state.tool_permission_mode != old_state.tool_permission_mode:
        sync_permissions(new_state.tool_permission_mode)
    if new_state.main_loop_model != old_state.main_loop_model:
        persist_model_choice(new_state.main_loop_model)
```

### 3. 消息类型

内部消息数组不是简单的 `{role, content}` 列表。它是一个区分联合类型，有 8+ 种类型和 14 种 system 子类型 -- 进度更新、压缩边界、tombstone、本地命令等等。

```python
class MessageType(str, Enum):
    USER            = "user"
    ASSISTANT       = "assistant"
    SYSTEM          = "system"       # 14 subtypes
    PROGRESS        = "progress"
    TOMBSTONE       = "tombstone"
    TOOL_USE_SUMMARY= "tool_use_summary"
    ATTACHMENT      = "attachment"
```

### 4. 标准化管道

每次调 API 之前，四阶段管道清洗消息流：过滤掉 system/progress/tombstone 消息，把空内容替换成哨兵值，合并相邻同角色消息，确保 tool_use/tool_result 配对。

```python
def normalize_messages_for_api(messages):
    # Phase 1: filter -- keep user + assistant, convert local_command
    # Phase 2: transform -- strip thinking, add sentinel for empty
    # Phase 3: merge -- combine adjacent same-role messages
    # Phase 4: ensure first message is user role
    if merged[0]["role"] != "user":
        merged.insert(0, {"role": "user", "content": "[system initialized]"})
    return merged
```

## 变更内容

| 组件 | s11 | s12 |
|------|-----|-----|
| 状态位置 | 散落在各个模块 | 单一 `Store<AppState>`（约 100 个字段） |
| 更新模式 | 直接修改 | 函数式更新: `setState(prev => ...)` |
| 变化检测 | 无 | 恒等性检查 + `onChange` 反应器 |
| 副作用 | 零散回调 | 集中的反应器，比较新旧状态 |
| 消息类型 | 只有 user/assistant | 8+ 种类型，14 种 system 子类型 |
| API 准备 | 消息原样发送 | 四阶段标准化管道 |
| 角色交替 | 听天由命 | 强制: 合并 + 注入合成 user 消息 |

## 试一试

```bash
cd learn-the-actual-claude-code
python agents/s12_state_management.py
```

留意这些输出：

- 函数式更新触发 `onChange` 反应器；恒等性检查跳过无效更新
- 14 条内部消息经标准化后变成约 6 条干净的 API 消息
- 最终输出严格交替 user/assistant，以 user 开头
- Selector 根据状态把输入路由到 leader 或正在查看的队友

试着加一个新的 system 子类型，追踪标准化管道是怎么自动把它过滤掉的。
