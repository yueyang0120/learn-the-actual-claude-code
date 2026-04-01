# Session 03 -- Tool Orchestration

## What this session covers

How Claude Code decides which tool calls run concurrently and which run
serially, how results stream back through async generators, and how pre/post
hooks plug into the execution pipeline.

## Learning objectives

1. Understand the `runTools()` async generator and its yield-based streaming
   pattern for feeding results back to the agent loop.
2. Learn how `partitionToolCalls()` splits a batch of tool-use blocks into
   **read-only concurrent** groups and **write serial** singletons.
3. See how the `all()` utility implements bounded-concurrency fan-out over
   async generators (default cap: 10, configurable via env var).
4. Trace the full execution path: orchestration -> permission check -> pre-hook
   -> tool call -> post-hook -> result message.
5. Understand context modifiers -- how concurrent tools queue their context
   changes to be applied after the batch, while serial tools apply them
   immediately.

## Source files covered

| File | Purpose |
|------|---------|
| `src/services/tools/toolOrchestration.ts` | Top-level orchestrator: `runTools`, `partitionToolCalls`, serial/concurrent runners |
| `src/services/tools/toolExecution.ts` | Per-tool execution: permission checks, input validation, progress streaming, error handling |
| `src/services/tools/toolHooks.ts` | Pre/post tool-use hooks: permission overrides, blocking errors, additional context injection |
| `src/services/tools/StreamingToolExecutor.ts` | Streaming variant that executes tools as they arrive (used during response streaming) |
| `src/utils/generators.ts` | `all()` -- bounded-concurrency async generator combiner |
| `src/Tool.ts` | `isConcurrencySafe` method on the Tool interface (default: false) |

## What shareAI-lab and similar clones miss

Most open-source Claude Code clones dispatch tool calls **sequentially in a
flat loop**: receive all tool_use blocks, execute them one-by-one, collect
results, send them back. This works but leaves significant performance on
the table.

The real Claude Code does three things they skip:

1. **Partitioning into concurrent vs. serial batches.**
   `partitionToolCalls()` inspects each tool's `isConcurrencySafe(input)` method
   (which may depend on the parsed input -- e.g., BashTool is only concurrency-safe
   when the command is read-only). Consecutive concurrency-safe tools are grouped
   into a single batch; everything else becomes a batch of one.

2. **Bounded fan-out with `all()`.**
   Concurrent batches run through a custom async-generator combiner that caps
   parallelism at `CLAUDE_CODE_MAX_TOOL_USE_CONCURRENCY` (default 10). This is
   not `Promise.all` -- it is a pull-based scheduler that starts new generators
   as earlier ones finish, yielding results as they arrive.

3. **Deferred context application for concurrent batches.**
   When tools run concurrently, their context modifiers (e.g., "I read file X,
   add it to the cache") are queued and applied in deterministic order *after*
   the entire batch completes. Serial tools apply context changes immediately
   between each execution. This prevents race conditions on shared mutable state.

4. **Streaming tool executor.**
   `StreamingToolExecutor` starts executing tools *as they stream in* from the
   model response, before the full response is even received. It maintains an
   ordered buffer so results are emitted in the original tool-call order even
   though execution may complete out of order.

## Reimplementation

`reimplementation.py` is a runnable Python (~250 LOC) that demonstrates the
partition-and-fan-out pattern with pre/post hooks. Run it directly:

```bash
cd sessions/s03-tool-orchestration
python reimplementation.py
```

No API key needed -- it uses simulated tools to show timing behavior.
