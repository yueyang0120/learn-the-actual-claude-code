# Session 09 -- Task System

## What this session covers

How Claude Code manages background tasks and structured to-do lists: the
`TaskType` taxonomy covering seven task variants, the lifecycle state machine
from pending through terminal states, dependency tracking via a DAG of
`blocks`/`blockedBy` edges, disk-backed output streaming, and the five
CRUD-plus-control tools that the model uses to drive the system.

## Learning objectives

1. Understand the **TaskType** enum and why Claude Code distinguishes seven
   flavors of task (`local_bash`, `local_agent`, `remote_agent`,
   `in_process_teammate`, `local_workflow`, `monitor_mcp`, `dream`).
2. Trace the **TaskStatus** state machine: `pending -> running ->
   completed | failed | killed`, and the `isTerminalTaskStatus` guard that
   prevents interacting with dead tasks.
3. Learn how **TaskStateBase** encapsulates shared bookkeeping -- output file
   path, byte offset for incremental reads, notification tracking, and
   wall-clock timing.
4. See how **TaskHandle** and **TaskContext** wire up cleanup callbacks and
   abort controllers to the centralized AppState.
5. Understand the **dependency DAG**: `blockTask()` writes symmetric
   `blocks`/`blockedBy` edges; `TaskListTool` filters out completed blockers
   so the model sees only live constraints; `deleteTask()` cascades edge
   removal.
6. Walk through the five management tools: TaskCreateTool, TaskUpdateTool,
   TaskListTool, TaskOutputTool, TaskStopTool.
7. See how **DiskTaskOutput** streams output to disk with a write queue,
   O_NOFOLLOW security hardening, and a 5 GB cap.

## Source files covered

| File | Purpose |
|------|---------|
| `src/Task.ts` | Core types: TaskType, TaskStatus, TaskStateBase, TaskHandle, TaskContext, generateTaskId |
| `src/tools/TaskCreateTool/TaskCreateTool.ts` | Creates tasks with pending status, fires TaskCreated hooks |
| `src/tools/TaskUpdateTool/TaskUpdateTool.ts` | Updates fields, manages blocks/blockedBy edges, handles deletion |
| `src/tools/TaskListTool/TaskListTool.ts` | Lists all tasks, filters completed blockers from blockedBy |
| `src/tools/TaskOutputTool/TaskOutputTool.tsx` | Reads output from running/completed tasks, supports blocking wait |
| `src/tools/TaskStopTool/TaskStopTool.ts` | Validates task is running, dispatches to type-specific kill |
| `src/tasks/stopTask.ts` | Shared stop logic: lookup, validate, dispatch kill, suppress bash notifications |
| `src/utils/tasks.ts` | Persistence layer: file-based CRUD with lockfile concurrency, blockTask, claimTask |
| `src/utils/task/diskOutput.ts` | DiskTaskOutput class: append queue, O_NOFOLLOW, 5 GB cap, delta/tail reads |

## What shareAI-lab and similar clones miss

Open-source Claude Code clones that implement a "task" or "todo" concept
typically use a flat in-memory list with string statuses. The real system
goes substantially further:

1. **Seven typed task variants.**  Each type has its own spawn, render, and
   kill implementation. `local_bash` writes output to disk files;
   `local_agent` symlinks to the full agent transcript; `in_process_teammate`
   runs as a thread in the same process; `remote_agent` connects to an
   external compute session. Clones usually support only one type (shell).

2. **Dependency DAG with automatic unblocking.**  `blockTask()` writes
   bidirectional edges (`blocks` and `blockedBy`). When a blocker task
   completes, TaskListTool automatically filters it from the `blockedBy`
   display so the model sees only live constraints. Deletion cascades edge
   removal across all referencing tasks. Clones have no dependency concept.

3. **File-based persistence with lockfile concurrency.**  Tasks are stored as
   individual JSON files under `~/.claude/tasks/<taskListId>/`. Concurrent
   swarm agents (10+) use `proper-lockfile` with exponential backoff to
   serialize writes. A high-water-mark file prevents ID reuse after
   deletion/reset. Clones keep tasks in memory, losing them on restart.

4. **Disk-backed output streaming.**  DiskTaskOutput uses a write queue with
   `O_NOFOLLOW` to prevent symlink attacks from sandboxed processes. Output
   files can grow to 5 GB. Delta reads (`readFileRange`) let TaskOutputTool
   fetch only new bytes since the last read. Clones capture output in memory
   strings.

5. **Swarm-aware task claiming.**  `claimTask()` atomically checks whether an
   agent already owns an in-progress task before allowing a new claim,
   preventing double-work in multi-agent swarms. Clones have no concept of
   task ownership or team coordination.

## Reimplementation

`reimplementation.py` is a runnable Python (~250 LOC) that demonstrates the
core task system patterns: typed tasks with lifecycle states, a dependency DAG
with auto-unblocking, output streaming from files, and background execution
via threading. Run it directly:

```bash
cd sessions/s09-task-system
python reimplementation.py
```

No API key needed -- it uses simulated tasks to show lifecycle and dependency
behavior.
