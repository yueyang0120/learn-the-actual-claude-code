"""
Task System reimplementation -- educational Python version.

Demonstrates the core patterns from Claude Code's task system:
  - Typed tasks with lifecycle state machine (pending -> running -> completed/failed/killed)
  - Dependency DAG with blocks/blockedBy and auto-unblocking
  - Disk-backed output streaming
  - Background task execution via threading
  - CRUD operations: create, update, list, get, stop

Real source references:
  src/Task.ts                              -- TaskType, TaskStatus, TaskStateBase
  src/utils/tasks.ts                       -- createTask, updateTask, blockTask, listTasks
  src/utils/task/diskOutput.ts             -- DiskTaskOutput, getTaskOutput
  src/tools/TaskCreateTool/TaskCreateTool.ts
  src/tools/TaskUpdateTool/TaskUpdateTool.ts
  src/tools/TaskListTool/TaskListTool.ts
  src/tools/TaskOutputTool/TaskOutputTool.tsx
  src/tools/TaskStopTool/TaskStopTool.ts
  src/tasks/stopTask.ts                    -- stopTask dispatch
"""

from __future__ import annotations
import os
import time
import tempfile
import threading
from enum import Enum
from dataclasses import dataclass, field
from typing import Optional, Callable

# ---------------------------------------------------------------------------
# 1. Core types -- mirrors src/Task.ts
# ---------------------------------------------------------------------------

class TaskType(Enum):
    """Seven task variants in Claude Code.  We model three for this demo."""
    # Real: local_bash, local_agent, remote_agent, in_process_teammate,
    #        local_workflow, monitor_mcp, dream
    LOCAL_BASH   = "local_bash"
    LOCAL_AGENT  = "local_agent"
    REMOTE_AGENT = "remote_agent"

# Prefix per type for human-readable IDs -- mirrors TASK_ID_PREFIXES
TASK_ID_PREFIXES = {
    TaskType.LOCAL_BASH:   "b",
    TaskType.LOCAL_AGENT:  "a",
    TaskType.REMOTE_AGENT: "r",
}

class TaskStatus(Enum):
    """Lifecycle states: pending -> running -> completed | failed | killed."""
    PENDING   = "pending"
    RUNNING   = "running"
    COMPLETED = "completed"
    FAILED    = "failed"
    KILLED    = "killed"

TERMINAL_STATUSES = {TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.KILLED}

def is_terminal(status: TaskStatus) -> bool:
    """Guard against interacting with dead tasks -- mirrors isTerminalTaskStatus."""
    return status in TERMINAL_STATUSES


# ---------------------------------------------------------------------------
# 2. TaskState -- mirrors TaskStateBase + the V2 Task schema from utils/tasks
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# 3. Disk output -- simplified DiskTaskOutput from utils/task/diskOutput.ts
# ---------------------------------------------------------------------------

class DiskTaskOutput:
    """Append-only output file for a task, with offset-based delta reads.
    Real version uses O_NOFOLLOW, 5 GB cap, and an async drain loop."""

    def __init__(self, path: str):
        self._path = path
        self._lock = threading.Lock()
        # Create empty file
        with open(path, "w") as f:
            pass

    def append(self, content: str) -> None:
        with self._lock:
            with open(self._path, "a") as f:
                f.write(content)

    def read_all(self) -> str:
        with self._lock:
            with open(self._path, "r") as f:
                return f.read()

    def read_delta(self, offset: int) -> tuple[str, int]:
        """Read new bytes from offset.  Returns (content, new_offset)."""
        with self._lock:
            with open(self._path, "r") as f:
                f.seek(offset)
                content = f.read()
            new_offset = offset + len(content.encode("utf-8"))
            return content, new_offset


# ---------------------------------------------------------------------------
# 4. TaskManager -- combines utils/tasks.ts CRUD + the five tool call methods
# ---------------------------------------------------------------------------

class TaskManager:
    """Central task registry.  In Claude Code the persistence is file-based JSON
    under ~/.claude/tasks/<taskListId>/ with proper-lockfile concurrency."""

    def __init__(self):
        self._tasks: dict[str, TaskState] = {}
        self._next_id: dict[TaskType, int] = {}
        self._output_dir = tempfile.mkdtemp(prefix="task_output_")
        self._outputs: dict[str, DiskTaskOutput] = {}

    # -- ID generation (mirrors generateTaskId) --
    def _gen_id(self, task_type: TaskType) -> str:
        n = self._next_id.get(task_type, 0) + 1
        self._next_id[task_type] = n
        prefix = TASK_ID_PREFIXES.get(task_type, "x")
        return f"{prefix}{n}"

    # -- Output management --
    def _init_output(self, task_id: str) -> DiskTaskOutput:
        path = os.path.join(self._output_dir, f"{task_id}.output")
        out = DiskTaskOutput(path)
        self._outputs[task_id] = out
        return out

    def get_output(self, task_id: str) -> str:
        """Read full output -- mirrors getTaskOutput."""
        out = self._outputs.get(task_id)
        return out.read_all() if out else ""

    def get_output_delta(self, task_id: str) -> str:
        """Read output since last call -- mirrors getTaskOutputDelta."""
        task = self._tasks.get(task_id)
        out = self._outputs.get(task_id)
        if not task or not out:
            return ""
        content, new_offset = out.read_delta(task.output_offset)
        task.output_offset = new_offset
        return content

    # -------------------------------------------------------------------
    # TaskCreateTool -- mirrors src/tools/TaskCreateTool/TaskCreateTool.ts
    # -------------------------------------------------------------------
    def create(
        self,
        task_type: TaskType,
        subject: str,
        description: str,
        owner: Optional[str] = None,
    ) -> str:
        """Create a new task with pending status."""
        tid = self._gen_id(task_type)
        out = self._init_output(tid)
        task = TaskState(
            id=tid,
            task_type=task_type,
            status=TaskStatus.PENDING,
            subject=subject,
            description=description,
            owner=owner,
            output_file=out._path,
        )
        self._tasks[tid] = task
        print(f"  [create] Task #{tid} created: {subject}")
        return tid

    # -------------------------------------------------------------------
    # TaskUpdateTool -- mirrors src/tools/TaskUpdateTool/TaskUpdateTool.ts
    # -------------------------------------------------------------------
    def update(
        self,
        task_id: str,
        status: Optional[TaskStatus] = None,
        subject: Optional[str] = None,
        description: Optional[str] = None,
        owner: Optional[str] = None,
        add_blocks: Optional[list[str]] = None,
        add_blocked_by: Optional[list[str]] = None,
    ) -> bool:
        """Update a task.  Handles dependency edges via block_task."""
        task = self._tasks.get(task_id)
        if not task:
            print(f"  [update] Task #{task_id} not found")
            return False

        updated: list[str] = []
        if subject is not None and subject != task.subject:
            task.subject = subject
            updated.append("subject")
        if description is not None and description != task.description:
            task.description = description
            updated.append("description")
        if owner is not None and owner != task.owner:
            task.owner = owner
            updated.append("owner")
        if status is not None and status != task.status:
            task.status = status
            updated.append("status")
            if is_terminal(status):
                task.end_time = time.time()

        # Dependency edges -- mirrors TaskUpdateTool addBlocks/addBlockedBy
        if add_blocks:
            for blocked_id in add_blocks:
                if blocked_id not in task.blocks:
                    self.block_task(task_id, blocked_id)
            updated.append("blocks")
        if add_blocked_by:
            for blocker_id in add_blocked_by:
                if blocker_id not in task.blocked_by:
                    self.block_task(blocker_id, task_id)
            updated.append("blocked_by")

        print(f"  [update] Task #{task_id}: {', '.join(updated)}")
        return True

    # -------------------------------------------------------------------
    # blockTask -- mirrors src/utils/tasks.ts blockTask()
    # -------------------------------------------------------------------
    def block_task(self, from_id: str, to_id: str) -> bool:
        """A blocks B: write bidirectional edges."""
        a = self._tasks.get(from_id)
        b = self._tasks.get(to_id)
        if not a or not b:
            return False
        if to_id not in a.blocks:
            a.blocks.append(to_id)
        if from_id not in b.blocked_by:
            b.blocked_by.append(from_id)
        return True

    # -------------------------------------------------------------------
    # TaskListTool -- mirrors src/tools/TaskListTool/TaskListTool.ts
    # -------------------------------------------------------------------
    def list_tasks(self) -> list[dict]:
        """List all tasks, filtering completed blockers from blocked_by.
        This 'auto-unblocking' is a read-time filter, not a write-time mutation."""
        completed_ids = {
            tid for tid, t in self._tasks.items()
            if t.status == TaskStatus.COMPLETED
        }
        result = []
        for t in self._tasks.values():
            live_blockers = [bid for bid in t.blocked_by if bid not in completed_ids]
            result.append({
                "id": t.id,
                "subject": t.subject,
                "status": t.status.value,
                "owner": t.owner,
                "blocked_by": live_blockers,
            })
        return result

    # -------------------------------------------------------------------
    # get -- mirrors getTask from utils/tasks.ts
    # -------------------------------------------------------------------
    def get(self, task_id: str) -> Optional[TaskState]:
        return self._tasks.get(task_id)

    # -------------------------------------------------------------------
    # TaskStopTool -- mirrors src/tools/TaskStopTool + tasks/stopTask.ts
    # -------------------------------------------------------------------
    def stop(self, task_id: str) -> bool:
        """Stop a running task.  Dispatches to type-specific kill."""
        task = self._tasks.get(task_id)
        if not task:
            print(f"  [stop] Task #{task_id} not found")
            return False
        if task.status != TaskStatus.RUNNING:
            print(f"  [stop] Task #{task_id} is not running ({task.status.value})")
            return False

        # Cancel the background work
        if task._cancel:
            task._cancel()
        task.status = TaskStatus.KILLED
        task.end_time = time.time()
        task.notified = True  # suppress noisy notification for bash kills
        print(f"  [stop] Task #{task_id} killed")
        return True

    # -------------------------------------------------------------------
    # Delete with cascade -- mirrors deleteTask in utils/tasks.ts
    # -------------------------------------------------------------------
    def delete(self, task_id: str) -> bool:
        """Delete a task and cascade-remove its dependency edges."""
        if task_id not in self._tasks:
            return False
        del self._tasks[task_id]
        # Cascade: remove edges referencing this task
        for t in self._tasks.values():
            t.blocks = [bid for bid in t.blocks if bid != task_id]
            t.blocked_by = [bid for bid in t.blocked_by if bid != task_id]
        print(f"  [delete] Task #{task_id} deleted, edges cascaded")
        return True

    # -------------------------------------------------------------------
    # Background execution -- simulates what spawners do (LocalShellTask etc.)
    # -------------------------------------------------------------------
    def run_in_background(
        self,
        task_id: str,
        work: Callable[[DiskTaskOutput], Optional[str]],
    ) -> None:
        """Execute a task function in a background thread.
        Real Claude Code uses child processes (bash) or subagent loops."""
        task = self._tasks.get(task_id)
        if not task:
            return
        out = self._outputs.get(task_id)
        if not out:
            return
        cancel_event = threading.Event()
        task._cancel = cancel_event.set

        def _worker():
            task.status = TaskStatus.RUNNING
            try:
                # Pass a cancel-check to the work function via closure
                result = work(out)
                if cancel_event.is_set():
                    return  # killed mid-flight
                task.status = TaskStatus.COMPLETED
                if result:
                    out.append(f"\n=== RESULT ===\n{result}\n")
            except Exception as e:
                task.status = TaskStatus.FAILED
                out.append(f"\n=== ERROR ===\n{e}\n")
            finally:
                task.end_time = time.time()

        t = threading.Thread(target=_worker, daemon=True)
        t.start()


# ---------------------------------------------------------------------------
# 5. Demo
# ---------------------------------------------------------------------------

def demo():
    print("=" * 60)
    print("Task System Demo")
    print("=" * 60)
    mgr = TaskManager()

    # --- Create tasks with a dependency chain ---
    print("\n-- Creating tasks --")
    t1 = mgr.create(TaskType.LOCAL_BASH, "Run unit tests", "pytest tests/")
    t2 = mgr.create(TaskType.LOCAL_BASH, "Run integration tests", "pytest tests/integration/")
    t3 = mgr.create(TaskType.LOCAL_AGENT, "Review test results", "Analyze failures and suggest fixes")

    # t3 is blocked by t1 and t2 (must finish tests before review)
    print("\n-- Setting up dependency DAG --")
    mgr.update(t3, add_blocked_by=[t1, t2])

    # Show initial task list
    print("\n-- Task list (initial) --")
    for item in mgr.list_tasks():
        blocked = f" [blocked by {item['blocked_by']}]" if item["blocked_by"] else ""
        print(f"  #{item['id']} [{item['status']}] {item['subject']}{blocked}")

    # --- Run t1 in background ---
    print("\n-- Running task t1 in background --")
    def unit_test_work(out: DiskTaskOutput) -> Optional[str]:
        out.append("Running pytest tests/...\n")
        time.sleep(0.3)
        out.append("test_auth.py ... PASSED\n")
        time.sleep(0.2)
        out.append("test_api.py ... PASSED\n")
        out.append("2 passed, 0 failed\n")
        return "All unit tests passed"

    mgr.run_in_background(t1, unit_test_work)
    time.sleep(0.1)  # Let thread start

    # Check delta output while running
    print("\n-- Reading output delta (while running) --")
    time.sleep(0.2)
    delta = mgr.get_output_delta(t1)
    print(f"  Delta: {delta.strip()!r}")

    # Wait for t1 to finish
    time.sleep(0.5)

    # --- Complete t1, run and complete t2 ---
    print("\n-- Completing tasks --")
    def integration_work(out: DiskTaskOutput) -> Optional[str]:
        out.append("Running integration tests...\n")
        time.sleep(0.2)
        out.append("test_e2e.py ... PASSED\n")
        return "Integration tests passed"

    mgr.run_in_background(t2, integration_work)
    time.sleep(0.5)  # Wait for t2 to finish

    # Show task list -- t3 should be auto-unblocked (blockers completed)
    print("\n-- Task list (after t1 and t2 completed) --")
    for item in mgr.list_tasks():
        blocked = f" [blocked by {item['blocked_by']}]" if item["blocked_by"] else ""
        print(f"  #{item['id']} [{item['status']}] {item['subject']}{blocked}")

    # --- Read full output ---
    print("\n-- Full output of t1 --")
    output = mgr.get_output(t1)
    for line in output.strip().split("\n"):
        print(f"  | {line}")

    # --- Demonstrate stop (kill) ---
    print("\n-- Demonstrating task stop --")
    t4 = mgr.create(TaskType.LOCAL_BASH, "Long running build", "make all")
    def long_work(out: DiskTaskOutput) -> Optional[str]:
        for i in range(50):
            out.append(f"Building step {i}...\n")
            time.sleep(0.1)
        return "Build complete"

    mgr.run_in_background(t4, long_work)
    time.sleep(0.3)
    mgr.stop(t4)

    # --- Demonstrate delete with cascade ---
    print("\n-- Demonstrating delete with edge cascade --")
    t5 = mgr.create(TaskType.LOCAL_BASH, "Setup DB", "docker compose up")
    t6 = mgr.create(TaskType.LOCAL_BASH, "Seed data", "python seed.py")
    mgr.update(t6, add_blocked_by=[t5])
    print(f"  Before delete: t6 blocked_by = {mgr.get(t6).blocked_by}")
    mgr.delete(t5)
    print(f"  After delete:  t6 blocked_by = {mgr.get(t6).blocked_by}")

    # --- Final task list ---
    print("\n-- Final task list --")
    for item in mgr.list_tasks():
        blocked = f" [blocked by {item['blocked_by']}]" if item["blocked_by"] else ""
        owner = f" ({item['owner']})" if item["owner"] else ""
        print(f"  #{item['id']} [{item['status']}] {item['subject']}{owner}{blocked}")

    print("\n" + "=" * 60)
    print("Demo complete.")
    print("=" * 60)


if __name__ == "__main__":
    demo()
