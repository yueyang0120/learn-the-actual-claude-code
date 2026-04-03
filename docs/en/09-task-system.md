# Chapter 9: The Task System

The agent loop (Chapter 1) processes tool calls sequentially. Subagents (Chapter 8) introduce concurrency at the agent level. But many real workflows require finer-grained parallelism -- running tests, linting, and building simultaneously, then reviewing results only after everything finishes. The task system provides this capability: a typed registry of background work items connected by a dependency graph, with append-only disk-backed output and a five-state lifecycle machine.

## The Problem

Sequential execution wastes time on independent work. A developer asking Claude Code to "run unit tests, integration tests, and lint in parallel, then summarize results" expects concurrent execution with ordered post-processing. Without a task abstraction, the agent must either run each job serially or spawn subagents with no structured way to track completion, express ordering constraints, or stream output incrementally.

The concurrency problem extends beyond execution ordering. Background tasks produce output continuously. A 30-second test run should not block the agent from reading partial results. If a task hangs, it must be killable without losing what has already been captured. And in swarm scenarios where multiple agents create tasks simultaneously, the registry must handle concurrent writes without corruption.

A dependency graph adds further complexity. Task A may depend on tasks B and C. When B completes, A should not unblock -- it must wait for C as well. The graph must be bidirectional (a task knows both what it blocks and what blocks it) and self-maintaining (completed blockers disappear automatically).

## How Claude Code Solves It

### Task types and ID generation

Seven task types cover the full range of background work. Each type receives a single-letter prefix, and every task ID combines that prefix with eight random alphanumeric characters drawn from a 36-character alphabet. This yields 36^8 (approximately 2.8 trillion) possible IDs per prefix, making collisions negligible even under heavy concurrent creation.

```typescript
// src/tools/task/Task.ts
type TaskType =
  | "local_bash"          // prefix: b
  | "local_agent"         // prefix: a
  | "remote_agent"        // prefix: r
  | "in_process_teammate" // prefix: t
  | "local_workflow"      // prefix: w
  | "monitor_mcp"         // prefix: m
  | "dream";              // prefix: d

function generateTaskId(type: TaskType): string {
  const prefix = TYPE_PREFIX_MAP[type]; // e.g. "b"
  const suffix = randomAlphanumeric(8); // 36^8 combinations
  return prefix + suffix;
}
```

The prefix serves a practical purpose: when scanning a task list, `b` IDs are immediately identifiable as bash tasks, `a` IDs as agent tasks, and so on. No lookup required.

### Lifecycle state machine

Every task follows a five-state lifecycle. The transitions are strict: a task begins as `pending`, moves to `running` when execution starts, and terminates in exactly one of three states.

```typescript
// src/tools/task/Task.ts
type TaskStatus = "pending" | "running" | "completed" | "failed" | "killed";

interface TaskStateBase {
  id: string;
  type: TaskType;
  status: TaskStatus;
  description: string;
  outputFile: string;
  outputOffset: number;
  notified: boolean;
  blocks: string[];
  blockedBy: string[];
  // ... 12 fields total
}
```

The `notified` field tracks whether the agent has been informed of completion. This prevents repeated notifications for the same terminal event -- without it, every status check on a completed task would re-trigger a "task finished" message. The `outputOffset` field enables incremental reads, discussed next. Together, the 12 fields of `TaskStateBase` capture the complete lifecycle of a work item: what it is, where it stands, what it produces, and how it relates to other tasks.

### Disk-backed output streaming

Each task writes to a dedicated append-only file. The DiskTaskOutput system enforces a 5 GB cap and provides offset-based incremental reads, analogous to `tail -f` but structured for programmatic consumption.

```typescript
// src/tools/task/diskOutput.ts
class DiskTaskOutput {
  append(content: string): void {
    // O_NOFOLLOW prevents symlink attacks
    // Append-only: never seeks backward, never truncates
    fs.appendFileSync(this.path, content, { flag: "a" });
  }

  readDelta(offset: number): { content: string; newOffset: number } {
    const fd = fs.openSync(this.path, "r");
    const buf = Buffer.alloc(CHUNK_SIZE);
    const bytesRead = fs.readSync(fd, buf, 0, CHUNK_SIZE, offset);
    fs.closeSync(fd);
    return {
      content: buf.toString("utf-8", 0, bytesRead),
      newOffset: offset + bytesRead,
    };
  }
}
```

The offset-based design means readers never re-process old output. An agent checking on a long-running test suite receives only the new lines since its last read, keeping context window consumption proportional to the delta rather than the total output.

### Dependency DAG

Dependencies form a directed acyclic graph with bidirectional edges. When task A is declared to block task B, both records update: A gains B in its `blocks` list, and B gains A in its `blockedBy` list.

```typescript
// src/utils/tasks.ts
function addDependency(tasks: Map<string, TaskState>, from: string, to: string) {
  const blocker = tasks.get(from);
  const blocked = tasks.get(to);
  blocker.blocks.push(to);
  blocked.blockedBy.push(from);
}

function listTasks(tasks: Map<string, TaskState>): TaskView[] {
  const completed = new Set(
    [...tasks.values()]
      .filter(t => t.status === "completed")
      .map(t => t.id)
  );
  return [...tasks.values()].map(t => ({
    ...t,
    blockedBy: t.blockedBy.filter(id => !completed.has(id)),
  }));
}
```

The critical detail is in `listTasks`: completed blockers are filtered out at read time rather than eagerly removed from the stored data. This is auto-unblocking. When the agent lists tasks, any dependency on a completed task simply disappears from the view, and the blocked task appears ready to run.

### Task creation with file locking

`TaskCreateTool` generates a new ID, writes the initial state to the task registry, and -- in swarm scenarios -- acquires a file lock before writing. The lock prevents two agents from simultaneously creating tasks and corrupting the registry file.

```typescript
// src/tools/task/TaskCreateTool.ts
async function createTask(
  type: TaskType,
  description: string,
  blockedBy?: string[]
): Promise<TaskState> {
  const id = generateTaskId(type);
  const task: TaskState = {
    id,
    type,
    status: "pending",
    description,
    outputFile: path.join(outputDir, `${id}.out`),
    outputOffset: 0,
    notified: false,
    blocks: [],
    blockedBy: blockedBy ?? [],
  };

  await withFileLock(registryPath, async () => {
    const registry = await readRegistry();
    registry.set(id, task);
    await writeRegistry(registry);
  });

  return task;
}
```

### Updates, deletion, and stopping

`TaskUpdateTool` supports field updates, dependency edge management, and deletion with cascade -- removing a task cleans all references to it from other tasks' `blocks` and `blockedBy` lists. It also includes a verification nudge that reminds the agent to check task results before marking work complete, reducing premature completion.

`TaskStopTool` dispatches type-specific kill signals. The dispatch is necessary because different task types run in different execution contexts: bash tasks are OS processes, agent tasks are cooperative async loops, and teammate tasks may be in separate panes.

```typescript
// src/tools/task/TaskStopTool.ts
function stopTask(task: TaskState): void {
  switch (task.type) {
    case "local_bash":
      process.kill(task.pid, "SIGTERM");
      break;
    case "local_agent":
    case "in_process_teammate":
      task.abortController.abort();
      break;
    // ... type-specific dispatch for each of the 7 types
  }
  task.status = "killed";
}
```

### Background execution

Tasks run in separate threads, each receiving an `AbortSignal` for cooperative cancellation. The thread writes output to the task's disk file as it progresses. Output continues streaming even during the kill sequence, ensuring that partial results are never lost. The combination of thread-based execution and disk-backed output means that the agent's main loop remains free to process other tool calls while background tasks run.

## Key Design Decisions

**Single-letter prefixes instead of namespaced strings.** A task list with IDs like `b3kx9m2p`, `a7tn4q1w` is immediately scannable. The alternative -- `local_bash_3kx9m2p` -- adds visual noise without additional information, since the seven types are well-known within the system.

**Read-time auto-unblocking instead of write-time eager removal.** Eager removal would require finding and updating all tasks that reference a completing task at the moment of completion. Read-time filtering defers this work to the point of consumption, which is simpler and avoids race conditions when multiple tasks complete simultaneously.

**Append-only output with offset reads instead of in-memory buffering.** Memory-buffered output caps the maximum task duration at whatever the process can hold in RAM. Disk-backed output with a 5 GB cap supports arbitrarily long tasks. The offset-based protocol is also naturally idempotent: re-reading from the same offset returns the same data.

**File locking for task creation in swarm mode.** When multiple agents in a swarm create tasks concurrently, the task registry file is a shared resource. File locking serializes writes at the OS level, avoiding the complexity of a database or coordination service.

## In Practice

When a user asks Claude Code to run tests and lint in parallel, the agent creates two `local_bash` tasks (IDs like `bA3x9m2p` and `bK7tn4q1`), then creates a `local_agent` task (`aR5wz8v3`) that declares both bash tasks as blockers. The bash tasks begin executing immediately in background threads. Their output streams to disk files. The agent periodically reads deltas from each output file to monitor progress. When both bash tasks reach `completed` status, the agent task's `blockedBy` list becomes empty at read time, and the agent begins its review.

If a test suite hangs, the user (or the agent itself) can invoke `TaskStopTool` to kill the specific task. The output captured up to that point remains available on disk. The task's status transitions to `killed`, and any tasks that depended on it can be updated or re-planned accordingly.

The `dream` task type serves a distinct purpose: background processing that runs speculatively while the user is idle. Unlike bash or agent tasks, dream tasks are low-priority and can be preempted when new user input arrives. The `monitor_mcp` type watches an MCP server connection, restarting it if it drops. The `local_workflow` type chains multiple steps into a single tracked unit. Each type uses the same lifecycle state machine and dependency graph, but the execution semantics differ based on the type's nature.

## Summary

- Seven task types identified by single-letter prefixes (`b`, `a`, `r`, `t`, `w`, `m`, `d`) with 8-character random suffixes provide collision-resistant, human-scannable IDs.
- A five-state lifecycle (`pending`, `running`, `completed`, `failed`, `killed`) governs every task from creation to termination.
- Append-only disk output with offset-based incremental reads supports long-running tasks without memory pressure, capped at 5 GB per task.
- A bidirectional dependency DAG with read-time auto-unblocking allows tasks to express ordering constraints that resolve automatically as blockers complete.
- File locking, type-specific kill dispatch, and cascade deletion handle the operational concerns of concurrent creation, graceful termination, and clean removal.
