# Session 12: State Management

## Overview

Claude Code uses a centralized, Zustand-inspired state store to coordinate its
entire runtime: UI rendering, tool permissions, MCP connections, plugin
lifecycle, task tracking, swarm coordination, bridge status, and more. The
`AppState` type contains roughly 100 fields partitioned into a `DeepImmutable`
read-only shell and a mutable escape hatch for types that carry functions or
need direct mutation (tasks, MCP clients, Maps/Sets). A custom 35-line
`Store<T>` provides `getState`, `setState`, and `subscribe` -- used by both
React components (via `useSyncExternalStore`) and non-React tool code.

Alongside the store, Claude Code defines a rich **message type hierarchy** with
8+ discriminated types (UserMessage, AssistantMessage, SystemMessage with 14+
subtypes, AttachmentMessage, ProgressMessage, TombstoneMessage,
ToolUseSummaryMessage, StreamEventMessage). The critical
`normalizeMessagesForAPI()` function strips internal-only messages, enforces
user/assistant role alternation, merges adjacent same-role messages, and ensures
tool_use/tool_result pairing before sending to the Anthropic API.

## Key Source Files

| File | Purpose |
|------|---------|
| `src/state/AppStateStore.ts` | Canonical `AppState` type (~100 fields), `getDefaultAppState()` factory |
| `src/state/store.ts` | Generic `Store<T>` -- 35 LOC, `getState`/`setState`/`subscribe` |
| `src/state/AppState.tsx` | React bindings: `AppStateProvider`, `useAppState(selector)`, `useSetAppState()` |
| `src/state/selectors.ts` | Derived-state selectors: `getViewedTeammateTask()`, `getActiveAgentForInput()` |
| `src/state/onChangeAppState.ts` | Side-effect reactor: syncs mode changes to CCR, persists model/view to config |
| `src/types/message.ts` | Full message type hierarchy (UserMessage, AssistantMessage, 14+ SystemMessage subtypes) |
| `src/utils/messages.ts` | `normalizeMessagesForAPI()`, `normalizeMessages()`, message creation helpers |
| `src/bootstrap/state.ts` | Separate global `State` singleton for process-level counters (cost, timing, cwd) |

## Architecture

```
+---------------------------------------------------------------------+
|                         AppState                                     |
|  DeepImmutable<{                      & {  (mutable escape hatch)   |
|    settings, verbose,                     tasks: {[id]: TaskState}   |
|    mainLoopModel,                         agentNameRegistry: Map     |
|    toolPermissionContext,                  mcp: {clients,tools,...}   |
|    replBridge* (8 fields),                plugins: {enabled,...}     |
|    kairosEnabled,                         teamContext?: {...}        |
|    footerSelection,                       inbox: {messages:[]}       |
|    ...~40 more                            replContext?: {vmContext}   |
|  }>                                       speculation: {...}         |
|                                           ...~20 more               |
+---------------------------------------------------------------------+
        ^                |
        |   get/set      v
  +-------------+   +----------+   +---------------------+
  | React UI    |<->| Store<T> |-->| onChangeAppState()  |
  | (Ink/hooks) |   | 35 LOC   |   | side-effect reactor |
  +-------------+   +----------+   +---------------------+
        |
  useSyncExternalStore
  (selector-based slicing)
```

## Message Type Hierarchy

```
Message  (discriminated union on `type`)
  |-- UserMessage        (type: "user")
  |     |-- human turn   (content: string, no toolUseResult, not isMeta)
  |     +-- tool result  (content: ToolResultBlockParam[], toolUseResult: id)
  |
  |-- AssistantMessage   (type: "assistant")
  |     content: (TextBlock | ToolUseBlock | ThinkingBlock | RedactedThinkingBlock)[]
  |     isApiErrorMessage?, isVirtual?, requestId?, origin?
  |
  |-- SystemMessage      (type: "system")  -- 14+ subtypes:
  |     |-- SystemInformationalMessage     (subtype: "informational")
  |     |-- SystemAPIErrorMessage          (subtype: "api_error")
  |     |-- SystemTurnDurationMessage      (subtype: "turn_duration")
  |     |-- SystemCompactBoundaryMessage   (subtype: "compact_boundary")
  |     |-- SystemMicrocompactBoundaryMessage
  |     |-- SystemMemorySavedMessage       (subtype: "memory_saved")
  |     |-- SystemStopHookSummaryMessage
  |     |-- SystemLocalCommandMessage      (subtype: "local_command")
  |     |-- SystemPermissionRetryMessage
  |     |-- SystemScheduledTaskFireMessage
  |     |-- SystemAwaySummaryMessage
  |     |-- SystemBridgeStatusMessage
  |     |-- SystemAgentsKilledMessage
  |     +-- SystemApiMetricsMessage
  |
  |-- AttachmentMessage  (type: "attachment")  -- files, hooks, teammate msgs
  |-- ProgressMessage    (type: "progress")    -- real-time tool progress
  |-- TombstoneMessage   (type: "tombstone")   -- deleted message placeholder
  +-- ToolUseSummaryMessage (type: "tool_use_summary") -- collapsed tool chain
```

## What shareAI-lab (and Most Clones) Miss

Open-source Claude Code replicas have **no centralized state management**.
Typical patterns in clones:

- **No store**: state is scattered across module-level variables, React useState
  hooks, and function closures with no single source of truth
- **No immutability strategy**: no DeepImmutable, no functional updaters, no
  Object.is change detection -- leading to unnecessary re-renders and stale reads
- **No message type hierarchy**: clones typically use a flat `{role, content}`
  shape matching the API directly, losing the internal discriminants (isMeta,
  toolUseResult, subtype, origin) that power compaction, permission tracking,
  and UI rendering
- **No normalizeMessagesForAPI()**: without internal message types, there is no
  need for normalization -- but also no ability to inject system messages,
  progress markers, or compact boundaries into the conversation
- **No side-effect reactor**: changes to permission mode, model selection, or
  view state are not propagated to external systems (CCR, SDK status stream,
  config persistence)
- **No selector-based subscriptions**: React components subscribe to the entire
  state object rather than fine-grained slices

## Learning Objectives

1. **Immutability with escape hatches** -- `DeepImmutable<{...}> & { tasks, ... }`
   makes most state readonly while exempting fields with function types
2. **Message discrimination** -- Multiple message "types" share `role: "user"` at
   the API level; internal types add discriminant fields for UI and control flow
3. **normalizeMessagesForAPI()** -- The critical bridge from rich internal state to
   clean API payloads: filter, merge, pair, alternate
4. **Store pattern** -- Custom 35-line store beats Zustand/Redux for Ink + non-React
   dual-context needs
5. **Side-effect reactor** -- `onChangeAppState()` decouples mutation from effect

## Exercises

1. Add a new SystemMessage subtype to the Python reimplementation and verify
   `normalize_messages_for_api()` filters it out
2. Implement a selector that subscribes to only `mcp.tools` changes and tracks
   how many times it fires vs total state updates
3. Write a test verifying tool_result messages are paired with their tool_use
   messages during normalization (catch orphaned tool_results)
4. Extend the store with an `onChange` callback and implement the model-persistence
   side effect from `onChangeAppState.ts`

## Running the Reimplementation

```bash
cd sessions/s12-state-management
python reimplementation.py
```
