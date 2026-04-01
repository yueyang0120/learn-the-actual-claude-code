# Session 03 -- 工具编排

`s01 > s02 > [ s03 ] s04 > s05 | s06 > s07 > s08 > s09 > s10 | s11 > s12 > s13 > s14`

> "Read-only tools run concurrently (max 10), write tools run serially. partitionToolCalls() + bounded fan-out."
> "只读工具并发执行（最多 10 个），写入工具串行执行。partitionToolCalls() + 有界扇出。"
>
> *实践层：`agents/s03_tool_orchestration.py` 用约 450 行 Python 重新实现了分区、有界并发和前/后钩子。运行它，用模拟工具观察批次的并行执行。*

---

## 问题

当模型在单次响应中返回多个工具调用时（这很常见 —— 比如"读取这 3 个文件"），简单的 Agent 会逐个执行。这浪费时间。但盲目地并行执行所有工具是危险的：两个并发的文件写入操作如果针对同一路径就会产生竞争。

Claude Code 需要一个系统来：
- **并发**运行只读工具以提升速度
- **串行**运行写入工具以保证安全
- 限制并发度以避免资源耗尽
- 保持模型期望的顺序保证
- 应用前/后钩子（日志、权限检查、输出大小警告）

---

## 解决方案

编排器将工具调用分区为**批次**，然后使用适当的策略执行每个批次：

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

## 工作原理

### 1. 分区：`partitionToolCalls()`

分区器从左到右扫描工具调用。连续的并发安全调用合并为一个批次；每个非安全调用独占一个批次：

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

注意**保守回退**：如果 `is_concurrency_safe()` 抛出异常，该工具会获得自己的串行批次。失败即关闭。

### 2. 使用信号量的有界并发

并发批次通过上限进行扇出（默认 10，可通过 `CLAUDE_CODE_MAX_TOOL_USE_CONCURRENCY` 环境变量配置）：

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

### 3. 带钩子的单工具执行

每次工具调用都经过前置钩子、执行和后置钩子的流程：

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

### 4. 顶层编排器

将所有部分串联起来 —— 分区，然后用正确的策略运行每个批次：

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

## 变化对比

| 组件 | 之前（教程风格） | 之后（Claude Code） |
|---|---|---|
| 执行模型 | 顺序执行 `for tool in tools` | 分区批次：并发 + 串行 |
| 并发控制 | 无或无限制 | 信号量限制（默认 10） |
| 安全分类 | 不适用 | 每次调用的 `isConcurrencySafe(input)` |
| 写入顺序 | 偶然的 | 批次内保证串行 |
| 钩子 | 无 | 每次工具调用的前/后钩子 |
| 结果流式传输 | 全部收集后返回列表 | 异步生成器，完成即 yield |
| 错误处理 | 崩溃 | 失败即关闭：异常变为错误结果 |

---

## 试一试

```bash
cd agents
python s03_tool_orchestration.py
```

演示模拟了 6 个工具调用 `[FileRead, Grep, FileRead, FileWrite, Glob, FileRead]`，带有人工延迟。观察输出可以看到：

- 形成了 3 个批次（并发 / 串行 / 并发）
- 并发批次的结果乱序到达（哪个先完成先输出）
- 总耗时显著少于顺序执行的总和
- 前置钩子记录每次调用

预期输出：

```
Tool calls from model: ['FileRead', 'Grep', 'FileRead', 'FileWrite', 'Glob', 'FileRead']

Orchestrator: 6 tool calls -> 3 batches (max concurrency: 10)
  Batch 0: [CONCURRENT] ['FileRead', 'Grep', 'FileRead']
  Batch 1: [SERIAL]     ['FileWrite']
  Batch 2: [CONCURRENT] ['Glob', 'FileRead']

Total time: ~1.2s
Sequential would be: ~2.1s
```

**接下来可以探索的源文件：**
- `src/services/tools/toolOrchestration.ts` -- `partitionToolCalls()`、`runTools()`
- `src/services/tools/toolExecution.ts` -- 单工具执行流水线
- `src/services/tools/toolHooks.ts` -- 前/后钩子系统
- `src/utils/generators.ts` -- `all()` 有界并发工具函数
