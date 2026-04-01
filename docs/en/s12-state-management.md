# Session 12 -- State Management

s01 > s02 > s03 > s04 > s05 | s06 > s07 > s08 > s09 > s10 | s11 > **s12** > s13 > s14

> "State is easy. State that 14 subsystems can read and write without corrupting each other -- that is hard."
>
> *Harness layer: Claude Code uses a Zustand-inspired Store with functional updates, an onChange reactor for side effects, 8+ discriminated message types, and a normalization pipeline that strips internal messages before every API call.*

---

## Problem

A CLI agent that handles tool calls, compaction, permissions, MCP connections, team coordination, and task management accumulates a lot of state. The naive approach -- scattered global variables or a flat dictionary -- breaks down fast:

1. **Who owns the data?** If the permission system, the UI renderer, and the MCP manager all mutate the same object, bugs become untraceable.
2. **How do you react to changes?** When the user switches permission mode, several subsystems need to know (metadata sync, config persistence, UI refresh). Polling is wasteful; callbacks are fragile.
3. **What goes to the API?** The internal message list contains system messages, progress updates, tombstones, and display-only virtual messages. The Anthropic API expects a clean `user`/`assistant` alternation. Something has to filter, merge, and repair the message stream before every call.

---

## Solution

Claude Code centralizes everything into a single `AppState` (~100 fields) managed by a Zustand-style `Store<T>`. The store enforces functional updates and fires an `onChange` reactor for side effects. Messages use a discriminated-union type system. A normalization pipeline transforms the internal message array into clean API format.

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

The message system sits alongside the store:

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

## How It Works

### 1. The Store -- Functional Updates with Identity Bailout

The store is tiny (~35 LOC in the real source) but enforces a critical invariant: `setState` takes an **updater function**, never a raw value. This mirrors React's `useState` updater pattern and ensures atomic transitions:

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

The identity check (`next_state is prev`) is the performance trick. If the updater returns the same object, no listeners fire, no side effects run. This prevents avalanche re-renders when nothing actually changed.

Source: `src/state/store.ts`

### 2. AppState -- Immutable Shell, Mutable Escape Hatch

The real `AppState` has approximately 100 fields split into two partitions. The immutable partition uses TypeScript's `DeepImmutable<T>` wrapper. The mutable partition covers subsystems that manage their own complex lifecycle (MCP, tasks, plugins):

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

Source: `src/state/AppStateStore.ts`

### 3. The onChange Reactor

When state transitions, the reactor inspects what changed and fires targeted side effects. This replaces scattered `useEffect` hooks with a single, auditable function:

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

In the real source, these reactions include: persisting the model choice to user settings, syncing permission mode to CCR metadata, clearing auth caches when settings change, and reapplying environment variables.

Source: `src/state/onChangeAppState.ts`

### 4. Message Types -- 8+ Discriminated Variants

The internal message array is not a flat list of `{role, content}` dicts. It is a discriminated union with at least 8 types, each carrying different metadata:

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

The `system` type alone has **14 subtypes**. Each subtype is handled differently during normalization -- `local_command` gets converted to a user message, `compact_boundary` gets filtered out, `turn_duration` is display-only.

Source: `src/types/message.ts`

### 5. normalizeMessagesForAPI -- The Four-Phase Pipeline

This is the critical bridge between the rich internal message stream and the Anthropic API's strict format requirements. It runs four phases:

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

After normalization, `ensureToolResultPairing` runs as a safety net: it strips orphaned `tool_result` blocks that reference non-existent `tool_use` IDs, and inserts synthetic error results for unpaired `tool_use` blocks.

Source: `src/utils/messages.ts`

### 6. Selectors -- Derived State

Rather than storing derived data, Claude Code computes it on demand through selectors:

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

This selector determines where user input gets routed -- to the main agent or to a teammate being viewed.

Source: `src/state/selectors.ts`

---

## What Changed

| Component | Before | After |
|-----------|--------|-------|
| State location | Scattered across modules | Single `Store<AppState>` with ~100 fields |
| Update pattern | Direct mutation | Functional updater: `setState(prev => ...)` |
| Change detection | Manual diffing or none | Identity check (`Object.is`) + `onChange` reactor |
| Side effects | Ad-hoc callbacks everywhere | Centralized reactor comparing old vs. new state |
| Message types | Just `user` and `assistant` | 8+ types with 14 system subtypes |
| API preparation | Messages sent as-is | 4-phase normalization: filter, transform, merge, pair |
| Empty responses | Crash or undefined | Sentinel: `"[no content - assistant responded with only tool calls]"` |
| Role alternation | Hope for the best | Enforced: merge adjacent same-role, inject synthetic `user` if needed |

---

## Try It

```bash
# Run the state management demo
python agents/s12_state_management.py
```

What to watch for in the output:

1. **Store lifecycle** -- functional updates fire the `onChange` reactor, identity bailout skips no-op updates
2. **Render counting** -- subscribe/unsubscribe works correctly; renders stop after `unsub()`
3. **Message diversity** -- 14 messages created across 7+ types
4. **Normalization** -- 14 internal messages collapse to ~6 clean API messages
5. **Role alternation** -- the final output strictly alternates `user`/`assistant`, starting with `user`
6. **Selectors** -- input routing changes from `leader` to `viewed` when a teammate task is selected

Try adding a new system subtype (e.g., `RATE_LIMIT_WARNING`) and trace how it flows through the normalization pipeline -- you will see it get filtered out automatically.
