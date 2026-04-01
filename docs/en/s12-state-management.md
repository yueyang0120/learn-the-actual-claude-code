# s12: State Management

s01 > s02 > s03 > s04 > s05 | s06 > s07 > s08 > s09 > s10 | s11 > **[ s12 ]** s13 > s14

> "State is easy. State that 14 subsystems can read and write without corruption -- that is hard."

## Problem

A CLI agent accumulates state across permissions, MCP connections, tool calls, and team coordination. Scattered globals break down fast: nobody knows who owns the data, there is no way to react to changes, and the internal message list is full of system metadata that the API must never see.

## Solution

Claude Code centralizes everything into a single `Store<AppState>` with functional updates and an `onChange` reactor for side effects. A normalization pipeline transforms the rich internal message stream into clean API format before every call.

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

## How It Works

### 1. The Store

The store is small but enforces a key invariant: `setState` takes an updater function, never a raw value. If the updater returns the same object, no listeners fire.

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

### 2. The onChange reactor

When state transitions, the reactor inspects what changed and fires targeted side effects. This replaces scattered callbacks with one auditable function.

```python
def on_change(new_state, old_state):
    if new_state.tool_permission_mode != old_state.tool_permission_mode:
        sync_permissions(new_state.tool_permission_mode)
    if new_state.main_loop_model != old_state.main_loop_model:
        persist_model_choice(new_state.main_loop_model)
```

### 3. Message types

The internal message array is not a flat list of `{role, content}` dicts. It is a discriminated union with 8+ types and 14 system subtypes -- progress updates, compact boundaries, tombstones, local commands, and more.

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

### 4. Normalization pipeline

Before every API call, a four-phase pipeline cleans the message stream: filter out system/progress/tombstone messages, transform empty content into a sentinel, merge adjacent same-role messages, and ensure tool_use/tool_result pairing.

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

## What Changed

| Component | Before (s11) | After (s12) |
|-----------|-------------|-------------|
| State location | Scattered across modules | Single `Store<AppState>` (~100 fields) |
| Update pattern | Direct mutation | Functional updater: `setState(prev => ...)` |
| Change detection | None | Identity check + `onChange` reactor |
| Side effects | Ad-hoc callbacks | Centralized reactor comparing old vs new |
| Message types | Just user/assistant | 8+ types with 14 system subtypes |
| API preparation | Messages sent as-is | 4-phase normalization pipeline |
| Role alternation | Hope for the best | Enforced: merge + inject synthetic user |

## Try It

```bash
cd learn-the-actual-claude-code
python agents/s12_state_management.py
```

Watch for:

- Functional updates fire the `onChange` reactor; identity bailout skips no-ops
- 14 internal messages collapse to ~6 clean API messages after normalization
- Final output strictly alternates user/assistant, starting with user
- Selector routes input to leader vs. viewed teammate based on state

Try adding a new system subtype and trace how the normalization pipeline filters it out automatically.
