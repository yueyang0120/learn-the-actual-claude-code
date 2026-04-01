"""
Session 13 -- Teams & Swarms: Reimplementation

Demonstrates the core swarm architecture from Claude Code:
  - Backend-agnostic TeammateExecutor interface
  - InProcessSwarmBackend with thread-based context isolation
  - File-based mailbox messaging with locking
  - Coordinator mode delegation
  - 2-agent communication demo

Source mapping:
  backends/types.ts           -> BackendType, TeammateExecutor ABC, SpawnConfig/Result
  backends/InProcessBackend.ts -> InProcessSwarmBackend
  teammateMailbox.ts          -> Mailbox
  TeamCreateTool.ts           -> TeammateManager.create_team()
  SendMessageTool.ts          -> TeammateManager.send_message()
  coordinatorMode.ts          -> is_coordinator_mode(), get_coordinator_system_prompt()
  leaderPermissionBridge.ts   -> (permission bridge in InProcessSwarmBackend)
  inProcessRunner.ts          -> _teammate_loop()

Run:
    python sessions/s13-teams-and-swarms/reimplementation.py
"""

from __future__ import annotations

import json
import os
import threading
import time
import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Optional


# ---------------------------------------------------------------------------
# 1. Types  (mirrors src/utils/swarm/backends/types.ts)
# ---------------------------------------------------------------------------

class BackendType(Enum):
    TMUX = "tmux"
    ITERM2 = "iterm2"
    IN_PROCESS = "in-process"


@dataclass
class TeammateMessage:
    """Mirrors TeammateMessage in types.ts / teammateMailbox.ts."""
    text: str
    from_agent: str
    timestamp: str = ""
    color: str = ""
    summary: str = ""
    read: bool = False

    def to_dict(self) -> dict:
        return {
            "from": self.from_agent,
            "text": self.text,
            "timestamp": self.timestamp or _now(),
            "color": self.color,
            "summary": self.summary,
            "read": self.read,
        }


@dataclass
class SpawnConfig:
    """Mirrors TeammateSpawnConfig in types.ts."""
    name: str
    team_name: str
    prompt: str
    color: str = ""
    parent_session_id: str = ""
    model: str = ""


@dataclass
class SpawnResult:
    """Mirrors TeammateSpawnResult in types.ts."""
    success: bool
    agent_id: str
    error: str = ""
    task_id: str = ""


# ---------------------------------------------------------------------------
# 2. File-based mailbox  (mirrors src/utils/teammateMailbox.ts)
#
# Real implementation: JSON files at ~/.claude/teams/{team}/inboxes/{name}.json
# Uses proper-lockfile with 10 retries for concurrent access.
# ---------------------------------------------------------------------------

_MAILBOX_LOCK = threading.Lock()


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S")


class Mailbox:
    """
    File-based message store.  Each agent gets an inbox at:
        {base_dir}/{team}/inboxes/{agent_name}.json

    Uses a threading lock to simulate the file-locking strategy
    in the real codebase (proper-lockfile with retries).
    """

    def __init__(self, base_dir: str | Path):
        self.base_dir = Path(base_dir)

    def _inbox_path(self, agent_name: str, team_name: str) -> Path:
        return self.base_dir / team_name / "inboxes" / f"{agent_name}.json"

    def write(
        self,
        recipient: str,
        message: TeammateMessage,
        team_name: str,
    ) -> None:
        """Append message to inbox.  Mirrors writeToMailbox()."""
        path = self._inbox_path(recipient, team_name)
        path.parent.mkdir(parents=True, exist_ok=True)

        with _MAILBOX_LOCK:
            existing = self._read_raw(path)
            existing.append(message.to_dict())
            path.write_text(json.dumps(existing, indent=2))

    def read_unread(self, agent_name: str, team_name: str) -> list[dict]:
        """Return unread messages.  Mirrors readUnreadMessages()."""
        path = self._inbox_path(agent_name, team_name)
        with _MAILBOX_LOCK:
            messages = self._read_raw(path)
            return [m for m in messages if not m.get("read")]

    def mark_all_read(self, agent_name: str, team_name: str) -> None:
        """Mark every message as read.  Mirrors markMessagesAsRead()."""
        path = self._inbox_path(agent_name, team_name)
        with _MAILBOX_LOCK:
            messages = self._read_raw(path)
            for m in messages:
                m["read"] = True
            if messages:
                path.write_text(json.dumps(messages, indent=2))

    @staticmethod
    def _read_raw(path: Path) -> list[dict]:
        if not path.exists():
            return []
        try:
            return json.loads(path.read_text())
        except (json.JSONDecodeError, OSError):
            return []


# ---------------------------------------------------------------------------
# 3. TeammateExecutor interface  (mirrors types.ts TeammateExecutor)
# ---------------------------------------------------------------------------

class TeammateExecutor(ABC):
    """
    Backend-agnostic lifecycle manager for teammates.

    Real source has three concrete implementations:
      - InProcessBackend  (same Node.js process, AsyncLocalStorage isolation)
      - PaneBackendExecutor<TmuxBackend>  (tmux panes)
      - PaneBackendExecutor<ITermBackend> (iTerm2 tabs)
    """

    backend_type: BackendType

    @abstractmethod
    def is_available(self) -> bool: ...

    @abstractmethod
    def spawn(self, config: SpawnConfig) -> SpawnResult: ...

    @abstractmethod
    def send_message(self, agent_id: str, message: TeammateMessage) -> None: ...

    @abstractmethod
    def terminate(self, agent_id: str, reason: str = "") -> bool: ...

    @abstractmethod
    def kill(self, agent_id: str) -> bool: ...

    @abstractmethod
    def is_active(self, agent_id: str) -> bool: ...


# ---------------------------------------------------------------------------
# 4. InProcessSwarmBackend  (mirrors backends/InProcessBackend.ts)
#
# Real implementation uses:
#   - spawnInProcessTeammate() to create AbortController + register task
#   - startInProcessTeammate() -> runInProcessTeammate() for the agent loop
#   - AsyncLocalStorage for per-teammate context isolation
#   - writeToMailbox() for all messaging (same as pane backends)
# ---------------------------------------------------------------------------

@dataclass
class _RunningTeammate:
    agent_id: str
    name: str
    team_name: str
    thread: threading.Thread
    abort: threading.Event
    prompt: str


class InProcessSwarmBackend(TeammateExecutor):
    """Runs teammates as threads.  Real version uses AsyncLocalStorage."""

    backend_type = BackendType.IN_PROCESS

    def __init__(self, mailbox: Mailbox):
        self.mailbox = mailbox
        self._teammates: dict[str, _RunningTeammate] = {}

    def is_available(self) -> bool:
        return True  # In-process is always available

    def spawn(self, config: SpawnConfig) -> SpawnResult:
        agent_id = f"{config.name}@{config.team_name}"

        if agent_id in self._teammates:
            return SpawnResult(False, agent_id, error="Already spawned")

        abort_event = threading.Event()
        task_id = f"task-{uuid.uuid4().hex[:8]}"

        t = threading.Thread(
            target=_teammate_loop,
            args=(config.name, config.team_name, config.prompt,
                  abort_event, self.mailbox),
            daemon=True,
            name=f"teammate-{config.name}",
        )

        self._teammates[agent_id] = _RunningTeammate(
            agent_id=agent_id,
            name=config.name,
            team_name=config.team_name,
            thread=t,
            abort=abort_event,
            prompt=config.prompt,
        )

        # Send initial prompt via mailbox (same as PaneBackendExecutor)
        self.mailbox.write(
            config.name,
            TeammateMessage(
                text=config.prompt,
                from_agent="team-lead",
                color="blue",
                summary="initial task",
            ),
            config.team_name,
        )

        t.start()
        return SpawnResult(success=True, agent_id=agent_id, task_id=task_id)

    def send_message(self, agent_id: str, message: TeammateMessage) -> None:
        parts = agent_id.split("@", 1)
        if len(parts) != 2:
            raise ValueError(f"Invalid agentId: {agent_id} (expected name@team)")
        agent_name, team_name = parts
        self.mailbox.write(agent_name, message, team_name)

    def terminate(self, agent_id: str, reason: str = "") -> bool:
        """Graceful: sends shutdown_request via mailbox."""
        teammate = self._teammates.get(agent_id)
        if not teammate:
            return False

        shutdown_msg = json.dumps({
            "type": "shutdown_request",
            "requestId": f"shutdown-{uuid.uuid4().hex[:8]}",
            "from": "team-lead",
            "reason": reason,
            "timestamp": _now(),
        })
        self.mailbox.write(
            teammate.name,
            TeammateMessage(text=shutdown_msg, from_agent="team-lead"),
            teammate.team_name,
        )
        return True

    def kill(self, agent_id: str) -> bool:
        """Forceful: aborts the thread immediately."""
        teammate = self._teammates.pop(agent_id, None)
        if not teammate:
            return False
        teammate.abort.set()
        teammate.thread.join(timeout=2.0)
        return True

    def is_active(self, agent_id: str) -> bool:
        teammate = self._teammates.get(agent_id)
        if not teammate:
            return False
        return teammate.thread.is_alive() and not teammate.abort.is_set()


def _teammate_loop(
    name: str,
    team: str,
    initial_prompt: str,
    abort: threading.Event,
    mailbox: Mailbox,
) -> None:
    """
    Main loop for an in-process teammate.

    Mirrors inProcessRunner.ts runInProcessTeammate():
      while (!aborted && !shouldExit):
        1. runAgent() with prompt
        2. Mark idle, send idle_notification to leader
        3. waitForNextPromptOrShutdown() -- polls mailbox every 500ms
           Priority: shutdown > team-lead messages > peer messages
        4. On new message -> loop again
        5. On shutdown -> model decides approve/reject
        6. On abort -> exit
    """
    print(f"  [{name}] started, processing initial prompt")

    # Simulate work on the initial prompt
    time.sleep(0.3)

    # Send idle notification (mirrors teammateInit.ts Stop hook)
    idle_msg = json.dumps({
        "type": "idle_notification",
        "from": name,
        "timestamp": _now(),
        "idleReason": "available",
    })
    mailbox.write(
        "team-lead",
        TeammateMessage(text=idle_msg, from_agent=name, color="green"),
        team,
    )
    print(f"  [{name}] idle, polling for messages...")

    # Mark initial messages read
    mailbox.mark_all_read(name, team)

    # Poll loop -- mirrors waitForNextPromptOrShutdown()
    while not abort.is_set():
        time.sleep(0.2)
        unread = mailbox.read_unread(name, team)
        if not unread:
            continue

        for msg in unread:
            text = msg.get("text", "")
            sender = msg.get("from", "unknown")

            # Priority 1: check for shutdown request
            try:
                parsed = json.loads(text)
                if parsed.get("type") == "shutdown_request":
                    print(f"  [{name}] received shutdown request, approving")
                    approval = json.dumps({
                        "type": "shutdown_approved",
                        "requestId": parsed.get("requestId", ""),
                        "from": name,
                        "timestamp": _now(),
                    })
                    mailbox.write(
                        "team-lead",
                        TeammateMessage(text=approval, from_agent=name),
                        team,
                    )
                    mailbox.mark_all_read(name, team)
                    abort.set()
                    return
            except (json.JSONDecodeError, TypeError):
                pass

            # Regular message -- simulate processing
            print(f"  [{name}] received message from {sender}: "
                  f"{text[:60]}...")
            time.sleep(0.2)

            # Send acknowledgement back to peer (not team-lead)
            if sender != "team-lead" and sender != name:
                mailbox.write(
                    sender,
                    TeammateMessage(
                        text=f"Acknowledged from {name}: processed your message",
                        from_agent=name,
                        summary=f"{name} ack",
                    ),
                    team,
                )

        mailbox.mark_all_read(name, team)


# ---------------------------------------------------------------------------
# 5. Coordinator Mode  (mirrors src/coordinator/coordinatorMode.ts)
#
# Enabled via CLAUDE_CODE_COORDINATOR_MODE=1.
# Restricts the leader to Agent/SendMessage/TaskStop tools and injects
# a system prompt focused on orchestration, synthesis, and delegation.
# ---------------------------------------------------------------------------

COORDINATOR_SYSTEM_PROMPT = """\
You are a coordinator. Your job is to:
- Direct workers to research, implement and verify code changes
- Synthesize results and communicate with the user

Your tools:
- Agent       -- spawn a new worker
- SendMessage -- continue an existing worker
- TaskStop    -- stop a running worker

Workers have access to: Bash, Read, Edit, plus MCP tools.
Parallelism is your superpower -- fan out independent work.
"""


def is_coordinator_mode() -> bool:
    """Real source: checks env var behind a feature flag."""
    return os.environ.get("CLAUDE_CODE_COORDINATOR_MODE", "") == "1"


def get_coordinator_system_prompt() -> str:
    return COORDINATOR_SYSTEM_PROMPT


# ---------------------------------------------------------------------------
# 6. TeammateManager
#    Combines TeamCreateTool, SendMessageTool, and registry logic.
# ---------------------------------------------------------------------------

@dataclass
class TeamConfig:
    """Mirrors TeamFile in teamHelpers.ts."""
    name: str
    lead_agent_id: str
    members: list[str] = field(default_factory=list)
    created_at: float = 0.0


class TeammateManager:
    """
    High-level orchestrator combining:
      - TeamCreateTool (team creation, config.json, AppState)
      - SendMessageTool (routing: unicast, broadcast, structured)
      - Backend registry (getTeammateExecutor)
    """

    def __init__(self, backend: TeammateExecutor, mailbox: Mailbox):
        self.backend = backend
        self.mailbox = mailbox
        self.teams: dict[str, TeamConfig] = {}

    def create_team(self, team_name: str) -> TeamConfig:
        """Mirrors TeamCreateTool.call()."""
        lead_id = f"team-lead@{team_name}"
        config = TeamConfig(
            name=team_name,
            lead_agent_id=lead_id,
            members=[lead_id],
            created_at=time.time(),
        )
        self.teams[team_name] = config
        print(f"[Manager] Created team '{team_name}' (lead: {lead_id})")
        return config

    def spawn_teammate(
        self,
        team_name: str,
        name: str,
        prompt: str,
    ) -> SpawnResult:
        team = self.teams.get(team_name)
        if not team:
            raise ValueError(f"Team '{team_name}' does not exist")

        result = self.backend.spawn(SpawnConfig(
            name=name,
            team_name=team_name,
            prompt=prompt,
        ))

        if result.success:
            team.members.append(result.agent_id)
            print(f"[Manager] Spawned '{name}' in team '{team_name}'")
        return result

    def send_message(
        self,
        team_name: str,
        to: str,
        text: str,
        from_agent: str = "team-lead",
    ) -> None:
        """
        Mirrors SendMessageTool.call():
          - to="*" -> broadcast to all except sender
          - to=name -> unicast via mailbox
        """
        team = self.teams.get(team_name)
        if not team:
            raise ValueError(f"Team '{team_name}' does not exist")

        if to == "*":
            for member_id in team.members:
                member_name = member_id.split("@")[0]
                if member_name == from_agent:
                    continue
                self.mailbox.write(
                    member_name,
                    TeammateMessage(text=text, from_agent=from_agent),
                    team_name,
                )
            print(f"[Manager] Broadcast from {from_agent} to "
                  f"{len(team.members) - 1} member(s)")
        else:
            self.mailbox.write(
                to,
                TeammateMessage(text=text, from_agent=from_agent),
                team_name,
            )
            print(f"[Manager] Message from {from_agent} -> {to}")

    def shutdown_teammate(self, team_name: str, agent_name: str) -> bool:
        """Graceful: sends shutdown_request via backend.terminate()."""
        agent_id = f"{agent_name}@{team_name}"
        return self.backend.terminate(agent_id, reason="task complete")

    def kill_teammate(self, team_name: str, agent_name: str) -> bool:
        """Forceful: aborts immediately via backend.kill()."""
        agent_id = f"{agent_name}@{team_name}"
        return self.backend.kill(agent_id)

    def cleanup_team(self, team_name: str) -> None:
        """Mirrors cleanupTeamDirectories() + cleanupSessionTeams()."""
        team = self.teams.get(team_name)
        if not team:
            return
        for member_id in team.members:
            if member_id.startswith("team-lead@"):
                continue
            self.backend.kill(member_id)
        del self.teams[team_name]
        print(f"[Manager] Cleaned up team '{team_name}'")


# ---------------------------------------------------------------------------
# 7. Demo
# ---------------------------------------------------------------------------

def main() -> None:
    import tempfile

    print("=" * 60)
    print("Teams & Swarms -- Reimplementation Demo")
    print("=" * 60)

    with tempfile.TemporaryDirectory() as tmp:
        mailbox = Mailbox(tmp)
        backend = InProcessSwarmBackend(mailbox)
        manager = TeammateManager(backend, mailbox)

        # 1. Create a team
        print("\n--- Step 1: Create team ---")
        manager.create_team("demo-team")

        # 2. Spawn two teammates
        print("\n--- Step 2: Spawn teammates ---")
        manager.spawn_teammate(
            "demo-team", "researcher",
            "Research the auth module for null pointer issues. "
            "Report file paths and line numbers.",
        )
        manager.spawn_teammate(
            "demo-team", "tester",
            "Find all test files related to src/auth/. "
            "Report test structure and coverage gaps.",
        )

        # 3. Wait for idle notifications
        print("\n--- Step 3: Wait for idle notifications ---")
        time.sleep(1.0)

        leader_msgs = mailbox.read_unread("team-lead", "demo-team")
        for m in leader_msgs:
            text = m.get("text", "")
            try:
                parsed = json.loads(text)
                if parsed.get("type") == "idle_notification":
                    print(f"  Leader sees: {parsed['from']} is idle "
                          f"(reason: {parsed.get('idleReason', '?')})")
            except (json.JSONDecodeError, TypeError):
                pass
        mailbox.mark_all_read("team-lead", "demo-team")

        # 4. Coordinator delegates follow-up
        print("\n--- Step 4: Coordinator sends follow-up ---")
        if is_coordinator_mode():
            print("  (coordinator mode active)")
        else:
            print("  (coordinator mode off -- set "
                  "CLAUDE_CODE_COORDINATOR_MODE=1 to enable)")
        manager.send_message(
            "demo-team",
            "researcher",
            "Fix the null pointer in src/auth/validate.ts:42. "
            "Add a null check before user.id access.",
            from_agent="team-lead",
        )

        # 5. Inter-agent messaging
        print("\n--- Step 5: Inter-agent communication ---")
        manager.send_message(
            "demo-team",
            "tester",
            "Can you verify the fix in validate.ts once researcher is done?",
            from_agent="researcher",
        )

        time.sleep(1.0)

        # 6. Broadcast
        print("\n--- Step 6: Broadcast ---")
        manager.send_message(
            "demo-team",
            "*",
            "Wrapping up -- please finish current tasks",
            from_agent="team-lead",
        )

        time.sleep(0.5)

        # 7. Graceful shutdown
        print("\n--- Step 7: Graceful shutdown ---")
        manager.shutdown_teammate("demo-team", "researcher")
        manager.shutdown_teammate("demo-team", "tester")

        time.sleep(1.5)

        # Check for shutdown approvals
        leader_msgs = mailbox.read_unread("team-lead", "demo-team")
        for m in leader_msgs:
            text = m.get("text", "")
            try:
                parsed = json.loads(text)
                if parsed.get("type") == "shutdown_approved":
                    print(f"  Leader sees: {parsed['from']} approved shutdown")
            except (json.JSONDecodeError, TypeError):
                pass

        # 8. Cleanup
        print("\n--- Step 8: Cleanup ---")
        manager.cleanup_team("demo-team")

        print("\n" + "=" * 60)
        print("Demo complete.")
        print("=" * 60)


if __name__ == "__main__":
    main()
