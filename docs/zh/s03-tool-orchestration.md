# s03: Tool Orchestration

`s01 > s02 > [ s03 ] s04 > s05`

> *"Read-only tools fan out, write tools go single-file"* -- 先分区, 再用对的策略跑每个批次。

## 问题

模型一次响应返回多个工具调用时（比如"读这 3 个文件"）, 一个一个跑太慢。但全部并行又危险 -- 两个并发写操作碰到同一个文件就会竞争。编排器需要知道哪些工具可以并发, 哪些必须串行。

## 解决方案

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

一个分区器, 一个 semaphore 上限, 三个批次。真实代码：`src/services/tools/toolOrchestration.ts`。

## 工作原理

**1. 把工具调用分区成批次。**

从左到右扫描。连续的并发安全调用合并成一个批次, 每个不安全的调用单独成批：

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

如果 `is_concurrency_safe()` 抛异常, 这个工具会被放进自己的串行批次。Fail-closed, 跟真实代码一样。

**2. 并发批次用 semaphore 限制上限（默认 10）。**

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

上限可通过 `CLAUDE_CODE_MAX_TOOL_USE_CONCURRENCY` 配置。真实代码在 `src/utils/generators.ts` 里用 `Promise.race` 模式。

**3. 串行批次逐个跑。**

```python
async def run_serial(blocks, tools, ctx):
    for block in blocks:
        result = await tools[block.name].execute(block.input, ctx)
        yield {"tool_use_id": block.id, "result": result}
```

**4. 顶层编排器把所有东西串起来。**

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

每次工具调用还会经过 pre-hook 和 post-hook（日志、权限检查、输出大小警告）, 然后才到工具的 `call()` 方法。真实代码：`src/services/tools/toolExecution.ts`。

## 变更内容

| 组件 | 之前 (s02) | 之后 (s03) |
|---|---|---|
| 执行模型 | 顺序 for 循环 | 分区批次：并发 + 串行 |
| 并发控制 | 无 | semaphore 限制（默认 10） |
| 安全分类 | Tool 上定义了但没用 | 驱动分区决策 |
| 钩子 | 无 | 每次工具调用有 pre/post hook |
| 结果流式 | 全部收集, 返回列表 | async generator, 完成一个 yield 一个 |

## 试一试

```bash
cd learn-the-actual-claude-code
python agents/s03_tool_orchestration.py
```

注意观察：

- 6 个工具调用被分成 3 个批次（并发 / 串行 / 并发）
- 并发批次结果乱序到达（谁先完成谁先出来）
- 总耗时明显少于顺序执行的总和（~1.2s vs ~2.0s）
