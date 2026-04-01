# Session 13 -- 团队与集群

s01 > s02 > s03 > s04 > s05 | s06 > s07 > s08 > s09 > s10 | s11 > s12 > **s13** > s14

> "One agent is smart. Multiple agents working together need a protocol."
> -- "单个 agent 很聪明。多个 agent 协同工作则需要一套协议。"
>
> *Harness 层: Claude Code 的集群系统与后端无关 -- 无论队友运行在 tmux 面板、iTerm2 标签还是同一进程内的线程中，相同的编排逻辑都能工作。协调通过基于文件的邮箱配合适当的锁机制完成。*

---

## 问题

单个 agent 一次只能做一件事。当用户说"研究 auth 模块、编写测试、修复空指针 bug"时 -- 这是三个独立的任务。顺序执行会浪费时间。

多 agent 编排引入了一些困难问题：

1. **后端多样性** -- 有些用户使用 tmux，有些使用 iTerm2，有些在无头 CI 环境中运行。集群系统不能假设特定的终端复用器。
2. **通信** -- agent 之间需要互相发送消息。如何在没有共享内存的情况下进行协调？
3. **生命周期** -- agent 必须能启动、空闲、接收后续任务并优雅关闭。当队友拒绝关闭请求时会发生什么？
4. **协调者问题** -- 如果领导者 agent 也能读取文件和编写代码，它可能会自己做工作而不是委派任务。如何约束它只进行编排？

---

## 解决方案

Claude Code 通过四个组件解决了这些问题：`TeammateExecutor` 抽象层、基于文件的邮箱、连接管理器，以及将领导者精简为仅有委派工具的协调者模式。

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

关键洞见：**所有通信都通过邮箱进行**，无论后端是什么。不管队友是作为 tmux 面板还是进程内线程运行，它都读写相同的 JSON inbox 文件。

---

## 工作原理

### 1. 后端抽象 -- TeammateExecutor

`TeammateExecutor` ABC 定义了每个后端必须实现的生命周期契约：

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

注意两种关闭路径：`terminate`（优雅关闭，通过邮箱）和 `kill`（强制关闭，立即中止）。队友有机会批准或拒绝优雅关闭。

来源: `src/utils/swarm/backends/types.ts`

### 2. 基于文件的邮箱

每个 agent 在 `~/.claude/teams/{team}/inboxes/{name}.json` 有一个 inbox 文件。邮箱使用文件锁（真实源码中使用 proper-lockfile，我们的重新实现中使用 threading lock）来处理并发访问：

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

为什么使用文件而不是内存队列？因为面板后端（tmux、iTerm2）在**独立进程**中运行队友。文件是进程间通信的最低公分母。

来源: `src/utils/teammateMailbox.ts`

### 3. 队友循环

每个进程内队友运行一个轮询循环，对应真实源码中的 `runInProcessTeammate()`。它通过优先级系统处理消息：关闭请求优先于普通消息：

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

结构化的 JSON 消息（`idle_notification`、`shutdown_request`、`shutdown_approved`）形成了一个建立在文本邮箱之上的简单协议。

来源: `src/utils/swarm/inProcessRunner.ts`

### 4. 协调者模式

当设置 `CLAUDE_CODE_COORDINATOR_MODE=1` 时，领导者 agent 获得一组受限的工具集和专注的系统提示词：

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

通过从协调者中移除文件编辑工具，系统强制进行委派。领导者不能通过自己做工作来走捷径。

来源: `src/coordinator/coordinatorMode.ts`

### 5. TeammateManager -- 编排 API

管理器将团队创建、队友生成和消息路由组合到一个单一接口中：

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

广播模式（`to="*"`）特别适用于协调者向所有队友同时发送"收工"信号。

来源: `TeamCreateTool.ts`、`SendMessageTool.ts`

---

## 变化对比

| 组件 | 之前 | 之后 |
|------|------|------|
| Agent 数量 | 每个会话单个 agent | 由领导者协调的多个 agent |
| 后端 | 假设特定终端类型 | 抽象化: tmux、iTerm2 或进程内 |
| 通信 | 无（单个 agent） | 基于文件的邮箱，在 `~/.claude/teams/` 使用锁机制 |
| 生命周期 | 启动和停止 | 生成、空闲通知、后续任务、优雅/强制关闭 |
| 关闭方式 | 立即 kill | 两阶段: 请求 + 批准/拒绝，然后必要时强制关闭 |
| 领导者角色 | 什么都做 | 协调者模式: 仅委派 (Agent, SendMessage, TaskStop) |
| 消息路由 | 无 | 单播 (`to=name`) 和广播 (`to="*"`) |
| Agent ID | 无 | 命名空间化: `name@team`，防止冲突的标识方式 |

---

## 试一试

```bash
# Run the teams and swarms demo
python agents/s13_teams.py
```

输出中需要关注的要点：

1. **团队创建** -- 创建了一个名为 `demo-team` 的团队和一个领导者
2. **队友生成** -- `researcher` 和 `tester` 在独立线程中启动
3. **空闲通知** -- 两个队友准备就绪后向领导者报告
4. **后续消息** -- 协调者向 `researcher` 发送了针对性任务
5. **agent 间通信** -- `researcher` 直接向 `tester` 发送消息
6. **广播** -- 领导者向所有队友同时发送"收工"
7. **优雅关闭** -- 发送关闭请求，队友批准并退出
8. **清理** -- 团队目录和所有状态被移除

尝试在运行前设置 `CLAUDE_CODE_COORDINATOR_MODE=1` 来查看协调者系统提示词的激活效果。然后尝试添加第三个队友，观察广播消息如何到达所有队友。
