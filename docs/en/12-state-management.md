# Chapter 12: State Management

Every subsystem described in previous chapters -- tools, permissions, MCP connections, tasks, hooks -- produces and consumes state. A permission mode changes, a model selection persists, a teammate joins, speculative execution begins. Without centralized management, this state scatters across modules as mutable globals, leading to update races, missed side effects, and an internal message list that diverges from what the API expects. This chapter examines how Claude Code concentrates all mutable state into a single store and normalizes its internal message stream for API consumption.

## The Problem

A CLI agent accumulates state from many sources. The permission system tracks the current mode and accumulated rules. The MCP layer maintains connection status for multiple servers. The task system holds a registry of background work items. The UI needs to know which model is selected, which view is active, and whether speculation is in progress.

If each subsystem manages its own state, three problems emerge. First, there is no single place to observe what changed -- debugging requires tracing through scattered mutation sites. Second, side effects (persisting a model choice, syncing permissions) must be triggered by each mutation site individually, creating duplication and inconsistency. Third, the internal representation of conversation messages is far richer than what the API accepts: system messages, progress indicators, tombstones, and tool-use summaries must all be stripped or transformed before each API call.

The normalization problem is particularly subtle. The Claude API enforces strict constraints: messages must alternate between user and assistant roles, the first message must be a user message, and tool_use blocks must be paired with tool_result blocks. The internal message list violates all of these constraints by design, since it serves the UI and debugging as well as the API.

## How Claude Code Solves It

### The Store primitive

The foundation is a generic `Store<T>` class -- approximately 35 lines of code. It holds a single state value, accepts functional updaters (never raw values), performs an identity bailout using `Object.is` to skip no-op updates, and notifies both an `onChange` hook and a set of subscribers.

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
    if (Object.is(next, prev)) return; // identity bailout
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

The identity bailout is the key invariant. If an updater function returns the exact same object reference, no listeners fire and no side effects trigger. This makes it safe to call `setState` speculatively -- passing an updater that might or might not produce a new state -- without worrying about spurious notifications.

### AppState: partitioned fields

The `AppState` type contains approximately 100 fields organized into 10 logical partitions. Some partitions are immutable after initialization (configuration, permissions). Others mutate frequently (UI state, task registry, speculation flags).

```typescript
// src/state/AppState.ts (representative structure)
interface AppState {
  // Partition 1: Core config (immutable after init)
  readonly settings: Settings;
  readonly mainLoopModel: string;

  // Partition 2: Permissions (immutable after init)
  readonly permissionMode: PermissionMode;
  readonly approvedTools: Set<string>;

  // Partition 3: UI (frequently mutable)
  viewState: ViewState;
  inputMode: InputMode;

  // Partition 4: Bridge
  bridgeState: BridgeState;

  // Partition 5: MCP
  mcpConnections: Map<string, McpConnectionState>;

  // Partition 6: Plugins
  loadedPlugins: PluginState[];

  // Partition 7: Tasks
  taskRegistry: Map<string, TaskState>;

  // Partition 8: Swarm
  teamState: TeamState | null;

  // Partition 9: Speculation
  speculationRef: SpeculationState | null;

  // Partition 10: Features
  featureFlags: Record<string, boolean>;
}
```

The immutable/mutable split is a convention, not a runtime enforcement. Partitions 1 and 2 are set during bootstrap and never modified. Partitions 3 through 10 are updated via `setState` throughout the session. The partition structure helps developers locate state by concern rather than searching through a flat list of 100 fields.

### The onChange reactor

The `Store` constructor accepts an `onChange` callback that fires on every state transition. This single function replaces scattered callbacks with one auditable site for all side effects.

```typescript
// src/state/onChangeAppState.ts
function onChangeAppState(next: AppState, prev: AppState): void {
  // Sync permission mode to environment
  if (next.permissionMode !== prev.permissionMode) {
    syncPermissionMode(next.permissionMode);
  }

  // Persist model choice to disk
  if (next.mainLoopModel !== prev.mainLoopModel) {
    persistModelChoice(next.mainLoopModel);
  }

  // Persist view state for session resume
  if (next.viewState !== prev.viewState) {
    persistViewState(next.viewState);
  }
}
```

Every side effect is a field-level comparison between `next` and `prev`. This pattern scales cleanly: adding a new side effect means adding one more comparison block, with no risk of disrupting existing logic.

### React integration

The UI layer uses React (via Ink for terminal rendering). The store integrates through a provider and a selector hook, following the same pattern as libraries like Zustand.

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

The `selector` function is critical for performance. A component that only needs `viewState` passes `(s) => s.viewState` and re-renders only when that specific field changes (assuming the selector returns a new reference). Components that do not use state at all never re-render from state changes.

### Message type hierarchy

The internal message list is not a flat array of `{role, content}` objects. It is a discriminated union of six primary types, with `SystemMessage` further divided into 14+ subtypes.

```typescript
// src/messages/types.ts
type Message =
  | UserMessage
  | AssistantMessage
  | SystemMessage          // 14+ subtypes: init, compact_boundary,
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
    // ... 14+ subtypes
  ;
  content: ContentBlock[];
}
```

This rich type hierarchy serves the UI (progress messages drive the spinner), debugging (tombstones mark removed content), and session management (compact boundaries separate pre- and post-compaction contexts). None of these types are valid API messages.

### The normalization pipeline

Before every API call, `normalizeMessagesForAPI()` transforms the internal message stream into the format the Claude API requires. The pipeline has five phases.

```typescript
// src/messages/normalizeMessagesForAPI.ts
function normalizeMessagesForAPI(messages: Message[]): ApiMessage[] {
  // Phase 1: Filter — remove system, progress, tombstone messages.
  //          Convert local_command to user messages.
  let filtered = messages.filter(m =>
    m.type === "user" || m.type === "assistant"
  );

  // Phase 2: Reorder — ensure tool_result follows its tool_use.
  filtered = reorderToolPairs(filtered);

  // Phase 3: Transform — strip thinking blocks, replace empty
  //          content with sentinel text.
  filtered = filtered.map(transformContent);

  // Phase 4: Merge — combine adjacent same-role messages.
  const merged = mergeAdjacentSameRole(filtered);

  // Phase 5: Ensure first message is user role.
  if (merged[0]?.role !== "user") {
    merged.unshift({ role: "user", content: "[system initialized]" });
  }

  return merged;
}
```

Phase 4 (merging adjacent same-role messages) handles a common situation: after filtering out system messages, two consecutive user messages may remain. The API rejects this, so the pipeline merges them into one. Phase 5 handles the edge case where the first surviving message after filtering is an assistant message, which violates the API's requirement that conversations begin with a user turn.

### Bootstrap state and speculation

Two additional state systems coexist with AppState. Bootstrap state holds the configuration gathered during startup (CLI arguments, environment detection, settings file loading) and is bridged into AppState during initialization. Speculation state uses mutable refs rather than the Store pattern, because speculative execution requires high-frequency updates without the overhead of change detection and listener notification.

```typescript
// src/state/speculation.ts
interface SpeculationState {
  active: boolean;
  pendingMessages: Message[];
  checkpoint: AppState;  // snapshot to revert on rejection
}
```

The speculation checkpoint captures a snapshot of AppState at the moment speculative execution begins. If the speculation is rejected, the system reverts to this checkpoint rather than attempting to undo individual changes.

## Key Design Decisions

**Functional updaters instead of direct mutation.** Requiring `setState(prev => ({...prev, field: newValue}))` instead of `state.field = newValue` ensures that every mutation flows through a single point. This makes the onChange reactor reliable -- it always sees the complete before-and-after state.

**Identity bailout with Object.is instead of deep equality.** Deep equality checking on a 100-field state object would be expensive. Identity comparison is a single pointer check. The tradeoff is that updaters must return new object references to signal changes, which is standard practice in immutable-update patterns.

**One onChange reactor instead of per-field watchers.** A per-field watcher system (like Vue's `watch`) is more granular but harder to audit. The single reactor function shows all side effects in one file, making it straightforward to verify that a state change triggers the correct downstream actions.

**Five-phase normalization instead of maintaining a parallel API-ready list.** Maintaining two synchronized message lists (internal and API-ready) introduces the risk of divergence. Normalizing on demand from a single source of truth eliminates this risk at the cost of repeated computation. The pipeline is fast enough that the cost is negligible relative to the API call it precedes.

**Mutable refs for speculation instead of Store.** Speculative execution involves rapid, high-frequency state updates during a single render cycle. Running each update through the Store's identity check, onChange reactor, and listener notification would introduce unnecessary overhead. Mutable refs bypass this machinery, with the understanding that speculation state is transient and does not need side-effect tracking.

## In Practice

When a user switches models via the CLI, the UI calls `setState(prev => ({...prev, mainLoopModel: "claude-sonnet-4-20250514"}))`. The Store's identity check detects a new reference. The onChange reactor fires, comparing the new and old `mainLoopModel` values. Because they differ, it calls `persistModelChoice()` to write the selection to disk. React components subscribed via `useAppState(s => s.mainLoopModel)` re-render to reflect the change.

When the agent loop prepares an API call, it passes the internal message list through `normalizeMessagesForAPI()`. A conversation with 40 internal messages (including progress updates, system init messages, compact boundaries, and tombstones) emerges as 12 clean API messages that strictly alternate between user and assistant roles.

## Summary

- A generic `Store<T>` with functional updaters, identity bailout, and an onChange hook centralizes all state management in approximately 35 lines of code.
- AppState contains approximately 100 fields across 10 logical partitions, with a clear split between immutable configuration and frequently-mutated runtime state.
- A single onChange reactor replaces scattered side-effect callbacks, making all state-driven persistence and synchronization auditable in one location.
- The internal message hierarchy (6 primary types, 14+ system subtypes) serves UI, debugging, and session management, then collapses to clean API format through a five-phase normalization pipeline.
- Speculation state uses mutable refs for performance, with a checkpoint snapshot enabling full rollback on rejection.
