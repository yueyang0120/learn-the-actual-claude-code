# Session 09 -- 任务系统：结构化依赖 DAG

s01 > s02 > s03 > s04 > s05 | s06 > s07 > s08 > **s09** > s10 | s11 > s12 > s13 > s14

---

> *"A task is not a thread -- it is a state machine with a dependency graph."*
> *"任务不是线程——它是一个带有依赖图的状态机。"*
>
> **Harness 层**: 本节涵盖任务系统——Claude Code 如何创建、跟踪和管理具有类型化
> 生命周期、依赖边和磁盘支持的输出流的并发工作项。

---

## 问题

真实世界的编程任务涉及并行性。你可能需要同时运行单元测试、集成测试和 linter，
然后仅在所有任务完成后才审查结果。如果一个任务依赖另一个任务，agent 需要知道
执行顺序。如果一个任务挂起，你需要在不丢失已捕获输出的情况下终止它。

你需要一个系统来：

- 以类型化的生命周期跟踪任务（pending、running、completed、failed、killed）
- 将依赖关系建模为有向无环图（DAG）
- 将输出流式写入磁盘，以便在任务运行时读取部分结果
- 支持在不丢失数据的情况下停止失控的任务
- 通过适当的并发控制在重启后持久化状态

## 解决方案

Claude Code 实现了一个具有 **7 种任务类型**、**生命周期状态机** 和 **依赖 DAG**
的任务系统，由基于文件的持久化和锁文件并发控制支撑。

```
                     Dependency DAG
  +----------+       +----------+
  | b1: unit |       | b2: integ|
  | tests    |       | tests    |
  +----+-----+       +----+-----+
       |                   |
       +--------+----------+
                |
           +----v-----+
           | a1: review|
           | results   |
           +-----------+

  Task Lifecycle:
  pending --> running --> completed
                 |------> failed
                 |------> killed
```

每个任务将其输出写入磁盘上的专用文件。读取器可以使用基于偏移量的增量读取来轮询
新输出——类似于 `tail -f`，但是结构化的。

## 工作原理

### 任务类型和生命周期

系统支持 7 种任务类型。每种类型获得一个带前缀的 ID 以提高人类可读性。

```python
# agents/s09_task_system.py -- mirrors src/Task.ts

class TaskType(Enum):
    LOCAL_BASH   = "local_bash"    # Shell commands
    LOCAL_AGENT  = "local_agent"   # Subagent tasks
    REMOTE_AGENT = "remote_agent"  # Remote execution
    # + in_process_teammate, local_workflow, monitor_mcp, dream

TASK_ID_PREFIXES = {
    TaskType.LOCAL_BASH:   "b",    # b1, b2, b3...
    TaskType.LOCAL_AGENT:  "a",    # a1, a2, a3...
    TaskType.REMOTE_AGENT: "r",    # r1, r2, r3...
}

class TaskStatus(Enum):
    PENDING   = "pending"
    RUNNING   = "running"
    COMPLETED = "completed"
    FAILED    = "failed"
    KILLED    = "killed"

TERMINAL_STATUSES = {TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.KILLED}
```

### 任务状态

每个任务携带其元数据、依赖边、输出文件路径和取消回调。

```python
# agents/s09_task_system.py -- mirrors TaskStateBase in Task.ts

@dataclass
class TaskState:
    id: str
    task_type: TaskType
    status: TaskStatus
    subject: str
    description: str
    owner: Optional[str] = None
    blocks: list[str] = field(default_factory=list)      # IDs this task blocks
    blocked_by: list[str] = field(default_factory=list)   # IDs that block this task
    output_file: str = ""
    output_offset: int = 0
    notified: bool = False
    start_time: float = field(default_factory=time.time)
    end_time: Optional[float] = None
    _cancel: Optional[Callable] = field(default=None, repr=False)
```

### 磁盘支持的输出流

每个任务获得一个专用输出文件。写入器增量追加；读取器可以使用基于偏移量的增量
读取来轮询新内容。

```python
# agents/s09_task_system.py -- mirrors DiskTaskOutput in diskOutput.ts

class DiskTaskOutput:
    """Append-only output file. Real version uses O_NOFOLLOW, 5 GB cap,
    and an async drain loop."""

    def __init__(self, path: str):
        self._path = path
        self._lock = threading.Lock()

    def append(self, content: str) -> None:
        with self._lock:
            with open(self._path, "a") as f:
                f.write(content)

    def read_delta(self, offset: int) -> tuple[str, int]:
        """Read new bytes from offset. Returns (content, new_offset)."""
        with self._lock:
            with open(self._path, "r") as f:
                f.seek(offset)
                content = f.read()
            new_offset = offset + len(content.encode("utf-8"))
            return content, new_offset
```

### CRUD 操作

任务管理器提供五个与 tool 映射的操作，与真实的 Claude Code tools 对应。

```python
# agents/s09_task_system.py -- mirrors the five task tools

class TaskManager:
    # TaskCreateTool: create a new task in pending status
    def create(self, task_type, subject, description, owner=None) -> str:
        tid = self._gen_id(task_type)
        out = self._init_output(tid)
        task = TaskState(id=tid, task_type=task_type,
                         status=TaskStatus.PENDING, subject=subject, ...)
        self._tasks[tid] = task
        return tid

    # TaskUpdateTool: update fields and manage dependency edges
    def update(self, task_id, status=None, add_blocks=None, add_blocked_by=None):
        # ... update fields, add bidirectional dependency edges
        if is_terminal(status):
            task.end_time = time.time()

    # TaskListTool: list with auto-unblocking (read-time filter)
    def list_tasks(self) -> list[dict]:
        completed_ids = {tid for tid, t in self._tasks.items()
                         if t.status == TaskStatus.COMPLETED}
        for t in self._tasks.values():
            live_blockers = [bid for bid in t.blocked_by
                             if bid not in completed_ids]
            # Return task with only live blockers shown

    # TaskStopTool: kill a running task
    def stop(self, task_id) -> bool:
        task = self._tasks[task_id]
        if task._cancel:
            task._cancel()
        task.status = TaskStatus.KILLED
```

### 依赖 DAG

依赖关系是双向边。当你说"任务 A 阻塞任务 B"时，两个任务都会被更新。列表操作在
读取时过滤掉已完成的阻塞者——这就是自动解除阻塞。

```python
# agents/s09_task_system.py -- mirrors blockTask() in utils/tasks.ts

def block_task(self, from_id: str, to_id: str) -> bool:
    """A blocks B: write bidirectional edges."""
    a = self._tasks[from_id]
    b = self._tasks[to_id]
    if to_id not in a.blocks:
        a.blocks.append(to_id)
    if from_id not in b.blocked_by:
        b.blocked_by.append(from_id)
    return True
```

### 级联删除

当一个任务被删除时，它的依赖边会从所有其他任务中移除。

```python
# agents/s09_task_system.py -- mirrors deleteTask() in utils/tasks.ts

def delete(self, task_id: str) -> bool:
    del self._tasks[task_id]
    # Cascade: remove edges referencing this task
    for t in self._tasks.values():
        t.blocks = [bid for bid in t.blocks if bid != task_id]
        t.blocked_by = [bid for bid in t.blocked_by if bid != task_id]
    return True
```

### 后台执行

任务在后台线程中运行。取消事件允许优雅终止。

```python
# agents/s09_task_system.py -- simulates spawner execution

def run_in_background(self, task_id, work):
    task = self._tasks[task_id]
    out = self._outputs[task_id]
    cancel_event = threading.Event()
    task._cancel = cancel_event.set

    def _worker():
        task.status = TaskStatus.RUNNING
        try:
            result = work(out)
            if cancel_event.is_set():
                return  # killed mid-flight
            task.status = TaskStatus.COMPLETED
        except Exception as e:
            task.status = TaskStatus.FAILED
            out.append(f"\n=== ERROR ===\n{e}\n")
        finally:
            task.end_time = time.time()

    threading.Thread(target=_worker, daemon=True).start()
```

## 变化对比

| 组件 | 之前 | 之后 |
|------|------|------|
| 任务跟踪 | 临时性的，无结构 | 7 种类型化任务类型，带前缀 ID |
| 生命周期 | 二元状态：运行中或完成 | 5 状态机：pending、running、completed、failed、killed |
| 依赖关系 | 用户手动排序 | 双向 DAG，读取时自动解除阻塞 |
| 输出捕获 | 任务结束后丢失 | 磁盘支持的流式传输，5 GB 上限和增量读取 |
| 并发控制 | 无 | 基于文件的持久化，带锁文件并发控制 |
| 停止任务 | 杀死进程，丢失输出 | 优雅取消，输出保留 |
| 级联删除 | 遗留孤立边 | 任务移除时清理所有边 |

## 试一试

```bash
# Run the task system demo
python agents/s09_task_system.py
```

演示逐步展示：

1. **创建任务** -- 单元测试、集成测试和一个审查任务
2. **建立 DAG** -- 审查任务被两个测试任务阻塞
3. **后台执行** -- 在线程中运行任务并流式输出
4. **增量读取** -- 在任务运行时轮询新输出
5. **自动解除阻塞** -- 已完成的阻塞者从 blocked_by 列表中消失
6. **停止任务** -- 在运行中终止一个长时间运行的构建
7. **级联删除** -- 移除一个任务并清理它的边

可以尝试以下实验：

- 创建一个更深的依赖链（A 阻塞 B 阻塞 C 阻塞 D）
- 启动一个任务，读取部分输出，然后停止它
- 删除链中间的一个任务并检查存活的边
