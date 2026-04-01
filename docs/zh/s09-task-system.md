# s09: Task System

`s01 > s02 > s03 > s04 > s05 | s06 > s07 > s08 > [ s09 ] s10 | s11 > s12 > s13 > s14`

> "任务是一个带依赖图的状态机，不只是一个线程。"

## 问题

真实的编程工作需要并行。同时跑单元测试、集成测试和 linter，等全部跑完再看结果。如果一个任务依赖另一个，agent 需要知道顺序。如果一个任务卡住了，你得能杀掉它而不丢已经输出的内容。

## 解决方案

Claude Code 实现了一个任务系统 -- 带类型化 task ID、生命周期状态机、依赖 DAG，输出流式写入磁盘。

```
  Dependency DAG                    Lifecycle
  +---------+     +---------+
  | b1:unit |     | b2:integ|      pending -> running -> completed
  | tests   |     | tests   |                    |-----> failed
  +----+----+     +----+----+                    |-----> killed
       |               |
       +-------+-------+
               |
          +----v-----+
          | a1:review|
          | results  |
          +----------+
```

每个任务把输出写到专用文件。读取器用基于偏移量的增量读取来轮询 -- 类似 `tail -f`，但是结构化的。

## 工作原理

### 第 1 步：任务类型和生命周期

真实代码里有 7 种任务类型，每种都有前缀 ID 方便辨认。5 种生命周期状态。源码参考：`Task.ts`。

```python
# agents/s09_task_system.py (simplified)

TASK_ID_PREFIXES = {
    "local_bash": "b",    # b1, b2, b3...
    "local_agent": "a",   # a1, a2, a3...
    "remote_agent": "r",  # r1, r2, r3...
}

TERMINAL = {"completed", "failed", "killed"}
```

### 第 2 步：磁盘流式输出

每个任务有一个 append-only 的输出文件。写入器增量追加，读取器用偏移量做增量读取。真实版本用了 `O_NOFOLLOW` 和 5 GB 上限。源码参考：`diskOutput.ts`。

```python
class DiskTaskOutput:
    def append(self, content):
        with open(self.path, "a") as f:
            f.write(content)

    def read_delta(self, offset):
        with open(self.path, "r") as f:
            f.seek(offset)
            content = f.read()
        return content, offset + len(content.encode("utf-8"))
```

### 第 3 步：依赖 DAG

依赖关系是双向边。"任务 A 阻塞任务 B"会同时更新两个任务。列表操作在读取时过滤掉已完成的 blocker -- 这就是自动解除阻塞。源码参考：`utils/tasks.ts`。

```python
def block_task(self, from_id, to_id):
    a, b = self.tasks[from_id], self.tasks[to_id]
    a.blocks.append(to_id)
    b.blocked_by.append(from_id)

def list_tasks(self):
    completed = {t.id for t in self.tasks.values() if t.status == "completed"}
    for t in self.tasks.values():
        live_blockers = [b for b in t.blocked_by if b not in completed]
        # Return task with only live blockers shown
```

### 第 4 步：CRUD 操作

五个跟 tool 对应的操作：create、update、list、stop、delete。删除会级联 -- 移除一个任务的同时清理所有依赖边。源码参考：`TaskCreateTool.ts`、`TaskStopTool.ts` 等。

```python
def delete(self, task_id):
    del self.tasks[task_id]
    for t in self.tasks.values():
        t.blocks = [b for b in t.blocks if b != task_id]
        t.blocked_by = [b for b in t.blocked_by if b != task_id]
```

### 第 5 步：后台执行

任务跑在后台线程里，有 cancel event 用于优雅终止。即使任务被杀，输出也会保留。

## 变更内容

| 组件 | 之前 (s08) | 之后 (s09) |
|------|-----------|-----------|
| 任务跟踪 | 不存在 | 7 种类型化任务，带前缀 ID |
| 生命周期 | 不存在 | 5 状态机（pending/running/completed/failed/killed） |
| 依赖关系 | 不存在 | 双向 DAG，读取时自动解除阻塞 |
| 输出捕获 | 不存在 | 磁盘流式输出，增量读取 |
| 停止任务 | 不存在 | 优雅 cancel，输出保留 |
| 级联删除 | 不存在 | 任务移除时清理所有边 |

## 试一试

```bash
cd learn-the-actual-claude-code
python agents/s09_task_system.py
```

演示会创建带依赖的任务，在后台线程里跑，读取输出增量，完成后自动解除阻塞，停止运行中的任务，演示级联删除。

试试这些 prompt 来看任务系统的效果：

- "Run unit tests and integration tests in parallel, then review the results"
- "Start a long build, then stop it after 5 seconds"
- "Show me all running tasks and their dependencies"
