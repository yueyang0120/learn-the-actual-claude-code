# Source Analysis -- Task System

## 1. TaskType enum: seven flavors of background work

Claude Code does not have a single "task" abstraction. It defines seven
distinct task types, each with its own spawn path, output strategy, and kill
implementation:

```typescript
// src/Task.ts lines 6-13
export type TaskType =
  | 'local_bash'          // Background shell command (e.g., `npm test &`)
  | 'local_agent'         // Subagent running in a local Claude process
  | 'remote_agent'        // Agent running on a remote compute session
  | 'in_process_teammate' // Teammate running as a thread in the same process
  | 'local_workflow'      // Multi-step workflow orchestration
  | 'monitor_mcp'         // MCP server monitor task
  | 'dream'               // Background "dreaming" / speculative work
```

Each type gets a single-letter prefix for its task ID, generated with
cryptographic randomness:

```typescript
// src/Task.ts lines 79-106
const TASK_ID_PREFIXES: Record<string, string> = {
  local_bash: 'b',
  local_agent: 'a',
  remote_agent: 'r',
  in_process_teammate: 't',
  local_workflow: 'w',
  monitor_mcp: 'm',
  dream: 'd',
}

// 36^8 ~ 2.8 trillion combinations -- resistant to brute-force symlink attacks
const TASK_ID_ALPHABET = '0123456789abcdefghijklmnopqrstuvwxyz'

export function generateTaskId(type: TaskType): string {
  const prefix = getTaskIdPrefix(type)
  const bytes = randomBytes(8)
  let id = prefix
  for (let i = 0; i < 8; i++) {
    id += TASK_ID_ALPHABET[bytes[i]! % TASK_ID_ALPHABET.length]
  }
  return id  // e.g., "b3k7f2m1x" for a local_bash task
}
```

The prefix serves two purposes: human readability (you can tell task type at a
glance) and security (directory listings cannot collide across types).

---

## 2. TaskStatus lifecycle: the state machine

Tasks follow a strict state machine with five states:

```
                   +--------+
                   | pending|
                   +---+----+
                       |
                       v
                   +--------+
                   | running|
                   +---+----+
                      /|\
                     / | \
                    v  v  v
           +----------+ +------+ +------+
           | completed| | failed| | killed|
           +----------+ +------+ +------+
                 (terminal states)
```

The `isTerminalTaskStatus` guard prevents interacting with dead tasks:

```typescript
// src/Task.ts lines 27-29
export function isTerminalTaskStatus(status: TaskStatus): boolean {
  return status === 'completed' || status === 'failed' || status === 'killed'
}
```

This guard is used throughout the codebase to:
- Prevent injecting messages into dead teammates
- Evict finished tasks from AppState
- Clean up orphaned task resources

---

## 3. TaskHandle and TaskContext

**TaskHandle** is returned when a task is spawned. It carries the task ID and
an optional cleanup callback:

```typescript
// src/Task.ts lines 31-34
export type TaskHandle = {
  taskId: string
  cleanup?: () => void  // Called when the task is evicted from state
}
```

**TaskContext** provides the environment a running task needs to interact with
the application:

```typescript
// src/Task.ts lines 38-42
export type TaskContext = {
  abortController: AbortController   // Signal to cancel the task
  getAppState: () => AppState        // Read current app state
  setAppState: SetAppState           // Update app state immutably
}
```

The **Task** interface itself is minimal -- just a name, type, and kill method.
The comment explains why: spawn and render were never called polymorphically
(removed in PR #22546):

```typescript
// src/Task.ts lines 72-76
export type Task = {
  name: string
  type: TaskType
  kill(taskId: string, setAppState: SetAppState): Promise<void>
}
```

---

## 4. TaskStateBase: the persistent task record

Every task in AppState carries these base fields:

```typescript
// src/Task.ts lines 45-57
export type TaskStateBase = {
  id: string                // Unique ID with type prefix
  type: TaskType            // Which variant of task
  status: TaskStatus        // Current lifecycle state
  description: string       // Human-readable description
  toolUseId?: string        // Links back to the tool_use that created it
  startTime: number         // Date.now() when created
  endTime?: number          // Set when reaching terminal state
  totalPausedMs?: number    // Accumulated pause time
  outputFile: string        // Path to disk output file
  outputOffset: number      // Byte offset for incremental reads
  notified: boolean         // Whether the model has been told about completion
}
```

The `createTaskStateBase` factory initializes these:

```typescript
// src/Task.ts lines 108-125
export function createTaskStateBase(
  id: string, type: TaskType, description: string, toolUseId?: string,
): TaskStateBase {
  return {
    id, type, status: 'pending', description, toolUseId,
    startTime: Date.now(),
    outputFile: getTaskOutputPath(id),  // e.g., <tempDir>/<sessionId>/tasks/<id>.output
    outputOffset: 0,
    notified: false,
  }
}
```

Key design decisions:
- **outputFile** points to a file in the project temp directory, scoped by
  session ID so concurrent sessions do not clobber each other.
- **outputOffset** enables delta reads -- TaskOutputTool reads only new bytes.
- **notified** prevents duplicate completion notifications. Once the model has
  seen the result, this flag is set to true.

---

## 5. TaskCreateTool: spawning tasks

TaskCreateTool creates a new task record in the file-based task list:

```typescript
// src/tools/TaskCreateTool/TaskCreateTool.ts lines 80-128
async call({ subject, description, activeForm, metadata }, context) {
  // 1. Create the task with pending status
  const taskId = await createTask(getTaskListId(), {
    subject,
    description,
    activeForm,
    status: 'pending',
    owner: undefined,
    blocks: [],       // Tasks this one blocks
    blockedBy: [],    // Tasks blocking this one
    metadata,
  })

  // 2. Execute TaskCreated hooks -- hooks can block creation
  const blockingErrors: string[] = []
  const generator = executeTaskCreatedHooks(...)
  for await (const result of generator) {
    if (result.blockingError) {
      blockingErrors.push(getTaskCreatedHookMessage(result.blockingError))
    }
  }

  // 3. If a hook blocked, delete the task and throw
  if (blockingErrors.length > 0) {
    await deleteTask(getTaskListId(), taskId)
    throw new Error(blockingErrors.join('\n'))
  }

  // 4. Auto-expand the tasks panel in the UI
  context.setAppState(prev => ({
    ...prev, expandedView: 'tasks' as const
  }))

  return { data: { task: { id: taskId, subject } } }
}
```

The underlying `createTask()` in `utils/tasks.ts` uses file locking to prevent
ID collisions in swarm scenarios:

```typescript
// src/utils/tasks.ts lines 284-308
export async function createTask(
  taskListId: string, taskData: Omit<Task, 'id'>
): Promise<string> {
  const lockPath = await ensureTaskListLockFile(taskListId)
  let release = await lockfile.lock(lockPath, LOCK_OPTIONS)
  try {
    const highestId = await findHighestTaskId(taskListId)  // Files + high water mark
    const id = String(highestId + 1)
    const task = { id, ...taskData }
    await writeFile(getTaskPath(taskListId, id), jsonStringify(task, null, 2))
    notifyTasksUpdated()
    return id
  } finally {
    await release()
  }
}
```

---

## 6. TaskUpdateTool: managing task state and dependencies

TaskUpdateTool is the most complex of the five tools. It handles:

**Field updates** (subject, description, status, owner, metadata):

```typescript
// src/tools/TaskUpdateTool/TaskUpdateTool.ts lines 160-270
// Diff-based: only writes fields that actually changed
const updates = {}
if (subject !== undefined && subject !== existingTask.subject) {
  updates.subject = subject
  updatedFields.push('subject')
}
// ... same pattern for description, activeForm, owner, metadata

// Status changes fire hooks:
if (status === 'completed') {
  const generator = executeTaskCompletedHooks(...)
  // Hooks can block the completion
}
```

**Dependency management** via `addBlocks` and `addBlockedBy`:

```typescript
// src/tools/TaskUpdateTool/TaskUpdateTool.ts lines 300-324
// addBlocks: "this task blocks taskId X"
if (addBlocks && addBlocks.length > 0) {
  const newBlocks = addBlocks.filter(id => !existingTask.blocks.includes(id))
  for (const blockId of newBlocks) {
    await blockTask(taskListId, taskId, blockId)
  }
}

// addBlockedBy: "this task is blocked by taskId X" (reverse direction)
if (addBlockedBy && addBlockedBy.length > 0) {
  const newBlockedBy = addBlockedBy.filter(
    id => !existingTask.blockedBy.includes(id)
  )
  for (const blockerId of newBlockedBy) {
    await blockTask(taskListId, blockerId, taskId)  // Note: reversed args
  }
}
```

**Deletion** is a special status value:

```typescript
if (status === 'deleted') {
  const deleted = await deleteTask(taskListId, taskId)
  // deleteTask cascades: removes blocks/blockedBy edges from all other tasks
  return { data: { success: deleted, ... } }
}
```

**Verification nudge**: When the last task in a 3+ task list is completed and
none was a verification step, the tool nudges the model to spawn a verification
agent.

---

## 7. TaskListTool: querying tasks with smart filtering

TaskListTool returns all tasks with a key filtering optimization -- completed
blocker IDs are removed from the `blockedBy` arrays:

```typescript
// src/tools/TaskListTool/TaskListTool.ts lines 65-89
async call() {
  const allTasks = (await listTasks(taskListId)).filter(
    t => !t.metadata?._internal  // Hide internal tasks
  )

  // Build set of completed task IDs
  const resolvedTaskIds = new Set(
    allTasks.filter(t => t.status === 'completed').map(t => t.id)
  )

  // Filter completed blockers from blockedBy
  const tasks = allTasks.map(task => ({
    id: task.id,
    subject: task.subject,
    status: task.status,
    owner: task.owner,
    blockedBy: task.blockedBy.filter(id => !resolvedTaskIds.has(id)),
  }))

  return { data: { tasks } }
}
```

This is critical for the dependency DAG: when task A blocks task B, and A is
completed, B should no longer appear as blocked. The filtering happens at
read time, not write time, so the persistent edges remain intact.

The text output format is compact and designed for LLM consumption:

```
#1 [pending] Fix authentication bug (alice) [blocked by #2, #3]
#2 [in_progress] Write test fixtures (bob)
#3 [completed] Set up CI pipeline
```

---

## 8. TaskOutputTool: streaming output from disk

TaskOutputTool reads output from a task's disk file with blocking/non-blocking
modes:

```typescript
// src/tools/TaskOutputTool/TaskOutputTool.tsx (call method)
async call(input, toolUseContext, ...) {
  const { task_id, block, timeout } = input

  if (!block) {
    // Non-blocking: return current output immediately
    if (task.status !== 'running' && task.status !== 'pending') {
      updateTaskState(task_id, ..., t => ({ ...t, notified: true }))
      return { data: { retrieval_status: 'success', task: await getTaskOutputData(task) } }
    }
    return { data: { retrieval_status: 'not_ready', task: await getTaskOutputData(task) } }
  }

  // Blocking: poll until completion or timeout (100ms intervals)
  const completedTask = await waitForTaskCompletion(
    task_id, toolUseContext.getAppState, timeout, toolUseContext.abortController
  )
  // ...
}
```

The `getTaskOutputData` function dispatches based on task type:

- **local_bash**: Reads from `shellCommand.taskOutput` (in-memory) or falls
  back to disk file
- **local_agent**: Prefers clean in-memory result over raw JSONL transcript
- **remote_agent**: Includes the command/prompt in output

The disk output system (`DiskTaskOutput` class) is sophisticated:

```typescript
// src/utils/task/diskOutput.ts
export class DiskTaskOutput {
  #path: string
  #queue: string[] = []        // Write queue
  #bytesWritten = 0
  #capped = false              // True when 5GB limit hit

  append(content: string): void {
    if (this.#capped) return
    this.#bytesWritten += content.length
    if (this.#bytesWritten > MAX_TASK_OUTPUT_BYTES) {  // 5GB
      this.#capped = true
      this.#queue.push('\n[output truncated: exceeded 5GB disk cap]\n')
    } else {
      this.#queue.push(content)
    }
    // Trigger drain loop if not already running
    if (!this.#flushPromise) {
      void track(this.#drain())
    }
  }
}
```

Security note: Files are opened with `O_NOFOLLOW` to prevent symlink attacks
from sandboxed processes. An attacker in the sandbox cannot create symlinks
in the tasks directory pointing to arbitrary host files.

---

## 9. TaskStopTool: dispatching to type-specific kill

TaskStopTool validates the task is running, then dispatches to the
type-specific kill implementation:

```typescript
// src/tasks/stopTask.ts lines 38-99
export async function stopTask(taskId: string, context: StopTaskContext) {
  const task = appState.tasks?.[taskId]

  // Validation
  if (!task) throw new StopTaskError('...', 'not_found')
  if (task.status !== 'running') throw new StopTaskError('...', 'not_running')

  // Look up the type-specific Task implementation
  const taskImpl = getTaskByType(task.type)
  if (!taskImpl) throw new StopTaskError('...', 'unsupported_type')

  // Dispatch to the type's kill method
  await taskImpl.kill(taskId, setAppState)

  // For bash tasks: suppress "exit code 137" notification (noise)
  // For agent tasks: don't suppress -- the AbortError carries partial results
  if (isLocalShellTask(task)) {
    setAppState(prev => {
      const prevTask = prev.tasks[taskId]
      if (!prevTask || prevTask.notified) return prev
      return { ...prev, tasks: { ...prev.tasks, [taskId]: { ...prevTask, notified: true } } }
    })
  }

  return { taskId, taskType: task.type, command }
}
```

The error class uses typed codes for programmatic handling:

```typescript
export class StopTaskError extends Error {
  constructor(
    message: string,
    public readonly code: 'not_found' | 'not_running' | 'unsupported_type',
  ) { ... }
}
```

---

## 10. Dependency DAG: blocks/blockedBy

The dependency system uses bidirectional edges stored in each task's JSON file:

```typescript
// src/utils/tasks.ts lines 458-486
export async function blockTask(
  taskListId: string, fromTaskId: string, toTaskId: string
): Promise<boolean> {
  const [fromTask, toTask] = await Promise.all([
    getTask(taskListId, fromTaskId),
    getTask(taskListId, toTaskId),
  ])
  if (!fromTask || !toTask) return false

  // Update source: "A blocks B"
  if (!fromTask.blocks.includes(toTaskId)) {
    await updateTask(taskListId, fromTaskId, {
      blocks: [...fromTask.blocks, toTaskId],
    })
  }

  // Update target: "B is blockedBy A"
  if (!toTask.blockedBy.includes(fromTaskId)) {
    await updateTask(taskListId, toTaskId, {
      blockedBy: [...toTask.blockedBy, fromTaskId],
    })
  }
  return true
}
```

When a task is deleted, the cascade removes all edges:

```typescript
// src/utils/tasks.ts lines 420-434
// Inside deleteTask():
const allTasks = await listTasks(taskListId)
for (const task of allTasks) {
  const newBlocks = task.blocks.filter(id => id !== taskId)
  const newBlockedBy = task.blockedBy.filter(id => id !== taskId)
  if (newBlocks.length !== task.blocks.length ||
      newBlockedBy.length !== task.blockedBy.length) {
    await updateTask(taskListId, task.id, {
      blocks: newBlocks, blockedBy: newBlockedBy,
    })
  }
}
```

The full lifecycle of a dependency:
1. **Creation**: TaskUpdateTool calls `blockTask(listId, A, B)` to say "A blocks B"
2. **Visibility**: TaskListTool filters completed blockers from `blockedBy`
3. **Deletion**: `deleteTask` cascades removal from all referencing tasks

This design ensures eventual consistency: the persistent edges are always
correct, and the view layer (TaskListTool) provides the "auto-unblocking"
illusion by filtering at read time.
