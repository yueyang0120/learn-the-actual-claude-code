# Session 13 -- Teams and Swarms

s01 > s02 > s03 > s04 > s05 | s06 > s07 > s08 > s09 > s10 | s11 > s12 > **s13** > s14

> "One agent is smart. Multiple agents working together need a protocol."
>
> *Harness layer: Claude Code's swarm system is backend-agnostic -- the same orchestration logic runs whether teammates live in tmux panes, iTerm2 tabs, or threads inside the same process. Coordination happens through file-based mailboxes with proper locking.*

---

## Problem

A single agent can only do one thing at a time. When a user says "research the auth module, write tests, and fix the null pointer bug" -- that is three independent tasks. Running them sequentially wastes time.

Multi-agent orchestration introduces hard problems:

1. **Backend diversity** -- Some users have tmux. Some use iTerm2. Some run headless CI. The swarm system cannot assume a specific terminal multiplexer.
2. **Communication** -- Agents need to send messages to each other. How do you coordinate without shared memory?
3. **Lifecycle** -- Agents must start, idle, receive follow-ups, and shut down gracefully. What happens when a teammate refuses a shutdown request?
4. **The coordinator problem** -- If the leader agent can also read files and write code, it might do the work itself instead of delegating. How do you constrain it to only orchestrate?

---

## Solution

Claude Code solves this with four components: a `TeammateExecutor` abstraction, a file-based mailbox, a connection manager, and a coordinator mode that strips the leader down to delegation-only tools.

```
                    +------------------+
                    |   User prompt    |
                    +--------+---------+
                             |
                    +--------v---------+
                    |  Leader Agent    |
                    |  (coordinator)   |
                    |  Tools: Agent,   |
                    |  SendMessage,    |
                    |  TaskStop only   |
                    +---+---------+----+
                        |         |
              +---------v--+  +---v----------+
              | Researcher |  |   Tester     |
              | (thread)   |  |  (thread)    |
              +-----+------+  +------+-------+
                    |                |
             +------v----------------v-------+
             |     File-based Mailbox        |
             |  ~/.claude/teams/{team}/      |
             |     inboxes/{name}.json       |
             +-------------------------------+
```

The critical insight: **all communication goes through the mailbox**, regardless of backend. Whether a teammate runs as a tmux pane or an in-process thread, it reads and writes the same JSON inbox files.

---

## How It Works

### 1. Backend Abstraction -- TeammateExecutor

The `TeammateExecutor` ABC defines the lifecycle contract that every backend must implement:

```python
class BackendType(Enum):
    TMUX = "tmux"
    ITERM2 = "iterm2"
    IN_PROCESS = "in-process"

class TeammateExecutor(ABC):
    """
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
```

Note the two shutdown paths: `terminate` (graceful, via mailbox) and `kill` (forceful, immediate abort). The teammate gets a chance to approve or reject a graceful shutdown.

Source: `src/utils/swarm/backends/types.ts`

### 2. File-Based Mailbox

Each agent gets an inbox file at `~/.claude/teams/{team}/inboxes/{name}.json`. The mailbox uses file locking (proper-lockfile in the real source, a threading lock in our reimplementation) to handle concurrent access:

```python
class Mailbox:
    def __init__(self, base_dir: str | Path):
        self.base_dir = Path(base_dir)

    def _inbox_path(self, agent_name: str, team_name: str) -> Path:
        return self.base_dir / team_name / "inboxes" / f"{agent_name}.json"

    def write(self, recipient: str, message: TeammateMessage, team_name: str) -> None:
        """Append message to inbox. Mirrors writeToMailbox()."""
        path = self._inbox_path(recipient, team_name)
        path.parent.mkdir(parents=True, exist_ok=True)
        with _MAILBOX_LOCK:
            existing = self._read_raw(path)
            existing.append(message.to_dict())
            path.write_text(json.dumps(existing, indent=2))

    def read_unread(self, agent_name: str, team_name: str) -> list[dict]:
        """Return unread messages. Mirrors readUnreadMessages()."""
        path = self._inbox_path(agent_name, team_name)
        with _MAILBOX_LOCK:
            messages = self._read_raw(path)
            return [m for m in messages if not m.get("read")]
```

Why files instead of in-memory queues? Because the pane backends (tmux, iTerm2) run teammates in **separate processes**. Files are the lowest common denominator for IPC.

Source: `src/utils/teammateMailbox.ts`

### 3. The Teammate Loop

Each in-process teammate runs a polling loop that mirrors the real `runInProcessTeammate()`. It processes messages with a priority system: shutdown requests take precedence over regular messages:

```python
def _teammate_loop(
    name: str, team: str, initial_prompt: str,
    abort: threading.Event, mailbox: Mailbox,
) -> None:
    """
    Mirrors inProcessRunner.ts runInProcessTeammate():
      while (!aborted && !shouldExit):
        1. runAgent() with prompt
        2. Mark idle, send idle_notification to leader
        3. waitForNextPromptOrShutdown() -- polls mailbox every 500ms
        4. On new message -> loop again
        5. On shutdown -> model decides approve/reject
        6. On abort -> exit
    """
    print(f"  [{name}] started, processing initial prompt")
    time.sleep(0.3)

    # Send idle notification
    idle_msg = json.dumps({
        "type": "idle_notification",
        "from": name,
        "timestamp": _now(),
        "idleReason": "available",
    })
    mailbox.write("team-lead", TeammateMessage(
        text=idle_msg, from_agent=name, color="green",
    ), team)

    mailbox.mark_all_read(name, team)

    # Poll loop
    while not abort.is_set():
        time.sleep(0.2)
        unread = mailbox.read_unread(name, team)
        if not unread:
            continue
        for msg in unread:
            text = msg.get("text", "")
            # Priority 1: check for shutdown request
            try:
                parsed = json.loads(text)
                if parsed.get("type") == "shutdown_request":
                    # Approve and exit
                    approval = json.dumps({
                        "type": "shutdown_approved",
                        "requestId": parsed.get("requestId", ""),
                        "from": name,
                    })
                    mailbox.write("team-lead", TeammateMessage(
                        text=approval, from_agent=name,
                    ), team)
                    abort.set()
                    return
            except (json.JSONDecodeError, TypeError):
                pass
            # Regular message -- process it
            ...
        mailbox.mark_all_read(name, team)
```

The structured JSON messages (`idle_notification`, `shutdown_request`, `shutdown_approved`) form a simple protocol layered on top of the text mailbox.

Source: `src/utils/swarm/inProcessRunner.ts`

### 4. Coordinator Mode

When `CLAUDE_CODE_COORDINATOR_MODE=1` is set, the leader agent gets a restricted tool set and a focused system prompt:

```python
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
    return os.environ.get("CLAUDE_CODE_COORDINATOR_MODE", "") == "1"
```

By removing file-editing tools from the coordinator, the system forces delegation. The leader cannot take shortcuts by doing the work itself.

Source: `src/coordinator/coordinatorMode.ts`

### 5. The TeammateManager -- Orchestration API

The manager combines team creation, teammate spawning, and message routing into a single interface:

```python
class TeammateManager:
    def __init__(self, backend: TeammateExecutor, mailbox: Mailbox):
        self.backend = backend
        self.mailbox = mailbox
        self.teams: dict[str, TeamConfig] = {}

    def send_message(
        self, team_name: str, to: str, text: str,
        from_agent: str = "team-lead",
    ) -> None:
        """
        to="*" -> broadcast to all except sender
        to=name -> unicast via mailbox
        """
        team = self.teams.get(team_name)
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
        else:
            self.mailbox.write(
                to,
                TeammateMessage(text=text, from_agent=from_agent),
                team_name,
            )
```

The broadcast pattern (`to="*"`) is particularly useful for the coordinator to signal "wrap up" to all teammates at once.

Source: `TeamCreateTool.ts`, `SendMessageTool.ts`

---

## What Changed

| Component | Before | After |
|-----------|--------|-------|
| Agent count | Single agent per session | Multiple agents coordinated by a leader |
| Backend | Assumed terminal type | Abstracted: tmux, iTerm2, or in-process |
| Communication | N/A (single agent) | File-based mailbox with locking at `~/.claude/teams/` |
| Lifecycle | Start and stop | Spawn, idle notification, follow-up, graceful/forceful shutdown |
| Shutdown | Immediate kill | Two-phase: request + approve/reject, then force if needed |
| Leader role | Does everything | Coordinator mode: delegation-only (Agent, SendMessage, TaskStop) |
| Message routing | N/A | Unicast (`to=name`) and broadcast (`to="*"`) |
| Agent IDs | N/A | Namespaced: `name@team` for collision-free identification |

---

## Try It

```bash
# Run the teams and swarms demo
python agents/s13_teams.py
```

What to watch for in the output:

1. **Team creation** -- a team named `demo-team` is created with a leader
2. **Teammate spawn** -- `researcher` and `tester` start in separate threads
3. **Idle notifications** -- both teammates report back to the leader when ready
4. **Follow-up messages** -- the coordinator sends a targeted task to `researcher`
5. **Inter-agent communication** -- `researcher` sends a message directly to `tester`
6. **Broadcast** -- the leader sends "wrap up" to all teammates at once
7. **Graceful shutdown** -- shutdown requests are sent, teammates approve and exit
8. **Cleanup** -- the team directory and all state are removed

Try setting `CLAUDE_CODE_COORDINATOR_MODE=1` before running to see the coordinator system prompt activate. Then try adding a third teammate and observe how broadcast messages reach all of them.
