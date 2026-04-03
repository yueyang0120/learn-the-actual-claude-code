# 第 13 章: 团队与集群

单个 agent 按顺序处理任务。Subagent (第 8 章) 在一个会话内增加并发, 但每个 subagent 共享父进程的进程和 context window。当工作是真正独立的 -- 研究一个模块的同时为另一个写测试, 同时修第三个的 bug -- 完全隔离需要独立的 agent 实例, 各自拥有 context window、工具和执行环境。上一章状态管理将所有可变数据集中到单一 store; 本章的团队系统则将执行本身分散到多个自治 agent, 通过基于文件的邮箱协议通信。

## 问题

多 agent 协调引入三类困难。

第一是后端多样性: 有些环境提供 tmux 做面板管理, 有些提供 iTerm2, 有些两者都没有。Agent 的启动机制必须跨所有环境工作, 并在没有终端复用器时提供进程内回退。

第二是通信。运行在独立进程 (或独立面板) 中的 agent 无法共享内存。需要一个可靠的进程间通信通道, 支持定向消息 (leader 到特定队友) 和广播 (leader 到所有队友), 并在多个 agent 同时写入时有恰当的并发控制。

第三是角色纪律。如果 leader agent 保留直接编辑文件和运行命令的能力, 它会走捷径而非委派。系统必须将 leader 限制为仅有委派工具, 强制它协调而非执行。

## Claude Code 的解法

### 后端抽象

两层架构将底层面板操作与高层 agent 生命周期分离。`PaneBackend` 处理终端特定操作 (创建面板、发送按键、读取输出)。`TeammateExecutor` 处理 agent 生命周期 (spawn, communicate, terminate, kill)。

```typescript
// src/teams/backends/types.ts
interface PaneBackend {
  createPane(config: PaneConfig): Promise<PaneHandle>;
  sendInput(pane: PaneHandle, text: string): void;
  readOutput(pane: PaneHandle): string;
  closePane(pane: PaneHandle): void;
}

interface TeammateExecutor {
  spawn(config: SpawnConfig): Promise<SpawnResult>;
  terminate(agentId: string, reason: string): Promise<boolean>;
  kill(agentId: string): Promise<boolean>;
}
```

三个后端实现这些接口。`TmuxBackend` 使用原生 tmux 命令创建面板, leader 占 30%, 队友平铺。`ITermBackend` 使用 iTerm2 的 AppleScript API 创建分割面板。`InProcessBackend` 将队友作为线程运行在 leader 进程内, 完全不需要终端复用器。

```typescript
// src/teams/backends/detection.ts
function selectBackend(): TeammateExecutor {
  if (process.env.IN_PROCESS_TEAMMATES) return new InProcessBackend();
  if (isTmuxAvailable()) return new TmuxBackend();
  if (isITermAvailable()) return new ITermBackend();
  return new InProcessBackend(); // 回退
}
```

检测优先级反映实际偏好: 显式请求时强制进程内执行; tmux 可用时优先 (开发环境中最常见的终端复用器); tmux 不可用时使用 iTerm2; 进程内执行作为兜底。

### 基于文件的邮箱

所有 agent 间通信通过文件邮箱流转, 根目录为 `~/.claude/teams/{team}/inboxes/{name}.json`。每个 agent 拥有自己的 inbox 文件。文件锁序列化并发访问 -- 面板后端的队友运行在独立进程中, 这是必要的。

```typescript
// src/teams/mailbox.ts
class FileMailbox {
  async write(
    recipient: string,
    message: TeamMessage,
    teamName: string
  ): Promise<void> {
    const path = this.inboxPath(recipient, teamName);
    await withFileLock(path, async () => {
      const existing = await readJson(path) ?? [];
      existing.push(message.serialize());
      await writeJson(path, existing);
    });
  }

  async readUnread(
    agentName: string,
    teamName: string
  ): Promise<TeamMessage[]> {
    const path = this.inboxPath(agentName, teamName);
    return withFileLock(path, async () => {
      const all = await readJson(path) ?? [];
      const unread = all.filter(m => !m.read);
      for (const m of unread) m.read = true;
      await writeJson(path, all);
      return unread.map(deserializeMessage);
    });
  }
}
```

文件是 IPC 的最低公分母。无论队友是独立进程 (tmux/iTerm2) 还是线程 (进程内), 文件都能工作。文件锁确保并发读写不会破坏 inbox, 即使多个 agent 同时通信。

### 消息类型: 纯文本与结构化协议

消息分为两类。纯文本消息承载任务指令和自然语言回复。结构化协议消息构成一个区分联合, 管控 agent 生命周期。

```typescript
// src/teams/messages.ts
type StructuredMessage =
  | { type: "idle_notification" }       // 队友就绪, 可接受工作
  | { type: "shutdown_request" }        // leader 请求优雅关闭
  | { type: "shutdown_approved" };      // 队友确认关闭

interface TeamMessage {
  from: string;
  to: string;              // agent 名称或 "*" 表示广播
  text: string;            // 纯文本或 JSON 序列化的结构化消息
  timestamp: number;
}
```

`idle_notification` 消息对协调协议至关重要。队友完成初始 prompt 后发送此消息给 leader, 表示可以接受后续工作。没有此消息, leader 只能通过轮询或猜测来判断队友何时空闲。

### 协调者模式

当环境变量 `CLAUDE_CODE_COORDINATOR_MODE=1` 设置时, leader agent 的工具集被限制为三个: `Agent` (启动 subagent), `SendMessage` (与队友通信), `TaskStop` (停止任务)。所有文件编辑、代码运行和文件读取工具被移除。

```typescript
// src/teams/coordinator.ts
function getCoordinatorTools(allTools: Tool[]): Tool[] {
  if (process.env.CLAUDE_CODE_COORDINATOR_MODE !== "1") {
    return allTools;
  }
  const allowed = new Set(["Agent", "SendMessage", "TaskStop"]);
  return allTools.filter(t => allowed.has(t.name));
}
```

这个约束是刻意的。拥有全部工具的 leader 倾向于"自己干" 而非委派, 尤其对看起来小的任务。移除工具强制委派, 这正是多 agent 架构的核心目的。

### 路由: 单播与广播

`SendMessageTool` 支持两种路由模式。`to` 设为特定 agent 名时, 仅投递到该 agent 的 inbox。`to` 设为 `"*"` 时, 广播到所有团队成员, 跳过发送者自身。

```typescript
// src/teams/SendMessageTool.ts
async function sendMessage(
  teamName: string,
  to: string,
  text: string,
  from: string
): Promise<void> {
  const msg: TeamMessage = { from, to, text, timestamp: Date.now() };

  if (to === "*") {
    const members = await getTeamMembers(teamName);
    for (const member of members) {
      if (member !== from) {
        await mailbox.write(member, msg, teamName);
      }
    }
  } else {
    await mailbox.write(to, msg, teamName);
  }
}
```

广播时跳过发送者防止 agent 收到自己的消息。不加这个保护, 广播发送者会在下次轮询时立即看到自己的消息, 产生混乱的反馈循环。

### 队友轮询循环

每个队友运行持续循环: 处理初始 prompt, 发送 idle 通知给 leader, 然后每 200ms 轮询邮箱获取新消息。Shutdown 请求优先于普通消息。

```typescript
// src/teams/teammateLoop.ts
async function teammateLoop(
  name: string,
  team: string,
  initialPrompt: string,
  abort: AbortSignal
): Promise<void> {
  // 阶段 1: 处理初始 prompt
  await processPrompt(initialPrompt);

  // 阶段 2: 发出就绪信号
  await mailbox.write("team-lead", {
    from: name,
    to: "team-lead",
    text: JSON.stringify({ type: "idle_notification" }),
    timestamp: Date.now(),
  }, team);

  // 阶段 3: 轮询等待工作
  while (!abort.aborted) {
    const messages = await mailbox.readUnread(name, team);
    for (const msg of messages) {
      const parsed = tryParseStructured(msg.text);
      if (parsed?.type === "shutdown_request") {
        await mailbox.write("team-lead", approvalMessage, team);
        return; // 干净退出
      }
      await processPrompt(msg.text);
    }
    await sleep(200);
  }
}
```

200ms 轮询间隔在响应性和资源消耗之间取得平衡。更短的间隔浪费 CPU 在文件系统访问上。更长的间隔在 leader 发送消息和队友接收之间引入可感知的延迟。

### 权限桥接与初始化

队友需要权限决策 (第 5 章) 但运行在独立 context 中。Leader 的权限桥被复用: 当队友遇到权限提示时, 将决策路由回 leader 的原生权限 UI。这避免了多个权限对话框在不同面板同时出现的问题。

初始化时, 每个队友接收团队范围的权限配置并注册 `Stop` hook。该 hook 确保队友会话结束时发送结构化的关闭确认, 而非悄无声息地消失。

## 关键设计决策

**基于文件的邮箱而非 TCP socket 或共享内存。** 文件在所有后端 (进程、线程、容器) 之间工作, 不需要网络配置。代价是每条消息的延迟高于 socket, 但在 200ms 轮询粒度下, 文件系统访问开销可以忽略。

**协调者模式通过环境变量而非设置。** 该标志需要在设置加载之前可读, 因为它在 bootstrap 阶段就影响工具注册。环境变量在进程启动时立即可用。

**三种结构化消息类型而非更丰富的协议。** 生命周期协议只需三个信号: "空闲"、"请停止"、"确认停止"。额外的复杂性 (心跳、进度报告、错误码) 可以作为纯文本消息表达, 由模型而非协议机制解释。

**200ms 轮询而非文件系统监视。** 文件系统监视 API (`inotify`, `FSEvents`) 是平台特定的, 与文件锁有边界情况。200ms 轮询普遍可靠, 最多引入 200ms 延迟, 在以秒计的 LLM 响应时间面前几乎无法感知。

## 实际体验

用户要求 Claude Code "研究 auth 模块, 为它写测试, 修复空指针 bug -- 三件事并行做"。Leader 创建一个包含三个队友的团队: `researcher`, `tester`, `fixer`。每个队友在自己的 tmux 面板 (或线程, 如果 tmux 不可用) 中启动, 开始处理初始 prompt。

Researcher 最先完成, 发送 `idle_notification`。以协调者模式运行的 leader 收到后, 发送后续消息请 researcher 检查 tester 的进度。Tester 此时完成测试编写, 发送自己的 `idle_notification`。Leader 广播 "收尾并汇报" 消息给所有队友。

Leader 满意后, 向每个队友发送 `shutdown_request`。每个队友收到请求, 回复 `shutdown_approved`, 退出循环。Leader 收集最终结果, 向用户呈现统一摘要。

## 总结

- 两层抽象将终端特定的面板操作 (`PaneBackend`) 与 agent 生命周期管理 (`TeammateExecutor`) 分离, 有三种实现: tmux, iTerm2, 进程内。
- 基于文件的邮箱位于 `~/.claude/teams/{team}/inboxes/{name}.json`, 配合文件锁提供跨所有后端的可靠 IPC, 支持单播和广播路由。
- 三种结构化协议消息 (`idle_notification`, `shutdown_request`, `shutdown_approved`) 管控队友生命周期, 纯文本消息承载任务指令。
- 协调者模式 (`CLAUDE_CODE_COORDINATOR_MODE=1`) 将 leader 限制为仅有委派工具, 阻止其直接执行工作。
- 队友运行持续轮询循环 (200ms 间隔), 处理初始 prompt, 发出就绪信号, 处理后续消息, 响应优雅关闭请求。
