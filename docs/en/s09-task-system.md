# s09: Task System

`s01 > s02 > s03 > s04 > s05 | s06 > s07 > s08 > [ s09 ] s10 | s11 > s12 > s13 > s14`

> "A task is a state machine with a dependency graph, not just a thread."

## Problem

Real coding work involves parallelism. Run unit tests, integration tests, and a linter simultaneously, then review results when everything finishes. If one task depends on another, the agent needs to know the ordering. If a task hangs, you need to kill it without losing captured output.

## Solution

Claude Code implements a task system with typed task IDs, a lifecycle state machine, and a dependency DAG backed by disk-streamed output.

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

Each task writes output to a dedicated file. Readers poll with offset-based delta reads -- like `tail -f` but structured.

## How It Works

### Step 1: Task types and lifecycle

Seven task types in the real code, each with a prefixed ID for readability. Five lifecycle states. Source: `Task.ts`.

```python
# agents/s09_task_system.py (simplified)

TASK_ID_PREFIXES = {
    "local_bash": "b",    # b1, b2, b3...
    "local_agent": "a",   # a1, a2, a3...
    "remote_agent": "r",  # r1, r2, r3...
}

TERMINAL = {"completed", "failed", "killed"}
```

### Step 2: Disk-backed output streaming

Each task gets an append-only output file. Writers append incrementally; readers use offset-based delta reads. Real version uses `O_NOFOLLOW` and a 5 GB cap. Source: `diskOutput.ts`.

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

### Step 3: Dependency DAG

Dependencies are bidirectional edges. "Task A blocks task B" updates both tasks. The list operation filters out completed blockers at read time -- this is auto-unblocking. Source: `utils/tasks.ts`.

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

### Step 4: CRUD operations

Five tool-mapped operations: create, update, list, stop, and delete. Delete cascades -- removing a task cleans up all its dependency edges. Source: `TaskCreateTool.ts`, `TaskStopTool.ts`, etc.

```python
def delete(self, task_id):
    del self.tasks[task_id]
    for t in self.tasks.values():
        t.blocks = [b for b in t.blocks if b != task_id]
        t.blocked_by = [b for b in t.blocked_by if b != task_id]
```

### Step 5: Background execution

Tasks run in background threads with a cancel event for graceful termination. Output is preserved even when a task is killed.

## What Changed

| Component | Before (s08) | After (s09) |
|-----------|-------------|-------------|
| Task tracking | N/A | 7 typed task types with prefixed IDs |
| Lifecycle | N/A | 5-state machine (pending/running/completed/failed/killed) |
| Dependencies | N/A | Bidirectional DAG with auto-unblocking |
| Output capture | N/A | Disk-backed streaming with delta reads |
| Stopping tasks | N/A | Graceful cancel with output preserved |
| Cascade delete | N/A | Edges cleaned up when a task is removed |

## Try It

```bash
cd learn-the-actual-claude-code
python agents/s09_task_system.py
```

The demo creates tasks with dependencies, runs them in background threads, reads output deltas, auto-unblocks on completion, stops a running task, and demonstrates cascade delete.

Try these prompts to see the task system in action:

- "Run unit tests and integration tests in parallel, then review the results"
- "Start a long build, then stop it after 5 seconds"
- "Show me all running tasks and their dependencies"
