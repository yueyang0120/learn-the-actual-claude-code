# Session 03 -- Tool Orchestration

`s01 > s02 > [ s03 ] s04 > s05 | s06 > s07 > s08 > s09 > s10 | s11 > s12 > s13 > s14`

> "Read-only tools run concurrently (max 10), write tools run serially. partitionToolCalls() + bounded fan-out."
>
> *Harness layer: `agents/s03_tool_orchestration.py` reimplements partitioning, bounded concurrency, and pre/post hooks in ~450 lines of Python. Run it with simulated tools and watch batches execute in parallel.*

---

## Problem

When the model returns multiple tool calls in a single response (which it often does -- "read these 3 files"), a naive agent executes them one by one. That wastes time. But blindly running everything in parallel is dangerous: two concurrent file writes to the same path will race.

Claude Code needs a system that:
- Runs read-only tools **concurrently** for speed
- Runs write tools **serially** for safety
- Caps concurrency to avoid resource exhaustion
- Preserves ordering guarantees the model expects
- Applies pre/post hooks (logging, permission checks, output size warnings)

---

## Solution

The orchestrator partitions tool calls into **batches**, then executes each batch with the appropriate strategy:

```
  Model returns: [Read, Grep, Read, Write, Glob, Read]

  partitionToolCalls() groups them:

  +---------------------------+     +-----------+     +------------------+
  | Batch 0: CONCURRENT       |     | Batch 1:  |     | Batch 2:         |
  | Read, Grep, Read          | --> | SERIAL    | --> | CONCURRENT       |
  | (all concurrency-safe)    |     | Write     |     | Glob, Read       |
  +---------------------------+     +-----------+     +------------------+

  Batch 0: fan-out with semaphore(10)  ~0.4s
  Batch 1: single execution            ~0.5s
  Batch 2: fan-out with semaphore(10)  ~0.3s

  Total: ~1.2s  (vs ~2.1s sequential)
```

---

## How It Works

### 1. Partitioning: `partitionToolCalls()`

The partitioner scans tool calls left-to-right. Consecutive concurrency-safe calls merge into one batch; each unsafe call gets its own batch:

```python
@dataclass
class Batch:
    is_concurrent: bool
    blocks: list[ToolUseBlock]


def partition_tool_calls(
    tool_use_blocks: list[ToolUseBlock],
    tools: dict[str, Tool],
) -> list[Batch]:
    """
    Partition tool calls into batches:
      - Consecutive concurrency-safe tools -> one concurrent batch
      - Each non-concurrency-safe tool -> its own serial batch

    Mirrors src/services/tools/toolOrchestration.ts:partitionToolCalls
    """
    batches: list[Batch] = []

    for block in tool_use_blocks:
        tool = tools.get(block.name)
        try:
            is_safe = bool(tool and tool.is_concurrency_safe(block.input))
        except Exception:
            is_safe = False  # Conservative fallback, same as real code

        if is_safe and batches and batches[-1].is_concurrent:
            batches[-1].blocks.append(block)
        else:
            batches.append(Batch(is_concurrent=is_safe, blocks=[block]))

    return batches
```

Notice the **conservative fallback**: if `is_concurrency_safe()` throws, the tool gets its own serial batch. Fail-closed.

### 2. Bounded concurrency with semaphore

Concurrent batches fan out with a cap (default 10, configurable via `CLAUDE_CODE_MAX_TOOL_USE_CONCURRENCY`):

```python
def get_max_concurrency() -> int:
    """Read from env var, default 10. Mirrors the real implementation."""
    raw = os.environ.get("CLAUDE_CODE_MAX_TOOL_USE_CONCURRENCY", "")
    try:
        return int(raw)
    except ValueError:
        return 10


async def run_tools_concurrently(
    blocks, tools, context, pre_hooks, post_hooks, max_concurrency,
) -> AsyncGenerator[MessageUpdate, None]:
    """
    Mirrors the all() utility in src/utils/generators.ts which uses
    Promise.race over async generators with a concurrency cap.
    """
    queue: asyncio.Queue[MessageUpdate | None] = asyncio.Queue()
    semaphore = asyncio.Semaphore(max_concurrency)

    async def run_one(block):
        async with semaphore:
            tool = tools[block.name]
            async for update in execute_single_tool(
                tool, block, context, pre_hooks, post_hooks,
            ):
                await queue.put(update)

    tasks = [asyncio.create_task(run_one(b)) for b in blocks]

    async def sentinel():
        await asyncio.gather(*tasks)
        await queue.put(None)

    sentinel_task = asyncio.create_task(sentinel())

    while True:
        update = await queue.get()
        if update is None:
            break
        yield update
```

### 3. Single tool execution with hooks

Each tool invocation flows through pre-hooks, execution, and post-hooks:

```python
async def execute_single_tool(
    tool, block, context, pre_hooks, post_hooks,
) -> AsyncGenerator[MessageUpdate, None]:
    """
    Pipeline mirrors toolExecution.ts:
      1. Check abort
      2. Run pre-hooks
      3. Execute tool
      4. Run post-hooks
      5. Yield result
    """
    if context.abort:
        yield MessageUpdate(tool_use_id=block.id, result="Cancelled", is_error=True)
        return

    for hook in pre_hooks:
        hook_msg = await hook(block.name, block.input)
        if hook_msg is not None:
            yield MessageUpdate(tool_use_id=block.id, result=f"[pre-hook] {hook_msg}")
            if hook_msg.startswith("DENY:"):
                yield MessageUpdate(tool_use_id=block.id, result=hook_msg, is_error=True)
                return

    try:
        result = await tool.execute(block.input, context)
    except Exception as exc:
        yield MessageUpdate(tool_use_id=block.id, result=f"Error: {exc}", is_error=True)
        return

    for hook in post_hooks:
        hook_msg = await hook(block.name, block.input, result)
        if hook_msg is not None:
            yield MessageUpdate(tool_use_id=block.id, result=f"[post-hook] {hook_msg}")

    yield MessageUpdate(tool_use_id=block.id, result=result)
```

### 4. The top-level orchestrator

Ties everything together -- partition, then run each batch with the right strategy:

```python
async def run_tools(
    tool_use_blocks, tools, context,
    pre_hooks=None, post_hooks=None,
) -> AsyncGenerator[MessageUpdate, None]:
    """
    Main orchestrator. Mirrors toolOrchestration.ts:runTools
    """
    max_conc = get_max_concurrency()
    batches = partition_tool_calls(tool_use_blocks, tools)

    for batch in batches:
        if batch.is_concurrent:
            async for update in run_tools_concurrently(
                batch.blocks, tools, context, pre_hooks, post_hooks, max_conc,
            ):
                yield update
        else:
            async for update in run_tools_serially(
                batch.blocks, tools, context, pre_hooks, post_hooks,
            ):
                yield update
```

---

## What Changed

| Component | Before (tutorial style) | After (Claude Code) |
|---|---|---|
| Execution model | Sequential `for tool in tools` | Partitioned batches: concurrent + serial |
| Concurrency | None or unbounded | Semaphore-capped (default 10) |
| Safety classification | N/A | Per-call `isConcurrencySafe(input)` |
| Write ordering | Accidental | Guaranteed serial within batch |
| Hooks | None | Pre/post hooks per tool invocation |
| Result streaming | Collect all, return list | Async generator yields as they finish |
| Error handling | Crash | Fail-closed: exceptions become error results |

---

## Try It

```bash
cd agents
python s03_tool_orchestration.py
```

The demo simulates 6 tool calls `[FileRead, Grep, FileRead, FileWrite, Glob, FileRead]` with artificial delays. Watch the output to see:

- 3 batches formed (concurrent / serial / concurrent)
- Concurrent batch results arriving out of order (whichever finishes first)
- Total wall time significantly less than sequential sum
- Pre-hooks logging each invocation

Expected output:

```
Tool calls from model: ['FileRead', 'Grep', 'FileRead', 'FileWrite', 'Glob', 'FileRead']

Orchestrator: 6 tool calls -> 3 batches (max concurrency: 10)
  Batch 0: [CONCURRENT] ['FileRead', 'Grep', 'FileRead']
  Batch 1: [SERIAL]     ['FileWrite']
  Batch 2: [CONCURRENT] ['Glob', 'FileRead']

Total time: ~1.2s
Sequential would be: ~2.1s
```

**Source files to explore next:**
- `src/services/tools/toolOrchestration.ts` -- `partitionToolCalls()`, `runTools()`
- `src/services/tools/toolExecution.ts` -- single tool execution pipeline
- `src/services/tools/toolHooks.ts` -- pre/post hook system
- `src/utils/generators.ts` -- the `all()` bounded-concurrency utility
