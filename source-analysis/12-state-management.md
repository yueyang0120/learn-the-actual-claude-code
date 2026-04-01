# Source Analysis: State Management

## 1. Store<T> -- The Foundation (src/state/store.ts)

The entire reactive state system rests on 35 lines of code:

```typescript
type Listener = () => void
type OnChange<T> = (args: { newState: T; oldState: T }) => void

export type Store<T> = {
  getState: () => T
  setState: (updater: (prev: T) => T) => void
  subscribe: (listener: Listener) => () => void
}

export function createStore<T>(initialState: T, onChange?: OnChange<T>): Store<T> {
  let state = initialState
  const listeners = new Set<Listener>()
  return {
    getState: () => state,
    setState: (updater) => {
      const prev = state
      const next = updater(prev)
      if (Object.is(next, prev)) return   // <-- identity check, skip if unchanged
      state = next
      onChange?.({ newState: next, oldState: prev })
      for (const listener of listeners) listener()
    },
    subscribe: (listener) => {
      listeners.add(listener)
      return () => listeners.delete(listener)
    },
  }
}
```

Key design decisions:

- **Functional updater only**: `setState` takes `(prev) => next`, never a raw
  value. This prevents stale-closure bugs (same pattern as React's `useState`).
- **Identity bailout**: `Object.is(next, prev)` skips notification when the
  updater returns the same reference. Components that spread-and-override
  produce new objects; components that return `prev` unchanged cause zero work.
- **onChange hook**: A single callback fires on every state transition, receiving
  both old and new state. This is where `onChangeAppState.ts` plugs in.
- **Set-based listeners**: Listeners are stored in a `Set<Listener>`, ensuring
  O(1) add/remove and no duplicate registrations. The unsubscribe function
  returned by `subscribe` simply calls `listeners.delete`.

### Why Not Zustand?

Claude Code's architecture requires the store to work in three contexts:
1. **React components** (Ink terminal UI) via `useSyncExternalStore`
2. **Non-React tool execution code** that calls `getState()`/`setState()` directly
3. **Headless/SDK mode** where no React tree exists

Zustand could satisfy all three, but the custom store adds zero dependencies,
has no middleware overhead, and integrates cleanly with Ink's render cycle.
The 35 lines are simpler to audit than a library.

## 2. AppState Type (src/state/AppStateStore.ts)

### The Immutable/Mutable Split

The `AppState` type uses an intersection to create two partitions:

```typescript
export type AppState = DeepImmutable<{
  // --- Immutable partition: ~50 scalar/config fields ---
  settings: SettingsJson
  verbose: boolean
  mainLoopModel: ModelSetting
  mainLoopModelForSession: ModelSetting
  toolPermissionContext: ToolPermissionContext
  agent: string | undefined
  kairosEnabled: boolean
  // ... UI state fields ...
  expandedView: 'none' | 'tasks' | 'teammates'
  footerSelection: FooterItem | null
  coordinatorTaskIndex: number
  // ... bridge state (8 fields) ...
  replBridgeEnabled: boolean
  replBridgeConnected: boolean
  replBridgeSessionActive: boolean
  replBridgeConnectUrl: string | undefined
  // ... etc ...
}> & {
  // --- Mutable partition: fields with functions, Maps, Sets ---
  tasks: { [taskId: string]: TaskState }        // contains AbortController
  agentNameRegistry: Map<string, AgentId>       // Map needs .set()
  mcp: { clients, tools, commands, resources, pluginReconnectKey }
  plugins: { enabled, disabled, commands, errors, installationStatus, needsRefresh }
  fileHistory: FileHistoryState
  attribution: AttributionState
  todos: { [agentId: string]: TodoList }
  teamContext?: { teamName, leadAgentId, teammates: {...} }
  inbox: { messages: Array<{id, from, text, status, ...}> }
  speculation: SpeculationState                 // contains abort(), mutable refs
  replContext?: { vmContext, registeredTools: Map, console }
  // ... etc ...
}
```

`DeepImmutable<T>` recursively makes all properties `readonly`. The `& { ... }`
intersection explicitly opts out fields that contain:
- **Functions** (`AbortController.abort`, tool handlers)
- **Maps and Sets** that need `.set()` / `.add()` mutation
- **Mutable refs** (`{ current: ... }` patterns in speculation)

### The Default Factory

`getDefaultAppState()` initializes all ~100 fields. Notable:

- `toolPermissionContext.mode` is `'plan'` when running as a teammate with
  `isPlanModeRequired()`, otherwise `'default'`
- `thinkingEnabled` calls `shouldEnableThinkingByDefault()` (GrowthBook gate)
- `promptSuggestionEnabled` calls `shouldEnablePromptSuggestion()`
- `sessionHooks` is a `Map` (mutable, needs `.set()`)
- `activeOverlays` is a `Set<string>` (for Escape key coordination)
- `speculation` starts as `{ status: 'idle' }` singleton

### State Partitions

| Partition | Example Fields | Mutability | Size |
|-----------|---------------|-----------|------|
| Core config | `settings`, `verbose`, `mainLoopModel` | Immutable | ~10 |
| Permissions | `toolPermissionContext` (mode, rules, allowlist) | Immutable | 1 (nested) |
| UI | `expandedView`, `footerSelection`, `coordinatorTaskIndex` | Immutable | ~8 |
| Bridge | `replBridge*` (8 fields: enabled, connected, url, ...) | Immutable | ~12 |
| MCP | `mcp.clients`, `mcp.tools`, `mcp.resources` | Mutable | 1 (nested) |
| Plugins | `plugins.enabled/disabled/errors/installationStatus` | Mutable | 1 (nested) |
| Tasks | `tasks` map, `foregroundedTaskId`, `viewingAgentTaskId` | Mutable | ~3 |
| Swarm | `teamContext`, `inbox`, `agentNameRegistry` | Mutable | ~3 |
| Speculation | `speculation`, `speculationSessionTimeSavedMs`, `promptSuggestion` | Mutable | ~3 |
| Features | `kairosEnabled`, `thinkingEnabled`, `fastMode`, `effortValue` | Mixed | ~6 |

## 3. React Integration (src/state/AppState.tsx)

### AppStateProvider

Creates the store once (via `useState` initializer) and provides it via React
context. The store reference never changes, so the Provider never triggers
re-renders. All reactivity flows through `useSyncExternalStore`.

### useAppState(selector)

```typescript
export function useAppState<T>(selector: (state: AppState) => T): T {
  const store = useAppStore()
  const get = () => selector(store.getState())
  return useSyncExternalStore(store.subscribe, get, get)
}
```

Components subscribe to fine-grained slices:
```typescript
const verbose = useAppState(s => s.verbose)         // boolean
const model = useAppState(s => s.mainLoopModel)      // ModelSetting
const suggestion = useAppState(s => s.promptSuggestion) // object ref
```

The selector must return a stable reference (existing sub-object) or a primitive.
Returning a new object would cause infinite re-renders because `Object.is`
always sees new objects as changed.

### useSetAppState()

Returns `store.setState` directly -- a stable reference that never changes.
Components that only write state never re-render from state changes.

### useAppStateMaybeOutsideOfProvider(selector)

A defensive variant that returns `undefined` if called outside the provider
tree. Uses a `NOOP_SUBSCRIBE` stub to satisfy `useSyncExternalStore` when
no store exists.

## 4. Side-Effect Reactor (src/state/onChangeAppState.ts)

The `onChangeAppState` function fires on every state transition (wired via the
`onChange` parameter to `createStore`). It decouples state mutation from effects:

### Permission Mode Sync

```typescript
const prevMode = oldState.toolPermissionContext.mode
const newMode = newState.toolPermissionContext.mode
if (prevMode !== newMode) {
  const prevExternal = toExternalPermissionMode(prevMode)
  const newExternal = toExternalPermissionMode(newMode)
  if (prevExternal !== newExternal) {
    notifySessionMetadataChanged({ permission_mode: newExternal, ... })
  }
  notifyPermissionModeChanged(newMode)
}
```

This is the **single choke point** for mode sync. Before this existed, mode
changes were only relayed by 2 of 8+ mutation paths, leaving CCR and the
web UI out of sync.

### Model Persistence

When `mainLoopModel` changes, the reactor writes to user settings and updates
the bootstrap module-level override:

```typescript
if (newState.mainLoopModel !== oldState.mainLoopModel) {
  updateSettingsForSource('userSettings', { model: newState.mainLoopModel })
  setMainLoopModelOverride(newState.mainLoopModel)
}
```

### View State Persistence

`expandedView` changes are persisted to global config for cross-session
sticky state (tasks panel visible, teammates panel visible).

### Settings Change Effects

When `settings` change, the reactor clears auth caches (API key helper, AWS/GCP
credentials) and re-applies environment variables from `settings.env`.

## 5. Selectors (src/state/selectors.ts)

Derived state kept pure and co-located:

### getViewedTeammateTask

Returns the `InProcessTeammateTaskState` if `viewingAgentTaskId` is set and
points to an in-process teammate. Returns `undefined` otherwise.

### getActiveAgentForInput

Discriminated union return type:
- `{ type: 'leader' }` -- input goes to the main conversation
- `{ type: 'viewed', task }` -- input goes to a viewed in-process teammate
- `{ type: 'named_agent', task }` -- input goes to a named local agent

Used by input routing to direct user messages to the correct agent.

## 6. Message Type Hierarchy (src/types/message.ts)

### Top-Level Union

```typescript
export type Message =
  | UserMessage          // type: "user"
  | AssistantMessage     // type: "assistant"
  | SystemMessage        // type: "system" (14+ subtypes)
  | AttachmentMessage    // type: "attachment"
  | ProgressMessage      // type: "progress"
  | TombstoneMessage     // type: "tombstone"
  | ToolUseSummaryMessage // type: "tool_use_summary"
```

All messages share a common shape: `{ type, uuid: UUID, timestamp: string }`.

### UserMessage

```typescript
type UserMessage = {
  type: 'user'
  message: { role: 'user', content: string | ContentBlockParam[] }
  uuid: UUID
  timestamp: string
  isMeta?: true                        // system-injected, not human-authored
  isVisibleInTranscriptOnly?: true     // display only, not sent to API
  isVirtual?: true                     // display-only (e.g. REPL inner calls)
  isCompactSummary?: true              // result of compaction
  toolUseResult?: unknown              // present on tool_result messages
  mcpMeta?: { _meta?, structuredContent? }
  sourceToolAssistantUUID?: UUID       // links tool_result to its tool_use
  permissionMode?: PermissionMode      // for rewind restoration
  origin?: MessageOrigin               // provenance: undefined = human keyboard
  summarizeMetadata?: { messagesSummarized, userContext?, direction? }
  imagePasteIds?: number[]
}
```

Two roles share `type: "user"`:
1. **Human turns**: `content` is a string, `toolUseResult` is undefined
2. **Tool results**: `content` is `ToolResultBlockParam[]`, `toolUseResult` is set

The `isHumanTurn()` predicate distinguishes them:
`type === 'user' && !isMeta && toolUseResult === undefined`

### AssistantMessage

```typescript
type AssistantMessage = {
  type: 'assistant'
  message: {
    content: (TextBlock | ToolUseBlock | ThinkingBlock | RedactedThinkingBlock)[]
    usage?: Usage
    stop_reason?: string
    model?: string
    context_management?: object | null
  }
  uuid: UUID
  timestamp: string
  isMeta?: true
  isVirtual?: true
  isApiErrorMessage?: true
  requestId?: string
  origin?: MessageOrigin
  error?: { type, message }
  apiError?: { status, type, message }
}
```

### System Message Subtypes (14+)

Each subtype is a separate TypeScript type with a discriminant `subtype` field:

| Type Name | `subtype` Value | Key Fields |
|-----------|----------------|------------|
| `SystemInformationalMessage` | `"informational"` | `level`, `content`, `toolUseID?` |
| `SystemAPIErrorMessage` | `"api_error"` | `error`, `retryAttempt`, `maxRetries`, `retryInMs` |
| `SystemTurnDurationMessage` | `"turn_duration"` | `durationMs` |
| `SystemCompactBoundaryMessage` | `"compact_boundary"` | `compactMetadata` |
| `SystemMicrocompactBoundaryMessage` | `"microcompact_boundary"` | `compactMetadata` |
| `SystemMemorySavedMessage` | `"memory_saved"` | `filePath`, `section?` |
| `SystemStopHookSummaryMessage` | `"stop_hook_summary"` | `hookCount`, `hookInfos[]`, `hookErrors[]` |
| `SystemLocalCommandMessage` | `"local_command"` | `content` (stdout/stderr XML) |
| `SystemPermissionRetryMessage` | `"permission_retry"` | `commands[]` |
| `SystemScheduledTaskFireMessage` | `"scheduled_task_fire"` | `content` |
| `SystemAwaySummaryMessage` | `"away_summary"` | summary of away period |
| `SystemBridgeStatusMessage` | `"bridge_status"` | `url`, `upgradeNudge?` |
| `SystemAgentsKilledMessage` | `"agents_killed"` | termination notice |
| `SystemApiMetricsMessage` | `"api_metrics"` | API call metrics |

### Normalization Pipeline

`normalizeMessages()` splits multi-block messages into single-block messages for
UI rendering. Each content block gets its own message with a **deterministic
derived UUID** (via `deriveUUID(parentUUID, index)`) to maintain stable React
keys across re-renders.

### ProgressMessage

```typescript
type ProgressMessage<P extends Progress = Progress> = {
  type: 'progress'
  data: P                  // BashProgress, MCPProgress, AgentToolProgress, etc.
  toolUseID: string
  parentToolUseID: string
  uuid: UUID
  timestamp: string
}
```

Progress messages carry typed data payloads: `BashProgress` has stdout/stderr
lines, `MCPProgress` has server-specific data, `AgentToolProgress` has
sub-agent status. They are **never sent to the API** -- filtered by
`normalizeMessagesForAPI()`.

### TombstoneMessage and ToolUseSummaryMessage

- **TombstoneMessage**: Replaces messages that were deleted during compaction.
  Acts as a placeholder to maintain UUID references.
- **ToolUseSummaryMessage**: Collapsed representation of a tool use chain,
  emitted to the SDK for human-readable progress updates.

## 7. normalizeMessagesForAPI() (src/utils/messages.ts)

The critical bridge between internal message state and the Anthropic API.

### Phase 1: Filter

Removes everything the API cannot accept:
- All SystemMessage subtypes (except `local_command`, converted to user message)
- ProgressMessage
- TombstoneMessage
- AttachmentMessage
- Compact boundaries
- Virtual messages (`isVirtual: true`)
- Synthetic API error messages

### Phase 2: Reorder

Attachments are bubbled up through the message array until they hit a
tool_result or assistant message. This ensures context injected by hooks
and memory attachments appears at the right position for the model.

### Phase 3: Transform

- **Tool inputs**: Normalized via `normalizeToolInputForAPI()` (handles edge cases)
- **Tool results**: `tool_reference` blocks stripped when tool search is not enabled;
  unavailable tool references stripped when tool search is enabled
- **PDF/image errors**: User messages that preceded too-large errors have their
  document/image blocks stripped to prevent re-sending on every API call
- **Empty assistant content**: Replaced with sentinel text
- **Thinking blocks**: Stripped if model doesn't support extended thinking
- **Advisor blocks**: Stripped for non-advisor models

### Phase 4: Merge

Adjacent same-role messages are merged to satisfy the API's strict
user/assistant alternation requirement. Content arrays are concatenated;
string content is joined with newlines.

### Phase 5: Pair

`ensureToolResultPairing()` runs as a final safety net:
- Forward: inserts synthetic error `tool_result` blocks for unpaired `tool_use` blocks
- Reverse: strips orphaned `tool_result` blocks referencing non-existent `tool_use` blocks
- Cross-message deduplication: tracks all `tool_use` IDs across messages to catch
  duplicates that span separate assistant entries

### Flow Through QueryEngine

```
QueryEngine.submitMessage(prompt)
  -> processUserInput()           // creates UserMessage(s)
  -> mutableMessages.push(...)    // append to conversation
  -> query({messages, ...})       // enters the API loop
       -> normalizeMessagesForAPI(messages, tools)
       -> ensureToolResultPairing(normalized)
       -> prependUserContext(normalized, userContext)
       -> appendSystemContext(normalized, systemContext)
       -> claude.stream(systemPrompt, normalized)  // API call
       -> yield assistant/user/system messages
  -> messages.push(response)
  -> recordTranscript(messages)
```

## 8. Bootstrap State vs AppState (src/bootstrap/state.ts)

Claude Code has **two** state systems:

1. **AppState** (Store-based, reactive): UI state, permissions, MCP, plugins, tasks
2. **Bootstrap State** (module singleton, imperative): process-level counters

Bootstrap state uses a module-scoped `STATE` object with getter/setter functions:
`getSessionId()`, `getTotalCostUSD()`, `addToTotalCostState()`, etc. It exists
separately because:
- It needs to be importable from any module without creating React dependencies
- It contains process-level data (session ID, cost counters, OTel meters)
- It must survive React tree unmount/remount cycles
- The import DAG requires bootstrap to be a leaf (no circular deps)

The two systems are bridged: `QueryEngine` receives `getAppState` and
`setAppState` as constructor parameters, letting it read reactive state while
operating outside the React tree.

## 9. Speculation State

A notable mutable AppState partition manages speculative execution:

```typescript
type SpeculationState =
  | { status: 'idle' }
  | { status: 'active', id, abort: () => void, startTime,
      messagesRef: { current: Message[] },    // mutable ref
      writtenPathsRef: { current: Set<string> }, // overlay paths
      boundary: CompletionBoundary | null,
      suggestionLength, toolUseCount, isPipelined,
      contextRef: { current: REPLHookContext },
      pipelinedSuggestion?: { text, promptId, generationRequestId } }
```

The `{ current: ... }` refs are intentionally mutable to avoid array spreading
per message during speculation. This is a performance optimization: speculation
generates many messages rapidly, and immutable updates would create excessive
garbage collection pressure.
