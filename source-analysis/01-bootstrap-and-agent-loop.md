# Source Analysis: Bootstrap Sequence and Agent Loop

Deep annotated walkthrough of how Claude Code starts up and runs its agent loop.
All file paths are relative to the Claude Code source root.
Line numbers are approximate and may shift between versions.

---

## 1. Bootstrap Sequence: cli.tsx Fast Path

**File**: `src/entrypoints/cli.tsx` (302 lines)

The CLI entrypoint is designed for *minimal module evaluation* on fast paths.
Every import is dynamic (`await import(...)`) to avoid loading the full
dependency tree when it is not needed.

```typescript
// src/entrypoints/cli.tsx ~L33-42
async function main(): Promise<void> {
  const args = process.argv.slice(2);

  // ANNOTATION: Fast-path for --version/-v: zero module loading needed.
  // This returns before importing ANYTHING -- not even the startup profiler.
  // Real measured time: <5ms vs ~300ms for full startup.
  if (args.length === 1 && (args[0] === '--version' || args[0] === '-v' || args[0] === '-V')) {
    console.log(`${MACRO.VERSION} (Claude Code)`);
    return;
  }

  // ANNOTATION: Only NOW do we load the startup profiler.
  // This is the first dynamic import -- everything before was zero-import.
  const { profileCheckpoint } = await import('../utils/startupProfiler.js');
  profileCheckpoint('cli_entry');
```

**Design rationale**: The fast-path pattern avoids the ~300ms cost of loading
the full module graph for trivial operations. This is a common pattern in
production CLIs but rarely seen in tutorials.

Additional fast paths in cli.tsx:
- `--dump-system-prompt` (~L53): Outputs the rendered system prompt. Used by prompt sensitivity evals.
- `--daemon-worker` (~L100): Spawns a lean worker process with no configs/analytics overhead.
- `remote-control` / `rc` (~L112): Bridge mode for serving the local machine remotely.

After all fast paths are exhausted, cli.tsx dynamically imports `main.tsx`:

```typescript
// src/entrypoints/cli.tsx (near end of file)
// ANNOTATION: This is the "heavy path" -- loads the full CLI.
// main.tsx has ~170 static imports at the top level.
const { default: mainFn } = await import('../main.js');
await mainFn();
```

---

## 2. Full Initialization: main.tsx

**File**: `src/main.tsx` (4,683 lines)

This is the heaviest file in the codebase. It handles argument parsing,
authentication, configuration, plugin loading, MCP server setup, and REPL launch.

### Side-Effect Imports (Lines 1-20)

```typescript
// src/main.tsx ~L1-20
// ANNOTATION: These side-effects MUST run before all other imports.
// They fire parallel subprocesses so I/O overlaps with module evaluation.
import { profileCheckpoint, profileReport } from './utils/startupProfiler.js';
profileCheckpoint('main_tsx_entry');          // ANNOTATION: Timestamp before heavy imports begin

import { startMdmRawRead } from './utils/settings/mdm/rawRead.js';
startMdmRawRead();                           // ANNOTATION: Fire MDM subprocess (plutil/reg query) in parallel

import { startKeychainPrefetch } from './utils/secureStorage/keychainPrefetch.js';
startKeychainPrefetch();                     // ANNOTATION: Fire macOS keychain reads in parallel (~65ms saved)
```

**Design rationale**: By starting I/O-bound operations (keychain reads, MDM
queries) *before* the remaining ~135ms of static imports, these operations
complete for free during module evaluation. This is a production-grade
optimization not seen in any tutorial.

### Static Imports (Lines 21-200)

Lines 21-200 contain approximately 170 static imports covering:
- Commander.js for arg parsing
- React/Ink for the terminal UI
- Authentication, config, and settings modules
- MCP client infrastructure
- Plugin and skill systems
- Analytics and telemetry

The sheer volume of imports (and the careful ordering of side-effect imports
before them) is itself an architectural signal: Claude Code is a large,
modular system, not a simple script.

---

## 3. QueryEngine: Conversation State Owner

**File**: `src/QueryEngine.ts` (1,295 lines)

QueryEngine is the bridge between the entry point (SDK/headless/REPL) and the
inner `query()` function. One QueryEngine per conversation.

### Class Structure

```typescript
// src/QueryEngine.ts ~L184-207
// ANNOTATION: QueryEngine owns the mutable state that persists across turns.
// Each submitMessage() call is one turn within the same conversation.
export class QueryEngine {
  private config: QueryEngineConfig
  private mutableMessages: Message[]              // ANNOTATION: The full conversation history
  private abortController: AbortController
  private permissionDenials: SDKPermissionDenial[]
  private totalUsage: NonNullableUsage            // ANNOTATION: Cumulative token usage
  private hasHandledOrphanedPermission = false
  private readFileState: FileStateCache           // ANNOTATION: Tracks which files the model has seen
  private discoveredSkillNames = new Set<string>()
  private loadedNestedMemoryPaths = new Set<string>()

  constructor(config: QueryEngineConfig) {
    this.config = config
    this.mutableMessages = config.initialMessages ?? []
    this.abortController = config.abortController ?? createAbortController()
    this.permissionDenials = []
    this.readFileState = config.readFileCache
    this.totalUsage = EMPTY_USAGE
  }
```

### The submitMessage Generator

```typescript
// src/QueryEngine.ts ~L209-212
// ANNOTATION: submitMessage is an AsyncGenerator, NOT an async function.
// It YIELDS messages as they happen -- the caller consumes them as a stream.
// This is the key architectural insight that tutorials miss.
async *submitMessage(
  prompt: string | ContentBlockParam[],
  options?: { uuid?: string; isMeta?: boolean },
): AsyncGenerator<SDKMessage, void, unknown> {
```

Inside `submitMessage` (~L209-675):
1. Destructures config into local variables (~L213-236)
2. Wraps `canUseTool` to track permission denials (~L244-271)
3. Fetches the system prompt (calls `fetchSystemPromptParts`) (~L284-300)
4. Processes user input via `processUserInput()` (~L410-428)
5. Records the transcript for resume support (~L440-463)
6. **Calls `query()` and yields its output** (~L675+):

```typescript
// src/QueryEngine.ts ~L675-686
// ANNOTATION: This is where the inner agent loop is invoked.
// query() is itself a generator -- yield* delegates all its yields to the caller.
for await (const message of query({
  messages,
  systemPrompt,
  userContext,
  systemContext,
  canUseTool: wrappedCanUseTool,
  toolUseContext: processUserInputContext,
  fallbackModel,
  querySource: 'sdk',
  maxTurns,
  taskBudget,
})) {
  // ... message handling, transcript recording, usage tracking ...
}
```

---

## 4. The Agent Loop: query() and queryLoop()

**File**: `src/query.ts` (1,729 lines)

This is the heart of Claude Code. The agent loop is implemented as two nested
generators.

### Outer Generator: query()

```typescript
// src/query.ts ~L219-239
// ANNOTATION: The outer generator wraps queryLoop and handles command lifecycle.
// Terminal is the return type -- a discriminated union of exit reasons.
export async function* query(
  params: QueryParams,
): AsyncGenerator<
  | StreamEvent
  | RequestStartEvent
  | Message
  | TombstoneMessage
  | ToolUseSummaryMessage,
  Terminal                                        // ANNOTATION: Return value is { reason: string }
> {
  const consumedCommandUuids: string[] = []
  const terminal = yield* queryLoop(params, consumedCommandUuids)
  // ANNOTATION: Command lifecycle notifications only fire on normal completion.
  // On throw/abort, they are skipped (asymmetric started-without-completed signal).
  for (const uuid of consumedCommandUuids) {
    notifyCommandLifecycle(uuid, 'completed')
  }
  return terminal
}
```

### Inner Generator: queryLoop() -- The State Machine

```typescript
// src/query.ts ~L241-280
async function* queryLoop(
  params: QueryParams,
  consumedCommandUuids: string[],
): AsyncGenerator<...> {
  // ANNOTATION: Immutable params -- never reassigned during the loop.
  const { systemPrompt, userContext, systemContext, canUseTool, ... } = params

  // ANNOTATION: Mutable cross-iteration state. This is the key data structure.
  // Each "continue" site rebuilds the entire State object.
  let state: State = {
    messages: params.messages,
    toolUseContext: params.toolUseContext,
    maxOutputTokensOverride: params.maxOutputTokensOverride,
    autoCompactTracking: undefined,
    stopHookActive: undefined,
    maxOutputTokensRecoveryCount: 0,
    hasAttemptedReactiveCompact: false,
    turnCount: 1,
    pendingToolUseSummary: undefined,
    transition: undefined,              // ANNOTATION: Why we continued. Undefined on first iteration.
  }
```

### The Main while(true) Loop

```typescript
// src/query.ts ~L307
// ANNOTATION: This is the actual loop. Each iteration = one API call + tool execution.
while (true) {
  let { toolUseContext } = state
  const { messages, autoCompactTracking, maxOutputTokensRecoveryCount, ... } = state
```

Each iteration of the loop does:

#### Step 1: Pre-processing pipeline (~L365-450)
```
messages -> applyToolResultBudget -> snipCompact -> microcompact -> contextCollapse -> autocompact
```

Each stage is independently feature-gated and can be compiled out of external builds.

#### Step 2: API Call with Streaming (~L654-863)

```typescript
// src/query.ts ~L659-708
// ANNOTATION: The model call streams messages. Each yielded message is either
// a stream event, an assistant message (with possible tool_use blocks), or an error.
for await (const message of deps.callModel({
  messages: prependUserContext(messagesForQuery, userContext),
  systemPrompt: fullSystemPrompt,
  thinkingConfig: toolUseContext.options.thinkingConfig,
  tools: toolUseContext.options.tools,
  signal: toolUseContext.abortController.signal,
  options: { ... },
})) {
  // ANNOTATION: While streaming, tool_use blocks are extracted and dispatched.
  if (message.type === 'assistant') {
    assistantMessages.push(message)
    const msgToolUseBlocks = message.message.content.filter(
      content => content.type === 'tool_use',
    ) as ToolUseBlock[]
    if (msgToolUseBlocks.length > 0) {
      toolUseBlocks.push(...msgToolUseBlocks)
      needsFollowUp = true                       // ANNOTATION: This flag drives the loop continuation
    }
    // ANNOTATION: Streaming tool execution -- tools start running while model still streams!
    if (streamingToolExecutor) {
      for (const toolBlock of msgToolUseBlocks) {
        streamingToolExecutor.addTool(toolBlock, message)
      }
    }
  }
}
```

#### Step 3: Tool Dispatch (~L1360-1408)

```typescript
// src/query.ts ~L1380-1408
// ANNOTATION: After streaming completes, remaining tools are executed.
// StreamingToolExecutor may have already started some during streaming.
const toolUpdates = streamingToolExecutor
  ? streamingToolExecutor.getRemainingResults()                    // ANNOTATION: Finish what started during streaming
  : runTools(toolUseBlocks, assistantMessages, canUseTool, toolUseContext)  // ANNOTATION: Or run all now

for await (const update of toolUpdates) {
  if (update.message) {
    yield update.message                          // ANNOTATION: Tool results yielded to caller in real-time
    toolResults.push(...)
  }
}
```

#### Step 4: Continuation Decision (~L1062-1728)

The loop decides whether to continue based on several conditions:

| Condition | Transition Reason | What Happens |
|-----------|------------------|--------------|
| No tool_use blocks | `completed` | **Return** -- the model is done |
| Tool results exist | `next_turn` | Feed results back, continue loop |
| Prompt too long (413) | `reactive_compact_retry` | Compact and retry |
| Max output tokens hit | `max_output_tokens_recovery` | Inject recovery message, continue |
| Stop hook blocked | `stop_hook_blocking` | Inject hook errors, continue |
| Context collapse drained | `collapse_drain_retry` | Re-run with collapsed context |
| Max turns exceeded | `max_turns` | **Return** with attachment |

```typescript
// src/query.ts ~L1715-1728
// ANNOTATION: The most common continuation -- tool results need a follow-up.
const next: State = {
  messages: [...messagesForQuery, ...assistantMessages, ...toolResults],
  toolUseContext: toolUseContextWithQueryTracking,
  autoCompactTracking: tracking,
  turnCount: nextTurnCount,
  maxOutputTokensRecoveryCount: 0,
  hasAttemptedReactiveCompact: false,
  pendingToolUseSummary: nextPendingToolUseSummary,
  maxOutputTokensOverride: undefined,
  stopHookActive,
  transition: { reason: 'next_turn' },           // ANNOTATION: Typed transition for test assertions
}
state = next
// ANNOTATION: No explicit "continue" -- falls to top of while(true)
```

---

## 5. Key Constants and Thresholds

Found in the source code:

```typescript
// src/query.ts ~L164
const MAX_OUTPUT_TOKENS_RECOVERY_LIMIT = 3        // ANNOTATION: Max retries for output-token-limit recovery

// src/utils/context.ts
const ESCALATED_MAX_TOKENS = 64_000               // ANNOTATION: Escalated limit when default 8k is hit

// src/services/compact/autoCompact.ts (referenced from lib/utils.py)
AUTOCOMPACT_BUFFER_TOKENS = 13_000
WARNING_THRESHOLD_BUFFER_TOKENS = 20_000
MAX_CONSECUTIVE_AUTOCOMPACT_FAILURES = 3
```

---

## 6. Design Decisions and Rationale

### Why Generators Instead of a Simple Loop?

The generator pattern (`async function*`) serves multiple purposes:

1. **Streaming to the UI**: Each `yield` pushes a message to the terminal UI or SDK consumer *immediately*. A simple loop would batch all output at the end.

2. **Backpressure**: The consumer controls the pace. If the UI is busy rendering, it naturally slows down consumption.

3. **Composability**: `yield*` allows the outer `query()` to delegate to `queryLoop()` transparently. The caller sees a single flat stream regardless of internal structure.

4. **Cancellation**: When the consumer calls `.return()` on the generator (e.g., user presses Ctrl+C), both generators close cleanly via the `using` cleanup pattern.

### Why a State Object Instead of Mutable Variables?

Each `continue` site rebuilds the entire `State` object:

```typescript
state = { messages: [...], toolUseContext, transition: { reason: 'next_turn' } }
```

This provides:
- **Auditability**: The `transition` field records *why* each continuation happened, enabling test assertions without inspecting message contents.
- **Atomic updates**: All 9 state fields are updated together, preventing partial-update bugs.
- **Immutable-ish pattern**: Although `state` is reassigned, each individual State is never mutated after creation.

### Why Streaming Tool Execution?

`StreamingToolExecutor` starts tool execution while the model is still streaming:

```
Time -->
Model streaming: [text][text][tool_use_1][text][tool_use_2][stop]
Tool execution:              [---tool_1 running---]  [---tool_2 running---]
                                                        (started during stream!)
```

For a typical turn with 2-3 tool calls, this saves 1-5 seconds by overlapping
tool I/O with the remaining model output.

### Why Feature Gates Everywhere?

Almost every non-core feature is behind a `feature('FLAG_NAME')` gate:

```typescript
if (feature('HISTORY_SNIP')) { ... }
if (feature('CONTEXT_COLLAPSE') && contextCollapse) { ... }
if (feature('CACHED_MICROCOMPACT')) { ... }
```

These are *build-time* gates (`bun:bundle`), not runtime flags. The entire
code block (including its imports) is dead-code-eliminated from external
builds. This keeps the open-source/external binary small while allowing
internal experimentation.
