# Session 09 -- Task System: Structured Dependency DAG

s01 > s02 > s03 > s04 > s05 | s06 > s07 > s08 > **s09** > s10 | s11 > s12 > s13 > s14

---

> *"A task is not a thread -- it is a state machine with a dependency graph."*
>
> **Harness layer**: This session covers the task system -- how Claude Code
> creates, tracks, and manages concurrent work items with typed lifecycles,
> dependency edges, and disk-backed output streaming.

---

## Problem

Real-world coding tasks involve parallelism. You might need to run unit tests,
integration tests, and a linter simultaneously, then review the results only
when everything finishes. If one task depends on another, the agent needs to
know the ordering. If a task hangs, you need to kill it without losing the
output captured so far.

You need a system that:

- Tracks tasks with typed lifecycles (pending, running, completed, failed, killed)
- Models dependencies as a directed acyclic graph (DAG)
- Streams output to disk so you can read partial results while a task runs
- Supports stopping runaway tasks without losing data
- Persists state across restarts with proper concurrency control

## Solution

Claude Code implements a task system with **7 task types**, a **lifecycle state
machine**, and a **dependency DAG** backed by file-based persistence with
lockfile concurrency.

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

Each task writes its output to a dedicated file on disk. Readers can poll for
new output using offset-based delta reads -- like `tail -f` but structured.

## How It Works

### Task Types and Lifecycle

The system supports 7 task types. Each gets a prefixed ID for human readability.

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

### Task State

Each task carries its metadata, dependency edges, output file path, and a
cancel callback.

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

### Disk-Backed Output Streaming

Each task gets a dedicated output file. Writers append incrementally; readers
can poll for new content using offset-based delta reads.

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

### CRUD Operations

The task manager provides five tool-mapped operations, matching the real
Claude Code tools.

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

### Dependency DAG

Dependencies are bidirectional edges. When you say "task A blocks task B", both
tasks get updated. The list operation filters out completed blockers at read
time -- this is auto-unblocking.

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

### Delete with Cascade

When a task is deleted, its dependency edges are removed from all other tasks.

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

### Background Execution

Tasks run in background threads. A cancel event allows graceful termination.

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

## What Changed

| Component | Before | After |
|-----------|--------|-------|
| Task tracking | Ad-hoc, no structure | 7 typed task types with prefixed IDs |
| Lifecycle | Binary: running or done | 5-state machine: pending, running, completed, failed, killed |
| Dependencies | Manual ordering by the user | Bidirectional DAG with auto-unblocking at read time |
| Output capture | Lost when task ends | Disk-backed streaming with 5 GB cap and delta reads |
| Concurrency control | None | File-based persistence with lockfile concurrency |
| Stopping tasks | Kill the process, lose output | Graceful cancel with output preserved |
| Cascade delete | Orphaned edges | All edges cleaned up when a task is removed |

## Try It

```bash
# Run the task system demo
python agents/s09_task_system.py
```

The demo walks through:

1. **Creating tasks** -- unit tests, integration tests, and a review task
2. **Setting up the DAG** -- review is blocked by both test tasks
3. **Background execution** -- running tasks in threads with output streaming
4. **Delta reads** -- polling for new output while a task runs
5. **Auto-unblocking** -- completed blockers disappear from the blocked_by list
6. **Stopping a task** -- killing a long-running build mid-flight
7. **Cascade delete** -- removing a task and cleaning up its edges

Experiment with the system:

- Create a deeper dependency chain (A blocks B blocks C blocks D)
- Start a task, read partial output, then stop it
- Delete a task in the middle of a chain and inspect the surviving edges
