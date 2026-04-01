# s13: Teams and Swarms

s01 > s02 > s03 > s04 > s05 | s06 > s07 > s08 > s09 > s10 | s11 > s12 > **[ s13 ]** s14

> "One agent is smart. Multiple agents working together need a protocol."

## Problem

A single agent runs tasks sequentially. When the user says "research the auth module, write tests, and fix the null pointer bug," that is three independent tasks wasted on one thread. Multi-agent coordination requires backend diversity (tmux, iTerm2, in-process), cross-agent communication, and a way to stop the leader from doing the work itself.

## Solution

Claude Code uses a backend-agnostic `TeammateExecutor`, a file-based mailbox for all communication, and a coordinator mode that strips the leader down to delegation-only tools.

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
                +---+----------+---+
                    |          |
          +---------v--+  +---v----------+
          | Researcher |  |    Tester    |
          | (thread)   |  |   (thread)   |
          +-----+------+  +------+-------+
                |                |
         +------v----------------v-------+
         |     File-based Mailbox        |
         |  ~/.claude/teams/{team}/      |
         |     inboxes/{name}.json       |
         +-------------------------------+
```

All communication goes through the mailbox, regardless of backend.

## How It Works

### 1. Backend abstraction

The `TeammateExecutor` interface defines the lifecycle every backend must implement: spawn, send, terminate (graceful), kill (forceful).

```python
# agents/s13_teams.py (simplified)

class TeammateExecutor(ABC):
    @abstractmethod
    def spawn(self, config: SpawnConfig) -> SpawnResult: ...

    @abstractmethod
    def terminate(self, agent_id: str, reason: str) -> bool: ...

    @abstractmethod
    def kill(self, agent_id: str) -> bool: ...
```

Three backends implement this: `InProcessBackend` (threads), `TmuxBackend` (panes), `ITermBackend` (tabs).

### 2. File-based mailbox

Each agent gets an inbox at `~/.claude/teams/{team}/inboxes/{name}.json`. File locking handles concurrent access. Files are the lowest common denominator for IPC since pane backends run teammates in separate processes.

```python
class Mailbox:
    def write(self, recipient, message, team_name):
        path = self._inbox_path(recipient, team_name)
        with _LOCK:
            existing = self._read_raw(path)
            existing.append(message.to_dict())
            path.write_text(json.dumps(existing))

    def read_unread(self, agent_name, team_name):
        with _LOCK:
            return [m for m in self._read_raw(path)
                    if not m.get("read")]
```

### 3. The teammate loop

Each teammate runs a polling loop: process initial prompt, go idle, poll the mailbox. Shutdown requests take priority over regular messages.

```python
def _teammate_loop(name, team, abort, mailbox):
    # 1. Process initial prompt
    # 2. Send idle_notification to leader
    # 3. Poll mailbox every 200ms
    while not abort.is_set():
        for msg in mailbox.read_unread(name, team):
            parsed = json.loads(msg["text"])
            if parsed.get("type") == "shutdown_request":
                # Approve and exit
                mailbox.write("team-lead", approval, team)
                return
            # Process regular message...
```

### 4. Coordinator mode

When `CLAUDE_CODE_COORDINATOR_MODE=1` is set, the leader loses file-editing tools and gets only `Agent`, `SendMessage`, and `TaskStop`. This forces delegation -- the leader cannot take shortcuts.

```python
def is_coordinator_mode():
    return os.environ.get("CLAUDE_CODE_COORDINATOR_MODE") == "1"
```

### 5. Message routing

The manager supports unicast (`to="researcher"`) and broadcast (`to="*"`). Broadcast skips the sender.

```python
def send_message(self, team_name, to, text, from_agent="team-lead"):
    if to == "*":
        for member in team.members:
            if member != from_agent:
                self.mailbox.write(member, msg, team_name)
    else:
        self.mailbox.write(to, msg, team_name)
```

## What Changed

| Component | Before (s12) | After (s13) |
|-----------|-------------|-------------|
| Agent count | Single agent per session | Multiple agents coordinated by a leader |
| Backend | N/A | Abstracted: tmux, iTerm2, or in-process |
| Communication | Centralized state only | File-based mailbox with locking |
| Lifecycle | N/A | Spawn, idle, follow-up, graceful/forceful shutdown |
| Leader role | Does everything | Coordinator mode: delegation-only tools |
| Routing | N/A | Unicast (`to=name`) and broadcast (`to="*"`) |
| Agent IDs | N/A | Namespaced: `name@team` |

## Try It

```bash
cd learn-the-actual-claude-code
python agents/s13_teams.py
```

Watch for:

- Team `demo-team` is created with a leader
- `researcher` and `tester` start in separate threads, report idle
- Coordinator sends a follow-up to `researcher`; `researcher` messages `tester` directly
- Leader broadcasts "wrap up" to all teammates
- Graceful shutdown: request sent, teammates approve, threads exit

Try setting `CLAUDE_CODE_COORDINATOR_MODE=1` before running. Then add a third teammate and see how broadcast reaches all of them.
