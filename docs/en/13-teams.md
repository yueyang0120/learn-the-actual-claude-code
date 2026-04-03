# Chapter 13: Teams and Swarms

A single agent processes tasks sequentially. Subagents (Chapter 8) add concurrency within one session, but each subagent shares the parent's process and context window. When the work is truly independent -- researching one module while writing tests for another while fixing a bug in a third -- full isolation requires separate agent instances with their own context windows, tools, and execution environments. The teams system provides this: a leader-delegate architecture where multiple autonomous agents communicate through a file-based mailbox protocol.

## The Problem

Multi-agent coordination introduces three categories of difficulty. The first is backend diversity: some environments provide tmux for pane management, others provide iTerm2, and some have neither. The agent spawning mechanism must work across all of them, with an in-process fallback when no terminal multiplexer is available.

The second is communication. Agents running in separate processes (or separate panes) cannot share memory. They need a reliable inter-process communication channel that supports both directed messages (leader to specific teammate) and broadcasts (leader to all teammates), with proper concurrency control when multiple agents write simultaneously.

The third is role discipline. If the leader agent retains the ability to edit files and run commands directly, it will take shortcuts instead of delegating. The system must constrain the leader to delegation-only tools, forcing it to coordinate rather than execute.

## How Claude Code Solves It

### Backend abstraction

Two layers separate the low-level pane operations from the high-level agent lifecycle. `PaneBackend` handles terminal-specific operations (creating panes, sending keystrokes, reading output). `TeammateExecutor` handles agent lifecycle (spawn, communicate, terminate, kill).

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

Three backends implement these interfaces. `TmuxBackend` uses native tmux commands to create panes in a 30%/70% split for the leader and tiled layout for teammates. `ITermBackend` uses iTerm2's AppleScript API to create split panes. `InProcessBackend` runs teammates as threads within the leader's process, requiring no terminal multiplexer at all.

```typescript
// src/teams/backends/detection.ts
function selectBackend(): TeammateExecutor {
  if (process.env.IN_PROCESS_TEAMMATES) return new InProcessBackend();
  if (isTmuxAvailable()) return new TmuxBackend();
  if (isITermAvailable()) return new ITermBackend();
  return new InProcessBackend(); // fallback
}
```

The detection priority reflects a practical preference: in-process execution is forced when explicitly requested, tmux is preferred when available (it is the most common terminal multiplexer in development environments), iTerm2 is used when tmux is absent, and in-process execution serves as the universal fallback.

### File-based mailbox

All inter-agent communication flows through a file-based mailbox rooted at `~/.claude/teams/{team}/inboxes/{name}.json`. Each agent has its own inbox file. File locking serializes concurrent access, which is necessary because pane-based backends run teammates in separate processes.

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
      // Mark as read
      for (const m of unread) m.read = true;
      await writeJson(path, all);
      return unread.map(deserializeMessage);
    });
  }
}
```

Files are the lowest common denominator for IPC. They work whether the teammate is a separate process (tmux/iTerm2) or a thread (in-process). The file locking ensures that concurrent reads and writes do not corrupt the inbox, even when multiple agents attempt to communicate simultaneously.

### Message types: plain text and structured protocol

Messages come in two categories. Plain text messages carry task instructions and natural-language responses. Structured protocol messages form a discriminated union that governs the agent lifecycle.

```typescript
// src/teams/messages.ts
type StructuredMessage =
  | { type: "idle_notification" }       // teammate is ready for work
  | { type: "shutdown_request" }        // leader requests graceful stop
  | { type: "shutdown_approved" };      // teammate acknowledges shutdown

interface TeamMessage {
  from: string;
  to: string;              // agent name or "*" for broadcast
  text: string;            // plain text or JSON-serialized structured message
  timestamp: number;
}
```

The `idle_notification` message is critical to the coordination protocol. After a teammate finishes its initial prompt, it sends this message to the leader, signaling that it is available for follow-up work. Without this message, the leader would need to poll or guess when teammates become free.

### Coordinator mode

When `CLAUDE_CODE_COORDINATOR_MODE=1` is set in the environment, the leader agent's tool set is restricted to three tools: `Agent` (for spawning subagents), `SendMessage` (for communicating with teammates), and `TaskStop` (for halting tasks). All file-editing, code-running, and file-reading tools are removed.

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

This constraint is deliberate. A leader with full tool access tends to "do the work itself" rather than delegating, especially for tasks that seem small. Removing the tools forces delegation, which is the entire point of the multi-agent architecture.

### Routing: unicast and broadcast

The `SendMessageTool` supports two routing modes. A message with `to` set to a specific agent name delivers to that agent's inbox only. A message with `to` set to `"*"` broadcasts to all team members, skipping the sender.

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

The skip-sender behavior for broadcasts prevents an agent from receiving its own message. Without this guard, a broadcast sender would immediately see its own message in its inbox on the next poll, creating a confusing feedback loop.

### The teammate polling loop

Each teammate runs a continuous loop: process the initial prompt, send an idle notification to the leader, then poll the mailbox every 200ms for new messages. Shutdown requests take priority over regular messages.

```typescript
// src/teams/teammateLoop.ts
async function teammateLoop(
  name: string,
  team: string,
  initialPrompt: string,
  abort: AbortSignal
): Promise<void> {
  // Phase 1: Process initial prompt
  await processPrompt(initialPrompt);

  // Phase 2: Signal readiness
  await mailbox.write("team-lead", {
    from: name,
    to: "team-lead",
    text: JSON.stringify({ type: "idle_notification" }),
    timestamp: Date.now(),
  }, team);

  // Phase 3: Poll for work
  while (!abort.aborted) {
    const messages = await mailbox.readUnread(name, team);
    for (const msg of messages) {
      const parsed = tryParseStructured(msg.text);
      if (parsed?.type === "shutdown_request") {
        await mailbox.write("team-lead", approvalMessage, team);
        return; // exit cleanly
      }
      await processPrompt(msg.text);
    }
    await sleep(200);
  }
}
```

The 200ms polling interval balances responsiveness against resource consumption. A shorter interval wastes CPU on filesystem access. A longer interval introduces noticeable latency between the leader sending a message and the teammate picking it up.

### Permission bridging and initialization

Teammates need permission decisions (Chapter 5) but run in separate contexts. The leader's permission bridge is reused: when a teammate encounters a permission prompt, it routes the decision back to the leader's native permission UI. This avoids the problem of multiple permission dialogs appearing simultaneously in different panes.

During initialization, each teammate receives the team-wide permission configuration and registers a `Stop` hook. The hook ensures that when a teammate's session ends, it sends a structured shutdown acknowledgment rather than silently disappearing.

## Key Design Decisions

**File-based mailbox instead of TCP sockets or shared memory.** Files work across all backends (processes, threads, containers) without requiring network configuration. The tradeoff is higher latency per message compared to sockets, but at the 200ms polling granularity, the filesystem access overhead is negligible.

**Coordinator mode as an environment variable rather than a setting.** The flag needs to be readable before settings are loaded, since it affects tool registration during bootstrap. An environment variable is available immediately at process start.

**Three structured message types instead of a richer protocol.** The lifecycle protocol needs only three signals: "I am idle," "please stop," and "I acknowledge the stop." Additional complexity (heartbeats, progress reports, error codes) can be expressed as plain text messages interpreted by the model rather than by protocol machinery.

**200ms polling instead of filesystem watches.** Filesystem watch APIs (`inotify`, `FSEvents`) are platform-specific and have edge cases with file locking. A 200ms poll is universally reliable and introduces at most 200ms of latency, which is imperceptible in the context of LLM response times measured in seconds.

## In Practice

A user asks Claude Code to "research the auth module, write tests for it, and fix the null-pointer bug -- work on all three in parallel." The leader creates a team with three teammates: `researcher`, `tester`, and `fixer`. Each teammate spawns in its own tmux pane (or thread, if tmux is unavailable) and begins processing its initial prompt.

The researcher finishes first and sends an `idle_notification`. The leader, operating in coordinator mode, receives it and sends a follow-up message asking the researcher to review the tester's progress. The tester, meanwhile, finishes writing tests and sends its own `idle_notification`. The leader broadcasts a "wrap up and report findings" message to all teammates.

When the leader is satisfied, it sends a `shutdown_request` to each teammate. Each teammate receives the request, sends a `shutdown_approved` response, and exits its loop. The leader collects the final results and presents a unified summary to the user.

## Summary

- A two-layer abstraction separates terminal-specific pane operations (`PaneBackend`) from agent lifecycle management (`TeammateExecutor`), with three implementations: tmux, iTerm2, and in-process.
- File-based mailboxes at `~/.claude/teams/{team}/inboxes/{name}.json` with file locking provide reliable IPC across all backends, supporting both unicast and broadcast routing.
- Three structured protocol messages (`idle_notification`, `shutdown_request`, `shutdown_approved`) govern the teammate lifecycle, while plain text messages carry task instructions.
- Coordinator mode (`CLAUDE_CODE_COORDINATOR_MODE=1`) restricts the leader to delegation-only tools, preventing it from executing work directly.
- Teammates run a continuous polling loop (200ms interval) that processes initial prompts, signals readiness, handles follow-up messages, and responds to graceful shutdown requests.
