# s03: Tool Orchestration

`s01 > s02 > [ s03 ] s04 > s05`

> *"Read-only tools fan out, write tools go single-file"* -- partition, then run each batch with the right strategy.

## Problem

When the model returns multiple tool calls in one response (e.g. "read these 3 files"), running them one by one wastes time. But running everything in parallel is dangerous -- two concurrent writes to the same file will race. The orchestrator needs to know which tools are safe to parallelize and which must run serially.

## Solution

```
  Model returns: [Read, Grep, Read, Write, Glob, Read]

  partition_tool_calls() groups by concurrency safety:

  +---------------------+     +---------+     +----------------+
  | Batch 0: CONCURRENT |     | Batch 1 |     | Batch 2:       |
  | Read, Grep, Read    | --> | SERIAL  | --> | CONCURRENT     |
  | (all read-only)     |     | Write   |     | Glob, Read     |
  +---------------------+     +---------+     +----------------+
       semaphore(10)           one-by-one        semaphore(10)
         ~0.4s                   ~0.5s              ~0.3s

  Total: ~1.2s  (vs ~2.0s sequential)
```

One partitioner, one semaphore cap, three batches. Real code: `src/services/tools/toolOrchestration.ts`.

## How It Works

**1. Partition tool calls into batches.**

Scan left-to-right. Consecutive concurrency-safe calls merge into one batch. Each unsafe call gets its own:

```python
def partition_tool_calls(blocks, tools) -> list[Batch]:
    batches = []
    for block in blocks:
        tool = tools.get(block.name)
        is_safe = tool and tool.is_concurrency_safe(block.input)

        if is_safe and batches and batches[-1].is_concurrent:
            batches[-1].blocks.append(block)  # merge
        else:
            batches.append(Batch(is_concurrent=is_safe, blocks=[block]))
    return batches
```

If `is_concurrency_safe()` throws, the tool gets its own serial batch. Fail-closed, same as real code.

**2. Run concurrent batches with a semaphore cap (default 10).**

```python
async def run_concurrent(blocks, tools, ctx, max_conc=10):
    sem = asyncio.Semaphore(max_conc)

    async def run_one(block):
        async with sem:
            result = await tools[block.name].execute(block.input, ctx)
            return (block.id, result)

    tasks = [asyncio.create_task(run_one(b)) for b in blocks]
    for coro in asyncio.as_completed(tasks):
        block_id, result = await coro
        yield {"tool_use_id": block_id, "result": result}
```

Cap is configurable via `CLAUDE_CODE_MAX_TOOL_USE_CONCURRENCY`. Real code uses a `Promise.race` pattern in `src/utils/generators.ts`.

**3. Run serial batches one by one.**

```python
async def run_serial(blocks, tools, ctx):
    for block in blocks:
        result = await tools[block.name].execute(block.input, ctx)
        yield {"tool_use_id": block.id, "result": result}
```

**4. Top-level orchestrator ties it together.**

```python
async def run_tools(blocks, tools, ctx):
    batches = partition_tool_calls(blocks, tools)
    for batch in batches:
        if batch.is_concurrent:
            async for r in run_concurrent(batch.blocks, tools, ctx):
                yield r
        else:
            async for r in run_serial(batch.blocks, tools, ctx):
                yield r
```

Each tool invocation also flows through pre-hooks and post-hooks (logging, permission checks, output size warnings) before reaching the tool's `call()` method. Real code: `src/services/tools/toolExecution.ts`.

## What Changed

| Component | Before (s02) | After (s03) |
|---|---|---|
| Execution model | Sequential for-loop | Partitioned batches: concurrent + serial |
| Concurrency | None | Semaphore-capped (default 10) |
| Safety classification | Defined on Tool but unused | Drives partitioning decisions |
| Hooks | None | Pre/post hooks per tool invocation |
| Result streaming | Collect all, return list | Async generator yields as each finishes |

## Try It

```bash
cd learn-the-actual-claude-code
python agents/s03_tool_orchestration.py
```

Watch the output for:

- 3 batches formed from 6 tool calls (concurrent / serial / concurrent)
- Concurrent batch results arriving out of order (whichever finishes first)
- Total wall time significantly less than sequential sum (~1.2s vs ~2.0s)
