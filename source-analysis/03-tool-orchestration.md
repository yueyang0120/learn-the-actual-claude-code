# Session 03 -- Source Analysis: Tool Orchestration

Deep annotated walkthrough of how Claude Code orchestrates tool execution.

---

## 1. The `runTools()` async generator

**File:** `src/services/tools/toolOrchestration.ts`, lines 19-82

`runTools()` is the entry point called by the agent loop after an assistant
message containing one or more `tool_use` blocks. It is an **async generator**
-- it `yield`s `MessageUpdate` objects as tools complete rather than returning
them all at once.

```typescript
export async function* runTools(
  toolUseMessages: ToolUseBlock[],     // All tool_use blocks from this turn
  assistantMessages: AssistantMessage[], // The assistant messages they came from
  canUseTool: CanUseToolFn,            // Permission check callback
  toolUseContext: ToolUseContext,       // Shared mutable context
): AsyncGenerator<MessageUpdate, void> {
  let currentContext = toolUseContext

  // Step 1: Partition into batches
  for (const { isConcurrencySafe, blocks } of partitionToolCalls(
    toolUseMessages,
    currentContext,
  )) {
    if (isConcurrencySafe) {
      // --- Concurrent path ---
      // Queue context modifiers; don't apply until batch finishes
      const queuedContextModifiers: Record<
        string,
        ((context: ToolUseContext) => ToolUseContext)[]
      > = {}

      for await (const update of runToolsConcurrently(
        blocks, assistantMessages, canUseTool, currentContext,
      )) {
        // Collect context modifiers keyed by tool use ID
        if (update.contextModifier) {
          const { toolUseID, modifyContext } = update.contextModifier
          if (!queuedContextModifiers[toolUseID]) {
            queuedContextModifiers[toolUseID] = []
          }
          queuedContextModifiers[toolUseID].push(modifyContext)
        }
        // Yield messages immediately -- UI sees progress in real time
        yield { message: update.message, newContext: currentContext }
      }

      // Apply queued context modifiers in deterministic order
      // (same order as the original blocks array)
      for (const block of blocks) {
        const modifiers = queuedContextModifiers[block.id]
        if (!modifiers) continue
        for (const modifier of modifiers) {
          currentContext = modifier(currentContext)
        }
      }
      yield { newContext: currentContext }  // Notify caller of final context

    } else {
      // --- Serial path ---
      for await (const update of runToolsSerially(
        blocks, assistantMessages, canUseTool, currentContext,
      )) {
        if (update.newContext) {
          currentContext = update.newContext  // Apply immediately
        }
        yield { message: update.message, newContext: currentContext }
      }
    }
  }
}
```

### Key design decisions

- **Yield-based streaming.** The caller (`for await (const update of runTools(...))`)
  sees each tool result as it completes. This is how the TUI updates in real time.
- **Context is threaded, not global.** `currentContext` is passed through and
  mutated only at well-defined points. Concurrent batches defer mutations to
  preserve determinism.
- **Batches execute in order.** Even though tools within a concurrent batch run
  in parallel, the batches themselves execute sequentially. If the model returns
  `[Read, Read, Write, Read]`, partitioning produces `[Read,Read]` then `[Write]`
  then `[Read]` -- the write always happens between the two read groups.

---

## 2. `partitionToolCalls()` -- batching logic

**File:** `src/services/tools/toolOrchestration.ts`, lines 91-116

```typescript
type Batch = { isConcurrencySafe: boolean; blocks: ToolUseBlock[] }

function partitionToolCalls(
  toolUseMessages: ToolUseBlock[],
  toolUseContext: ToolUseContext,
): Batch[] {
  return toolUseMessages.reduce((acc: Batch[], toolUse) => {
    const tool = findToolByName(toolUseContext.options.tools, toolUse.name)
    const parsedInput = tool?.inputSchema.safeParse(toolUse.input)

    // Determine concurrency safety -- depends on BOTH the tool AND its input
    const isConcurrencySafe = parsedInput?.success
      ? (() => {
          try {
            return Boolean(tool?.isConcurrencySafe(parsedInput.data))
          } catch {
            return false  // Conservative: treat parse failures as unsafe
          }
        })()
      : false

    // Merge into the last batch if both are concurrency-safe
    if (isConcurrencySafe && acc[acc.length - 1]?.isConcurrencySafe) {
      acc[acc.length - 1]!.blocks.push(toolUse)
    } else {
      acc.push({ isConcurrencySafe, blocks: [toolUse] })
    }
    return acc
  }, [])
}
```

### How `isConcurrencySafe` works per tool

The method is defined on the `Tool` interface in `src/Tool.ts`:

```typescript
isConcurrencySafe(input: z.infer<Input>): boolean
```

Default is `false` (fail-closed). Examples from the codebase:

| Tool | `isConcurrencySafe` | Logic |
|------|---------------------|-------|
| `FileReadTool` | Always `true` | Reading files never conflicts |
| `GrepTool` | Always `true` | Pure search |
| `GlobTool` | Always `true` | Pure search |
| `BashTool` | `this.isReadOnly(input)` | Only safe when command is classified as read-only |
| `FileEditTool` | `false` (default) | Writes to files |
| `FileWriteTool` | `false` (default) | Writes to files |

**BashTool's `isReadOnly`** is notable -- it parses the shell command and checks
constraints like whether it contains `cd`, writes to files, runs `rm`, etc.
This means `bash cat foo.txt` runs concurrently but `bash echo x > out` does not.

### Partitioning example

Given tool calls: `[Grep, FileRead, BashWrite, GlobTool, FileRead]`

Partition result:
```
Batch 1: { isConcurrencySafe: true,  blocks: [Grep, FileRead] }
Batch 2: { isConcurrencySafe: false, blocks: [BashWrite] }
Batch 3: { isConcurrencySafe: true,  blocks: [GlobTool, FileRead] }
```

Execution order: Batch 1 runs concurrently -> Batch 2 runs alone -> Batch 3
runs concurrently. Write ordering is preserved.

---

## 3. `getMaxToolUseConcurrency()` -- env-configurable cap

**File:** `src/services/tools/toolOrchestration.ts`, lines 8-12

```typescript
function getMaxToolUseConcurrency(): number {
  return (
    parseInt(process.env.CLAUDE_CODE_MAX_TOOL_USE_CONCURRENCY || '', 10) || 10
  )
}
```

Simple but effective: parse the env var, fall back to 10. This value is passed
to the `all()` utility as the concurrency cap.

**Why 10?** It balances throughput vs. resource pressure. Reading 10 files
concurrently saturates typical SSD I/O without spawning hundreds of file handles.
Users with slow NFS mounts might lower it; users with fast SSDs might raise it.

---

## 4. The `all()` utility -- bounded async generator combiner

**File:** `src/utils/generators.ts`, lines 32-72

This is the engine behind concurrent execution. It is NOT `Promise.all` -- it is
a **pull-based scheduler** over async generators with a concurrency cap.

```typescript
export async function* all<A>(
  generators: AsyncGenerator<A, void>[],
  concurrencyCap = Infinity,
): AsyncGenerator<A, void> {
  const next = (generator: AsyncGenerator<A, void>) => {
    const promise = generator.next().then(({ done, value }) => ({
      done, value, generator, promise,
    }))
    return promise
  }

  const waiting = [...generators]          // Generators not yet started
  const promises = new Set<Promise<...>>() // Currently racing

  // Start initial batch up to concurrency cap
  while (promises.size < concurrencyCap && waiting.length > 0) {
    const gen = waiting.shift()!
    promises.add(next(gen))
  }

  while (promises.size > 0) {
    // Race all active generators -- whoever yields first wins
    const { done, value, generator, promise } = await Promise.race(promises)
    promises.delete(promise)

    if (!done) {
      promises.add(next(generator))    // Generator has more values; re-arm it
      if (value !== undefined) {
        yield value                     // Pass through to caller
      }
    } else if (waiting.length > 0) {
      const nextGen = waiting.shift()!  // Generator finished; start next one
      promises.add(next(nextGen))
    }
  }
}
```

### Why async generators instead of Promise.all?

Each tool execution is itself an async generator that can yield **multiple**
updates (progress messages, intermediate results, context modifiers). Using
`Promise.all` would require collecting all results first, then emitting them
in a batch. The generator-based approach lets the UI stream progress from
multiple concurrent tools as it happens.

---

## 5. `MessageUpdate` and `MessageUpdateLazy` types

**File:** `src/services/tools/toolOrchestration.ts`, lines 14-17
**File:** `src/services/tools/toolExecution.ts`, lines 264-270

```typescript
// From toolOrchestration.ts -- the final shape yielded to the agent loop
export type MessageUpdate = {
  message?: Message        // Tool result, progress, error, etc.
  newContext: ToolUseContext // Updated context after this tool
}

// From toolExecution.ts -- internal shape with lazy context modifiers
export type MessageUpdateLazy<M extends Message = Message> = {
  message: M
  contextModifier?: {
    toolUseID: string
    modifyContext: (context: ToolUseContext) => ToolUseContext
  }
}
```

The "Lazy" variant carries a **context modifier function** instead of an already-
applied context. This is critical for concurrent execution: you cannot apply
context changes immediately because other tools in the batch are reading the
old context. The orchestrator collects all modifiers, then applies them in
order after the batch completes.

---

## 6. Pre/Post tool hooks in the execution pipeline

**File:** `src/services/tools/toolHooks.ts`

Hooks intercept tool execution at two points:

### PreToolUse hooks (`runPreToolUseHooks`)

Run **before** each tool executes. They can:
- **Allow** (bypass permission prompt): `{ permissionBehavior: 'allow' }`
- **Deny** (block execution): `{ permissionBehavior: 'deny' }`
- **Ask** (force prompt even if rules say allow): `{ permissionBehavior: 'ask' }`
- **Modify input**: `{ updatedInput: {...} }` -- transparent to the model
- **Inject additional context**: `{ additionalContexts: [...] }`
- **Prevent continuation**: stop the agentic loop entirely

```typescript
export async function* runPreToolUseHooks(
  toolUseContext, tool, processedInput, toolUseID, ...
): AsyncGenerator<
  | { type: 'message'; message: MessageUpdateLazy }
  | { type: 'hookPermissionResult'; hookPermissionResult: PermissionResult }
  | { type: 'hookUpdatedInput'; updatedInput: Record<string, unknown> }
  | { type: 'preventContinuation'; shouldPreventContinuation: boolean }
  | { type: 'stop' }
> {
  for await (const result of executePreToolHooks(tool.name, ...)) {
    // Dispatch based on result type...
  }
}
```

### PostToolUse hooks (`runPostToolUseHooks`)

Run **after** successful tool execution. They can:
- Inject additional context into the conversation
- Block (override the result with an error)
- Prevent continuation of the agentic loop
- Modify MCP tool output

### PostToolUseFailure hooks (`runPostToolUseFailureHooks`)

Run **after** a tool fails. Same capabilities as post-hooks but receives the
error string instead of the tool output.

### Hook permission resolution (`resolveHookPermissionDecision`)

A critical subtlety: a hook saying "allow" does NOT bypass settings.json deny
rules. The resolution logic:

```
Hook says ALLOW:
  -> Check settings.json rules
  -> If rule says DENY: deny wins (security boundary)
  -> If rule says ASK: prompt the user anyway
  -> If no rule: allow (hook bypasses interactive prompt)

Hook says DENY:
  -> Always deny (hooks can block anything)

Hook says ASK:
  -> Force the permission prompt with the hook's message
```

---

## 7. Error handling and abort controller integration

### Abort controller

Every `ToolUseContext` carries an `AbortController`. Before executing each tool,
the code checks `abortController.signal.aborted`:

```typescript
if (toolUseContext.abortController.signal.aborted) {
  yield { message: createUserMessage({
    content: [createToolResultStopMessage(toolUse.id)],
    toolUseResult: CANCEL_MESSAGE,
  }) }
  return
}
```

The `StreamingToolExecutor` also creates a **child** abort controller so that
when one bash command in a concurrent batch fails, sibling processes are
cancelled without aborting the entire query.

### In-progress tracking

Tools register themselves as in-progress via `setInProgressToolUseIDs`:

```typescript
// Before execution
toolUseContext.setInProgressToolUseIDs(prev => new Set(prev).add(toolUse.id))

// After execution (via markToolUseAsComplete)
toolUseContext.setInProgressToolUseIDs(prev => {
  const next = new Set(prev)
  next.delete(toolUseID)
  return next
})
```

This powers the TUI's "X tools running" indicator.

### Error classification

`classifyToolError()` in `toolExecution.ts` maps errors to telemetry-safe
strings, handling the fact that minified builds mangle constructor names:

```typescript
export function classifyToolError(error: unknown): string {
  if (error instanceof TelemetrySafeError) return error.telemetryMessage
  if (error instanceof Error) {
    const errnoCode = getErrnoCode(error)
    if (errnoCode) return `Error:${errnoCode}`         // ENOENT, EACCES, etc.
    if (error.name && error.name.length > 3) return error.name
    return 'Error'
  }
  return 'UnknownError'
}
```

### The full execution pipeline (per tool)

```
1. Check abort signal
2. Find tool definition (or yield "no such tool" error)
3. Validate input against Zod schema
4. Run PreToolUse hooks
5. Resolve hook permission decision
6. Check canUseTool (interactive permission prompt if needed)
7. Start telemetry span
8. Execute tool.call(input, context, progress)
9. Run PostToolUse hooks (or PostToolUseFailure hooks on error)
10. Emit tool_result message
11. Mark tool as complete
```

Each step can short-circuit with a yield, and the whole thing is wrapped in
error handling that ensures a `tool_result` message is always emitted (even
on crash), so the API conversation stays well-formed.
