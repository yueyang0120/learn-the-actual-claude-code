# Chapter 3: Tool Orchestration

Chapter 2 defined what a tool is. This chapter describes how multiple tool calls from a single model response are scheduled, executed, and finalized. The orchestration layer sits between the agent loop (Chapter 1) and individual tool implementations (Chapter 2), turning a list of tool-use blocks into a sequence of results.

## The Problem

When the model responds to a query, it may request multiple tool calls in a single response. A typical example: the model wants to read three files and run a grep search. These four operations are all read-only and independent — executing them one after another wastes time. But if the model also requests a file write in the same response, that write must not run concurrently with the reads, because the reads might depend on the pre-write state of the filesystem.

The orchestration problem is: given an ordered list of tool calls with per-invocation safety metadata, partition them into batches that maximize concurrency without violating safety constraints, then execute each batch with bounded parallelism, running pre- and post-execution hooks at every step.

A secondary concern is extensibility. Claude Code supports hooks — user-defined scripts that run before or after tool execution. These hooks can inspect, modify, or block tool calls. The orchestration layer must integrate hooks without entangling them with the core scheduling logic.

A third concern is resource management. Unbounded parallelism can exhaust file descriptors, saturate disk I/O, or overwhelm a local development server with simultaneous requests. The orchestration layer must cap concurrency at a reasonable limit while still providing meaningful speedup over serial execution.

## How Claude Code Solves It

### The Entry Point

The orchestration logic lives in `toolOrchestration.ts` (188 lines). Despite the complexity of its responsibilities, the implementation is compact — a sign that the abstractions are well-chosen. The entry point is `runTools()`, an async generator that accepts the list of tool-use blocks from the model's response and yields results as they complete:

```typescript
// src/toolOrchestration.ts — entry point (conceptual)
async function* runTools(
  toolUseBlocks: ToolUseBlock[],
  context: ToolUseContext
): AsyncGenerator<ToolResult> {
  const batches = partitionToolCalls(toolUseBlocks, context)
  for (const batch of batches) {
    yield* executeBatch(batch, context)
  }
}
```

The function first partitions the tool calls into batches, then executes each batch sequentially, yielding results as they arrive. The `yield*` delegation means results from concurrent tools within a batch stream out as soon as each individual tool finishes, rather than waiting for the entire batch to complete. This is important for the UI: the user sees tool results appearing one at a time, providing a sense of progress even when multiple tools are running in parallel.

### Partitioning into Batches

The `partitionToolCalls()` function scans the ordered list of tool-use blocks and groups them into batches based on a single criterion: the result of `isConcurrencySafe(input)` for each tool call. Consecutive calls where this function returns `true` are grouped into a single concurrent batch. Any call where it returns `false` terminates the current concurrent batch (if one exists) and starts a new serial batch containing only that call.

The algorithm is a single linear scan with O(n) complexity:

```typescript
// src/toolOrchestration.ts — partitioning (conceptual)
function partitionToolCalls(
  blocks: ToolUseBlock[],
  context: ToolUseContext
): Batch[] {
  const batches: Batch[] = []
  let currentConcurrent: ToolUseBlock[] = []

  for (const block of blocks) {
    const tool = lookupTool(block.name, context)
    if (tool.isConcurrencySafe(block.input)) {
      currentConcurrent.push(block)
    } else {
      if (currentConcurrent.length > 0) {
        batches.push({ type: 'concurrent', blocks: currentConcurrent })
        currentConcurrent = []
      }
      batches.push({ type: 'serial', blocks: [block] })
    }
  }
  if (currentConcurrent.length > 0) {
    batches.push({ type: 'concurrent', blocks: currentConcurrent })
  }
  return batches
}
```

Consider this sequence of tool calls from a single model response:

```
[Grep, FileRead, BashWrite, GlobTool, FileRead]
```

The partitioning logic evaluates each call:

| Tool | `isConcurrencySafe(input)` | Batch |
|---|---|---|
| Grep | true | Batch 1 (concurrent) |
| FileRead | true | Batch 1 (concurrent) |
| BashWrite | false | Batch 2 (serial) |
| GlobTool | true | Batch 3 (concurrent) |
| FileRead | true | Batch 3 (concurrent) |

This produces three batches. Batch 1 runs Grep and FileRead in parallel. Batch 2 runs BashWrite alone. Batch 3 runs GlobTool and FileRead in parallel. The batches execute strictly in order: 1 completes, then 2 completes, then 3 completes.

The order of tool calls within the original list is preserved within batches. This matters because the model may have ordered its tool calls intentionally (e.g., listing a file read before a grep that might depend on it), and while the orchestrator cannot determine whether the ordering is meaningful, preserving it within concurrent batches provides a reasonable default. Within a concurrent batch, tools _start_ in list order but may _complete_ in any order depending on execution time.

Note the algorithm's simplicity: it is a single linear scan, not a graph analysis. It does not attempt to identify data dependencies between tool calls or build an optimal execution DAG. The sequential-batches approach uses position in the list as a proxy for dependency. A more sophisticated algorithm would need dependency information that the model does not provide.

### Bounded Concurrency

Concurrent batches do not run all tools simultaneously without limit. The `getMaxToolUseConcurrency()` function returns a concurrency cap — defaulting to 10, configurable via an environment variable.

The implementation uses an `all()` utility: a bounded async generator combiner with a semaphore. It accepts an array of async generators (one per tool) and a concurrency limit, and yields results from whichever generator completes next, ensuring no more than `limit` generators are active at any time:

```typescript
// src/toolOrchestration.ts — bounded concurrency (conceptual)
async function* executeBatch(
  batch: ToolUseBlock[],
  context: ToolUseContext
): AsyncGenerator<ToolResult> {
  const maxConcurrency = getMaxToolUseConcurrency()
  const generators = batch.map(block => executeOneTool(block, context))
  yield* all(generators, maxConcurrency)
}
```

The `all()` function works as follows: it starts up to `maxConcurrency` generators immediately. Each time a generator yields a value, that value is forwarded to the caller. When a generator completes, the semaphore releases a slot and the next pending generator (if any) is started. This produces a steady stream of results with bounded resource usage.

The semaphore pattern prevents resource exhaustion. Without it, a model response requesting 20 concurrent file reads could overwhelm the file descriptor limit or saturate disk I/O. A cap of 10 provides meaningful parallelism while staying within typical OS resource limits. Setting the cap to 1 via the environment variable effectively disables concurrency, which can be useful for debugging tool interactions.

### The Per-Tool Pipeline

Each individual tool invocation passes through a 13-step pipeline inside `executeOneTool()`. The steps, in order:

1. **Abort check**: If the user has cancelled the operation (via Ctrl+C or equivalent), skip immediately. The abort signal is checked before any work begins to avoid wasting cycles on a cancelled request.

2. **Tool lookup**: Resolve the tool name to a `Tool` instance from the assembled pool. If the name is not found, check aliases and fuzzy-match via `searchHint` before returning an error to the model.

3. **Input validation**: Validate the model's input against the tool's `inputSchema`. If `strict` mode is enabled, reject any extraneous fields. If strict mode is off, silently ignore extra fields and apply defaults for missing optional fields.

4. **Behavioral classification**: Call `isReadOnly(input)` and `isConcurrencySafe(input)`. These results were already used during partitioning, but they are re-evaluated here because hooks (step 6) can modify the input, potentially changing the classification.

5. **Permission check**: Call `checkPermissions(input, context)`. If the result is `deny`, return a structured error result to the model explaining that the tool call was not permitted. If the result is `ask`, display a permission prompt to the user and wait for their response. If `allow`, proceed.

6. **PreToolUse hooks**: Run any registered `PreToolUse` hooks. Each hook receives the tool name, input, and context. A hook can return one of three outcomes (detailed below). If any hook blocks the call, execution stops and the block reason is returned to the model.

7. **Execution**: Call `tool.call(input, context)`, consuming the async generator. Intermediate yields (progress events) are forwarded to the UI. The final yield is the tool result.

8. **Result yielding**: Yield the tool result back to the orchestration layer, which forwards it to the agent loop for inclusion in the conversation.

9. **PostToolUse hooks**: Run any registered `PostToolUse` hooks, which receive the tool name, input, result, and context. These hooks can observe but not modify the result. Common uses include logging and analytics.

10. **PostToolUseFailure hooks**: If the tool threw an error during execution, run failure-specific hooks. These are separate from PostToolUse hooks to allow different handling of success and failure cases (e.g., alerting on failures but not on successes).

11. **Context modifiers**: Process any `MessageUpdate` objects returned by the tool (explained below).

12. **Telemetry**: Record execution time, success/failure status, tool name, and input size. This data feeds into performance monitoring and usage analytics.

13. **Error wrapping**: If any step threw an unhandled exception, wrap it into a structured error result. The model receives a message like "Tool execution failed: [error message]" rather than experiencing a crash or timeout. This ensures the model can attempt to recover (e.g., by trying a different approach) rather than the entire loop failing.

### PreToolUse Hooks in Detail

PreToolUse hooks are the primary extension point for controlling tool behavior. A hook receives the tool name, the input, and the context, and returns one of three outcomes:

- **Allow**: The tool call proceeds as normal. The hook had no objection.
- **Block**: The tool call is prevented. The hook provides a reason string that is returned to the model as the tool result, so the model understands why the call was blocked and can adjust its approach.
- **Modify**: The hook returns a modified input object. The tool call proceeds with the modified input. This allows hooks to rewrite arguments — for example, converting relative paths to absolute paths, or adding default flags to a shell command.

This mechanism enables a range of use cases:

- Blocking all writes to files matching `*.lock` (a project-specific safety rule).
- Rewriting file paths to enforce a sandbox directory (all writes go to a shadow copy).
- Logging every shell command before execution (for audit trails).
- Injecting environment variables into shell commands.
- Rate-limiting tool calls (blocking if a tool has been called too frequently).

Hooks are configured by the user in their project's settings, not built into Claude Code. They run as external processes (typically shell scripts or small programs), which provides language-agnostic extensibility at the cost of subprocess overhead per tool call. The subprocess receives the hook input as JSON on stdin and returns its response as JSON on stdout.

### MessageUpdate: Lazy Context Modification

Some tool results need to modify the conversation state beyond simply appending a result. For example, a tool that changes the current working directory needs to update the environment section of the system prompt so the model sees the new path. A tool that installs a new MCP server needs to update the tool pool and the MCP instructions section of the prompt.

Rather than giving tools direct write access to the conversation state (which would create complex ownership issues and race conditions in concurrent batches), tools return `MessageUpdate` objects alongside their results. A `MessageUpdate` is a declarative instruction — "set the CWD to /foo/bar" or "add this MCP tool to the pool" — rather than an imperative mutation.

The `MessageUpdate` type supports several kinds of updates:

- **System prompt updates**: Changes to environment information, CLAUDE.md content, or other dynamic prompt sections.
- **Tool pool modifications**: Adding or removing tools (typically triggered by MCP server connections or disconnections).
- **Message modifications**: Retroactive changes to earlier messages in the conversation (rare, but used by context management tools).

The orchestration layer collects `MessageUpdate` objects from all tool results in a batch and processes them _after_ the entire batch completes. This ensures that updates from concurrent tools are applied in a deterministic order (by tool call index in the original list) rather than in completion order (which is non-deterministic). Deterministic ordering prevents subtle bugs where the final state depends on which tool happened to finish first.

This approach keeps tools stateless from their own perspective while still allowing them to influence the broader conversation context. A tool does not need to know about the conversation data structure; it just returns a declarative update and the orchestration layer handles the rest.

## Key Design Decisions

**Partitioning based on per-invocation flags, not per-tool flags.** Because `isConcurrencySafe` is a function of the input (see Chapter 2), the partitioning logic accurately reflects the safety of each specific invocation. A tool that is usually safe but occasionally unsafe (like Bash) gets the right treatment in each case without requiring the orchestrator to special-case individual tools.

**Sequential batch execution, concurrent within batches.** This is a conservative strategy. A more aggressive approach would analyze data dependencies between tool calls and build a DAG, potentially running independent unsafe operations in parallel. But the model provides no explicit dependency information, and inferring dependencies from tool inputs (e.g., checking whether two file writes target the same path) would be fragile and incomplete. The sequential-batches approach uses ordering as a proxy for dependency. It sacrifices some potential parallelism for correctness — an acceptable tradeoff for a system where incorrect results are far more costly than a few hundred milliseconds of extra latency.

**Bounded concurrency with configurable limit.** The default of 10 is a pragmatic choice. Most model responses contain 1-5 tool calls, so the cap rarely constrains real workloads. But when it does (e.g., a model reading 15 files at once), the bound prevents resource exhaustion. Making it configurable via environment variable lets power users tune it for their hardware without requiring a code change.

**Hooks as external processes.** Running hooks as subprocesses rather than in-process callbacks means hooks cannot crash Claude Code, cannot access internal state, and can be written in any language. The tradeoff is latency: each hook invocation pays subprocess spawn overhead (~5-20ms). For the typical case of 0-2 hooks per tool call, this is acceptable. For pathological cases (many hooks per call), the overhead becomes significant, but this is a user-controlled configuration.

**13 steps per tool, not fewer.** The pipeline may seem over-decomposed — could abort check, validation, and classification be a single step? They could, but separating them provides clearer error messages, more precise telemetry, and easier testing. Each step has a distinct failure mode and a distinct recovery action. Collapsing them would obscure which step failed and why.

## In Practice

A typical interaction that exercises orchestration: the user asks Claude Code to refactor a function. The model responds with five tool calls: three file reads (to understand the current code), one file write (the refactored code), and one Bash call (to run tests).

The orchestrator partitions these into three batches: the three reads run concurrently (~100ms total instead of ~300ms serial), then the write runs alone (possibly with a permission prompt), then the test runs alone. If the user has configured a PreToolUse hook that logs all file writes, it fires before the write step, adding perhaps 10ms of overhead. A PostToolUse hook configured to notify a Slack channel on test failures would fire after the Bash tool completes, but only if the exit code indicates failure.

The user sees tool calls completing in rapid succession for the reads, a brief pause for the write (with a permission prompt if not auto-approved), and then test output streaming in. The orchestration is invisible except in its effects: things happen faster than they would sequentially, safety constraints are never violated, and user-defined hooks run at the right moments.

## Summary

- `runTools()` partitions tool calls into batches via a linear scan: consecutive concurrency-safe calls run in parallel; unsafe calls form singleton serial batches.
- Concurrency within batches is bounded by a configurable limit (default 10) using a semaphore-based `all()` async generator combiner.
- Each tool invocation passes through a 13-step pipeline covering abort checks, validation, permissions, hooks, execution, telemetry, and error handling.
- PreToolUse hooks can block, allow, or modify tool calls; PostToolUse and PostToolUseFailure hooks observe results. All hooks run as isolated subprocesses.
- `MessageUpdate` objects allow tools to request context modifications declaratively, processed in deterministic order after batch completion.
