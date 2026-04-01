# s13: Teams and Swarms

s01 > s02 > s03 > s04 > s05 | s06 > s07 > s08 > s09 > s10 | s11 > s12 > **[ s13 ]** s14

> "One agent is smart. Multiple agents working together need a protocol."

## 问题

单个 agent 只能顺序执行任务。用户说"研究 auth 模块、写测试、修那个空指针 bug"，这明明是三个独立任务，全排在一个线程上太浪费了。多 agent 协调需要后端多样性（tmux、iTerm2、进程内）、跨 agent 通信，还要有办法防止 leader 自己偷偷干活。

## 解决方案

Claude Code 用一个与后端无关的 `TeammateExecutor`，一个基于文件的邮箱处理所有通信，再加一个协调者模式 -- 把 leader 精简到只剩委派工具。

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

所有通信都走邮箱，不管用的是哪个后端。

## 工作原理

### 1. 后端抽象

`TeammateExecutor` 接口定义了每个后端必须实现的生命周期：spawn（启动）、terminate（优雅关闭）、kill（强制关闭）。

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

三个后端实现了它：`InProcessBackend`（线程）、`TmuxBackend`（面板）、`ITermBackend`（标签页）。

### 2. 基于文件的邮箱

每个 agent 在 `~/.claude/teams/{team}/inboxes/{name}.json` 有一个 inbox 文件。文件锁处理并发访问。用文件而不是内存队列，是因为面板后端的队友跑在独立进程里 -- 文件是 IPC 的最低公分母。

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

### 3. 队友循环

每个队友跑一个轮询循环：处理初始 prompt，进入空闲，轮询邮箱。关闭请求优先于普通消息。

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

### 4. 协调者模式

设置 `CLAUDE_CODE_COORDINATOR_MODE=1` 后，leader 失去文件编辑工具，只剩 `Agent`、`SendMessage` 和 `TaskStop`。这样就强制它只做委派，没法自己偷偷干活。

```python
def is_coordinator_mode():
    return os.environ.get("CLAUDE_CODE_COORDINATOR_MODE") == "1"
```

### 5. 消息路由

manager 支持单播（`to="researcher"`）和广播（`to="*"`）。广播会跳过发送者自己。

```python
def send_message(self, team_name, to, text, from_agent="team-lead"):
    if to == "*":
        for member in team.members:
            if member != from_agent:
                self.mailbox.write(member, msg, team_name)
    else:
        self.mailbox.write(to, msg, team_name)
```

## 变更内容

| 组件 | s12 | s13 |
|------|-----|-----|
| Agent 数量 | 每个会话一个 agent | 多个 agent，由 leader 协调 |
| 后端 | 无 | 抽象化: tmux、iTerm2 或进程内 |
| 通信 | 只有集中状态 | 基于文件的邮箱 + 锁 |
| 生命周期 | 无 | spawn、空闲、后续任务、优雅/强制关闭 |
| Leader 角色 | 什么都干 | 协调者模式: 只能委派 |
| 路由 | 无 | 单播 (`to=name`) 和广播 (`to="*"`) |
| Agent ID | 无 | 命名空间化: `name@team` |

## 试一试

```bash
cd learn-the-actual-claude-code
python agents/s13_teams.py
```

留意这些输出：

- 团队 `demo-team` 创建，带一个 leader
- `researcher` 和 `tester` 在独立线程启动，报告空闲
- 协调者给 `researcher` 发后续任务；`researcher` 直接给 `tester` 发消息
- Leader 广播"收工"给所有队友
- 优雅关闭：发请求，队友批准，线程退出

试试运行前设 `CLAUDE_CODE_COORDINATOR_MODE=1`，再加个第三队友，看广播怎么送达所有人。
