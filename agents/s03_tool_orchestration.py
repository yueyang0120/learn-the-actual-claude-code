"""
Session 03 -- Tool Orchestration Reimplementation

A runnable Python reimplementation of Claude Code's tool orchestration layer,
demonstrating:
  - Partitioning tool calls into concurrent (read-only) and serial (write) batches
  - Bounded-concurrency fan-out via asyncio.Semaphore
  - Pre/post tool hooks
  - Async generator streaming of results

Based on:
  src/services/tools/toolOrchestration.ts
  src/services/tools/toolExecution.ts
  src/services/tools/toolHooks.ts
  src/utils/generators.ts

Run directly:
  python reimplementation.py
"""

from __future__ import annotations

import asyncio
import os
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, AsyncGenerator, Callable, Awaitable

# ---------------------------------------------------------------------------
# Imports from shared lib (built in sessions 01-02)
# ---------------------------------------------------------------------------
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from lib.types import ToolUseBlock, ToolResultBlock, ToolUseContext


# =====================================================================
# Core types
# =====================================================================

@dataclass
class MessageUpdate:
    """Yielded by the orchestrator back to the agent loop."""
    tool_use_id: str | None = None
    result: str | None = None
    is_error: bool = False
    context: ToolUseContext | None = None


# Hook callables: receive (tool_name, tool_input) and return optional message
PreHook = Callable[[str, dict[str, Any]], Awaitable[str | None]]
PostHook = Callable[[str, dict[str, Any], str], Awaitable[str | None]]


# =====================================================================
# Tool definition with concurrency-safety flag
# =====================================================================

@dataclass
class Tool:
    """
    Mirrors src/Tool.ts -- each tool declares whether it is concurrency-safe.
    In the real code, isConcurrencySafe can depend on the parsed input
    (e.g., BashTool checks if the command is read-only).
    """
    name: str
    is_concurrency_safe: Callable[[dict[str, Any]], bool] = lambda _: False
    execute: Callable[[dict[str, Any], ToolUseContext], Awaitable[str]] = None  # type: ignore[assignment]


# =====================================================================
# Partitioning -- mirrors partitionToolCalls()
# =====================================================================

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
            # Merge into existing concurrent batch
            batches[-1].blocks.append(block)
        else:
            batches.append(Batch(is_concurrent=is_safe, blocks=[block]))

    return batches


# =====================================================================
# Max concurrency -- mirrors getMaxToolUseConcurrency()
# =====================================================================

def get_max_concurrency() -> int:
    """Read from env var, default 10. Mirrors the real implementation."""
    raw = os.environ.get("CLAUDE_CODE_MAX_TOOL_USE_CONCURRENCY", "")
    try:
        return int(raw)
    except ValueError:
        return 10


# =====================================================================
# Tool execution with hooks
# =====================================================================

async def execute_single_tool(
    tool: Tool,
    block: ToolUseBlock,
    context: ToolUseContext,
    pre_hooks: list[PreHook],
    post_hooks: list[PostHook],
) -> AsyncGenerator[MessageUpdate, None]:
    """
    Execute a single tool with pre/post hooks.
    Mirrors the pipeline in toolExecution.ts:
      1. Check abort
      2. Run pre-hooks
      3. Execute tool
      4. Run post-hooks
      5. Yield result
    """
    # 1. Check abort
    if context.abort:
        yield MessageUpdate(
            tool_use_id=block.id,
            result="Cancelled",
            is_error=True,
        )
        return

    # 2. Pre-hooks
    for hook in pre_hooks:
        hook_msg = await hook(block.name, block.input)
        if hook_msg is not None:
            yield MessageUpdate(
                tool_use_id=block.id,
                result=f"[pre-hook] {hook_msg}",
            )
            # In real code, hooks can deny execution. We simulate that:
            if hook_msg.startswith("DENY:"):
                yield MessageUpdate(
                    tool_use_id=block.id,
                    result=hook_msg,
                    is_error=True,
                )
                return

    # 3. Execute
    try:
        result = await tool.execute(block.input, context)
    except Exception as exc:
        # Post-failure hooks would run here in real code
        yield MessageUpdate(
            tool_use_id=block.id,
            result=f"Error: {exc}",
            is_error=True,
        )
        return

    # 4. Post-hooks
    for hook in post_hooks:
        hook_msg = await hook(block.name, block.input, result)
        if hook_msg is not None:
            yield MessageUpdate(
                tool_use_id=block.id,
                result=f"[post-hook] {hook_msg}",
            )

    # 5. Yield final result
    yield MessageUpdate(tool_use_id=block.id, result=result)


# =====================================================================
# Concurrent runner -- mirrors runToolsConcurrently + all()
# =====================================================================

async def run_tools_concurrently(
    blocks: list[ToolUseBlock],
    tools: dict[str, Tool],
    context: ToolUseContext,
    pre_hooks: list[PreHook],
    post_hooks: list[PostHook],
    max_concurrency: int,
) -> AsyncGenerator[MessageUpdate, None]:
    """
    Run multiple concurrency-safe tools with bounded parallelism.

    Mirrors the all() utility in src/utils/generators.ts which uses
    Promise.race over async generators with a concurrency cap.
    We use asyncio.Semaphore + asyncio.Queue for the same effect.
    """
    queue: asyncio.Queue[MessageUpdate | None] = asyncio.Queue()
    semaphore = asyncio.Semaphore(max_concurrency)

    async def run_one(block: ToolUseBlock) -> None:
        async with semaphore:
            tool = tools[block.name]
            async for update in execute_single_tool(
                tool, block, context, pre_hooks, post_hooks,
            ):
                await queue.put(update)

    tasks = [asyncio.create_task(run_one(b)) for b in blocks]

    async def sentinel() -> None:
        """Put None when all tasks finish to signal completion."""
        await asyncio.gather(*tasks)
        await queue.put(None)

    sentinel_task = asyncio.create_task(sentinel())

    while True:
        update = await queue.get()
        if update is None:
            break
        yield update

    await sentinel_task  # Ensure cleanup


# =====================================================================
# Serial runner -- mirrors runToolsSerially
# =====================================================================

async def run_tools_serially(
    blocks: list[ToolUseBlock],
    tools: dict[str, Tool],
    context: ToolUseContext,
    pre_hooks: list[PreHook],
    post_hooks: list[PostHook],
) -> AsyncGenerator[MessageUpdate, None]:
    """Run tools one-by-one, applying context changes immediately."""
    for block in blocks:
        tool = tools.get(block.name)
        if tool is None:
            yield MessageUpdate(
                tool_use_id=block.id,
                result=f"Error: No such tool: {block.name}",
                is_error=True,
            )
            continue
        async for update in execute_single_tool(
            tool, block, context, pre_hooks, post_hooks,
        ):
            yield update


# =====================================================================
# Top-level orchestrator -- mirrors runTools()
# =====================================================================

async def run_tools(
    tool_use_blocks: list[ToolUseBlock],
    tools: dict[str, Tool],
    context: ToolUseContext,
    pre_hooks: list[PreHook] | None = None,
    post_hooks: list[PostHook] | None = None,
) -> AsyncGenerator[MessageUpdate, None]:
    """
    Main orchestrator. Partitions tool calls into concurrent and serial
    batches, then executes each batch appropriately.

    Mirrors src/services/tools/toolOrchestration.ts:runTools
    """
    pre = pre_hooks or []
    post = post_hooks or []
    max_conc = get_max_concurrency()
    batches = partition_tool_calls(tool_use_blocks, tools)

    print(f"\n{'='*60}")
    print(f"Orchestrator: {len(tool_use_blocks)} tool calls -> "
          f"{len(batches)} batches (max concurrency: {max_conc})")
    for i, batch in enumerate(batches):
        mode = "CONCURRENT" if batch.is_concurrent else "SERIAL"
        names = [b.name for b in batch.blocks]
        print(f"  Batch {i}: [{mode}] {names}")
    print(f"{'='*60}\n")

    for batch in batches:
        if batch.is_concurrent:
            async for update in run_tools_concurrently(
                batch.blocks, tools, context, pre, post, max_conc,
            ):
                yield update
        else:
            async for update in run_tools_serially(
                batch.blocks, tools, context, pre, post,
            ):
                yield update


# =====================================================================
# Demo: simulated tools and execution
# =====================================================================

async def _sim_file_read(input: dict[str, Any], ctx: ToolUseContext) -> str:
    """Simulate reading a file (takes 0.3s)."""
    await asyncio.sleep(0.3)
    return f"Contents of {input.get('path', '?')}: [... file data ...]"


async def _sim_grep(input: dict[str, Any], ctx: ToolUseContext) -> str:
    """Simulate grep search (takes 0.4s)."""
    await asyncio.sleep(0.4)
    return f"Found 3 matches for '{input.get('pattern', '?')}'"


async def _sim_glob(input: dict[str, Any], ctx: ToolUseContext) -> str:
    """Simulate glob (takes 0.2s)."""
    await asyncio.sleep(0.2)
    return f"Matched 12 files for '{input.get('pattern', '?')}'"


async def _sim_file_write(input: dict[str, Any], ctx: ToolUseContext) -> str:
    """Simulate writing a file (takes 0.5s)."""
    await asyncio.sleep(0.5)
    return f"Wrote {len(input.get('content', ''))} chars to {input.get('path', '?')}"


async def _sim_bash_readonly(input: dict[str, Any], ctx: ToolUseContext) -> str:
    """Simulate a read-only bash command (takes 0.3s)."""
    await asyncio.sleep(0.3)
    return f"$ {input.get('command', '?')}\n[output]"


async def _sim_bash_write(input: dict[str, Any], ctx: ToolUseContext) -> str:
    """Simulate a write bash command (takes 0.4s)."""
    await asyncio.sleep(0.4)
    return f"$ {input.get('command', '?')}\n[done]"


# Pre-hook: log every tool invocation
async def logging_pre_hook(tool_name: str, tool_input: dict[str, Any]) -> str | None:
    print(f"  [pre-hook] About to run: {tool_name}")
    return None  # Returning None means "don't interfere"


# Post-hook: warn on large outputs
async def size_check_post_hook(
    tool_name: str, tool_input: dict[str, Any], result: str,
) -> str | None:
    if len(result) > 10_000:
        return f"Warning: {tool_name} produced {len(result)} chars"
    return None


def make_block(name: str, input: dict[str, Any]) -> ToolUseBlock:
    return ToolUseBlock(
        id=f"toolu_{uuid.uuid4().hex[:12]}",
        name=name,
        input=input,
    )


async def main() -> None:
    # Define tools
    tools: dict[str, Tool] = {
        "FileRead": Tool(
            name="FileRead",
            is_concurrency_safe=lambda _: True,
            execute=_sim_file_read,
        ),
        "Grep": Tool(
            name="Grep",
            is_concurrency_safe=lambda _: True,
            execute=_sim_grep,
        ),
        "Glob": Tool(
            name="Glob",
            is_concurrency_safe=lambda _: True,
            execute=_sim_glob,
        ),
        "FileWrite": Tool(
            name="FileWrite",
            is_concurrency_safe=lambda _: False,  # Writes are never concurrent
            execute=_sim_file_write,
        ),
        "Bash": Tool(
            name="Bash",
            # Concurrency depends on input -- just like real BashTool
            is_concurrency_safe=lambda inp: inp.get("read_only", False),
            execute=lambda inp, ctx: (
                _sim_bash_readonly(inp, ctx) if inp.get("read_only")
                else _sim_bash_write(inp, ctx)
            ),
        ),
    }

    # Simulate a realistic tool call sequence from the model:
    # [Read, Grep, Read, Write, Glob, Read]
    # Expected partitioning:
    #   Batch 0: [CONCURRENT] FileRead, Grep, FileRead  (all read-only)
    #   Batch 1: [SERIAL]     FileWrite                  (write)
    #   Batch 2: [CONCURRENT] Glob, FileRead             (read-only)
    tool_calls = [
        make_block("FileRead", {"path": "src/main.ts"}),
        make_block("Grep", {"pattern": "TODO", "path": "."}),
        make_block("FileRead", {"path": "README.md"}),
        make_block("FileWrite", {"path": "out.txt", "content": "hello world"}),
        make_block("Glob", {"pattern": "**/*.py"}),
        make_block("FileRead", {"path": "config.json"}),
    ]

    context = ToolUseContext()
    pre_hooks = [logging_pre_hook]
    post_hooks = [size_check_post_hook]

    print("=" * 60)
    print("Session 03 -- Tool Orchestration Demo")
    print("=" * 60)
    print(f"\nTool calls from model: {[b.name for b in tool_calls]}")

    start = time.monotonic()

    async for update in run_tools(tool_calls, tools, context, pre_hooks, post_hooks):
        elapsed = time.monotonic() - start
        status = "ERROR" if update.is_error else "OK"
        print(f"  [{elapsed:5.2f}s] {update.tool_use_id}: [{status}] {update.result}")

    total = time.monotonic() - start
    print(f"\n{'='*60}")
    print(f"Total time: {total:.2f}s")
    print(f"Sequential would be: ~2.1s (0.3+0.4+0.3+0.5+0.2+0.3)")
    print(f"Concurrent batching saves: ~{max(0, 2.1 - total):.1f}s")
    print(f"{'='*60}")


if __name__ == "__main__":
    asyncio.run(main())
