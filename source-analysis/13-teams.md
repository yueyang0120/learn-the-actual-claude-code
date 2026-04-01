# Source Analysis -- Teams and Swarms

## 1. Backend Type System (`backends/types.ts`)

The foundation is a set of TypeScript types that decouple teammate
lifecycle from any specific terminal multiplexer.

```
BackendType = 'tmux' | 'iterm2' | 'in-process'
```

Two separate interfaces serve different layers:

### PaneBackend -- low-level pane operations

Used only by pane-based backends (tmux, iTerm2). Deals with raw
terminal panes: create, kill, hide/show, send keystrokes, set colors.

```ts
type PaneBackend = {
  readonly type: BackendType
  isAvailable(): Promise<boolean>
  isRunningInside(): Promise<boolean>
  createTeammatePaneInSwarmView(name, color): Promise<CreatePaneResult>
  sendCommandToPane(paneId, command, useExternalSession?): Promise<void>
  setPaneBorderColor(paneId, color, ...): Promise<void>
  setPaneTitle(paneId, name, color, ...): Promise<void>
  enablePaneBorderStatus(...): Promise<void>
  rebalancePanes(windowTarget, hasLeader): Promise<void>
  killPane(paneId, ...): Promise<boolean>
  hidePane(paneId, ...): Promise<boolean>
  showPane(paneId, target, ...): Promise<boolean>
}
```

### TeammateExecutor -- high-level lifecycle

The interface that callers actually use. Works across all backends
(pane and in-process):

```ts
type TeammateExecutor = {
  readonly type: BackendType
  isAvailable(): Promise<boolean>
  spawn(config: TeammateSpawnConfig): Promise<TeammateSpawnResult>
  sendMessage(agentId, message: TeammateMessage): Promise<void>
  terminate(agentId, reason?): Promise<boolean>   // graceful
  kill(agentId): Promise<boolean>                  // force
  isActive(agentId): Promise<boolean>
}
```

### Spawn configuration

`TeammateSpawnConfig` bundles identity, prompt, cwd, model, system
prompt, permissions, and parent session ID. `TeammateSpawnResult`
returns success, agentId, and backend-specific handles (AbortController
for in-process, paneId for pane-based).

---

## 2. TmuxBackend (`backends/TmuxBackend.ts`)

Implements PaneBackend using tmux CLI commands. Two operating modes:

### Inside tmux (native)

Leader occupies the left 30% of the window. Teammates are split into
the right 70%. Layout uses `main-vertical` with the leader pane resized
to 30%. First teammate splits horizontally from the leader; subsequent
teammates alternate between vertical and horizontal splits from existing
teammate panes.

### Outside tmux (external session)

Creates a dedicated `claude-swarm-{pid}` socket with a session named
`claude-swarm` and a window named `swarm-view`. All teammates share a
`tiled` layout (no leader pane visible).

Key implementation details:
- **Pane creation lock**: A promise-chain mutex prevents race conditions
  when multiple teammates spawn in parallel.
- **Shell init delay**: 200ms wait after pane creation for shell RC files.
- **Color mapping**: Translates agent color names to tmux color values
  (e.g., `purple` -> `magenta`, `orange` -> `colour208`).
- **Self-registration**: Module-level side effect calls
  `registerTmuxBackend(TmuxBackend)` to avoid circular deps.

---

## 3. ITermBackend (`backends/ITermBackend.ts`)

Implements PaneBackend using `it2` CLI (iTerm2's Python API bridge).

- First teammate: vertical split (`-v`) from the leader's session
  (extracted from `ITERM_SESSION_ID` env var).
- Subsequent teammates: horizontal split from the last teammate's session.
- **At-fault recovery**: If the targeted session is dead (user closed
  the pane), the backend prunes the stale ID and retries. Bounded at
  O(N+1) iterations.
- **Cosmetic no-ops**: `setPaneBorderColor` and `setPaneTitle` are
  no-ops because each `it2` call spawns a Python process (slow).
- `hidePane`/`showPane` return false (not supported).
- `killPane` uses `it2 session close -f` (force flag required because
  iTerm2 otherwise prompts for confirmation).

---

## 4. InProcessBackend (`backends/InProcessBackend.ts`)

Implements TeammateExecutor directly (not PaneBackend -- no terminal
panes involved).

### spawn()

1. Calls `spawnInProcessTeammate()` to create context and register task
   in AppState.
2. If successful, calls `startInProcessTeammate()` to kick off the
   agent execution loop in a fire-and-forget background promise.
3. Returns agentId, taskId, and AbortController.

### sendMessage()

Parses `agentId` (format: `name@team`), then calls `writeToMailbox()`.
All backends use the same file-based mailbox for messaging.

### terminate()

Sends a `shutdown_request` structured message to the teammate's mailbox.
Sets `shutdownRequested` on the task state. The teammate model decides
whether to approve or reject shutdown.

### kill()

Aborts the teammate's AbortController immediately, updates task state
to `killed`, removes from team file.

### isActive()

Checks that the AppState task exists, has status `running`, and its
AbortController is not aborted.

---

## 5. PaneBackendExecutor (`backends/PaneBackendExecutor.ts`)

An adapter that wraps any PaneBackend to implement TeammateExecutor.

### spawn()

1. Assigns a color via `assignTeammateColor()`.
2. Calls `backend.createTeammatePaneInSwarmView()` to create a pane.
3. Builds a full CLI command with teammate identity flags
   (`--agent-id`, `--agent-name`, `--team-name`, `--agent-color`,
   `--parent-session-id`, `--plan-mode-required`).
4. Sends the command to the pane via `backend.sendCommandToPane()`.
5. Sends the initial prompt to the teammate's mailbox.
6. Registers cleanup to kill all panes on leader exit.

### sendMessage(), terminate()

Both use `writeToMailbox()` -- the same mechanism as InProcessBackend.
Terminate sends a `shutdown_request` structured message.

### kill()

Looks up the paneId from the internal `spawnedTeammates` map and calls
`backend.killPane()`.

---

## 6. Backend Registry and Detection (`backends/registry.ts`, `detection.ts`)

### Detection priority

```
1. Inside tmux         -> TmuxBackend (native)
2. In iTerm2 + it2 CLI -> ITermBackend (native)
3. In iTerm2 - it2     -> TmuxBackend (fallback) + needsIt2Setup flag
4. tmux available      -> TmuxBackend (external session)
5. Nothing available   -> error with platform-specific install instructions
```

### In-process vs. pane decision

`isInProcessEnabled()` checks:
- `teammateMode === 'in-process'` -> always in-process
- `teammateMode === 'tmux'` -> always pane-based
- `teammateMode === 'auto'` (default):
  - Non-interactive session (`-p` mode) -> in-process
  - Inside tmux or iTerm2 -> pane-based
  - Otherwise -> in-process

### getTeammateExecutor()

The single entry point. Returns InProcessBackend or PaneBackendExecutor
based on the resolved mode. Caches instances.

---

## 7. Coordinator Mode (`coordinator/coordinatorMode.ts`)

Enabled via `CLAUDE_CODE_COORDINATOR_MODE=1` env var (gated behind
`COORDINATOR_MODE` feature flag).

### What it does

Injects a detailed system prompt that redefines the leader as a
**coordinator** with three tools:
- `Agent` -- spawn a worker
- `SendMessage` -- continue an existing worker
- `TaskStop` -- stop a running worker

The prompt includes:
- **Task workflow phases**: Research -> Synthesis -> Implementation -> Verification
- **Concurrency guidance**: Read tasks in parallel, write tasks serialized
- **Worker prompt quality rules**: Self-contained, synthesized, no lazy delegation
- **Continue vs. spawn heuristics**: Based on context overlap

### getCoordinatorUserContext()

Returns a `workerToolsContext` string listing what tools workers have
access to. Includes MCP server names and scratchpad directory if
available.

### matchSessionMode()

When resuming a session, flips `CLAUDE_CODE_COORDINATOR_MODE` to match
the stored session mode. Returns a warning message if the mode changed.

---

## 8. TeamCreateTool (`tools/TeamCreateTool/TeamCreateTool.ts`)

Creates a new team. Steps:

1. Validates team_name is non-empty.
2. Checks not already leading a team (one team per leader).
3. Generates unique name if collision exists.
4. Creates `TeamFile` with leader as first member.
5. Writes config.json under `~/.claude/teams/{team}/`.
6. Registers for session cleanup (auto-remove on leader exit).
7. Resets task list directory for the team.
8. Updates AppState with `teamContext` (teamName, leadAgentId, teammates map).

The team file structure:

```ts
type TeamFile = {
  name: string
  description?: string
  createdAt: number
  leadAgentId: string           // "team-lead@teamName"
  leadSessionId?: string
  hiddenPaneIds?: string[]
  teamAllowedPaths?: TeamAllowedPath[]
  members: Array<{
    agentId, name, agentType?, model?, prompt?, color?,
    planModeRequired?, joinedAt, tmuxPaneId, cwd,
    worktreePath?, sessionId?, subscriptions, backendType?,
    isActive?, mode?
  }>
}
```

---

## 9. SendMessageTool (`tools/SendMessageTool/SendMessageTool.ts`)

The central messaging hub. Handles multiple message types:

### Plain text messages

- To a specific teammate: `writeToMailbox(recipientName, ...)`
- Broadcast (`to: "*"`): iterates all team members, writes to each inbox
- To an in-process subagent (by agentId or name): queues via
  `queuePendingMessage()` or auto-resumes stopped agents

### Structured messages (discriminated union)

| Type | Direction | Purpose |
|------|-----------|---------|
| `shutdown_request` | Leader -> Teammate | Ask teammate to shut down |
| `shutdown_response` (approve) | Teammate -> Leader | Approve shutdown, then abort self |
| `shutdown_response` (reject) | Teammate -> Leader | Reject shutdown with reason |
| `plan_approval_response` (approve) | Leader -> Teammate | Approve plan, inherit permission mode |
| `plan_approval_response` (reject) | Leader -> Teammate | Reject plan with feedback |

### Cross-session messaging

When `UDS_INBOX` feature is enabled:
- `bridge:{session-id}` -> sends via Remote Control bridge
- `uds:{socket-path}` -> sends via Unix domain socket

### Validation rules

- `to` must not be empty or contain `@` (no agentId format)
- Structured messages cannot be broadcast
- Shutdown responses must target `team-lead`
- Rejection requires a reason
- Summary required for plain text messages

---

## 10. File-Based Mailbox (`utils/teammateMailbox.ts`)

Each teammate has an inbox at:
```
~/.claude/teams/{team}/inboxes/{agent_name}.json
```

### Core operations

| Function | Purpose |
|----------|---------|
| `writeToMailbox()` | Append message with file locking (proper-lockfile, 10 retries) |
| `readMailbox()` | Read all messages |
| `readUnreadMessages()` | Filter for `read: false` |
| `markMessageAsReadByIndex()` | Mark single message read (with lock) |
| `markMessagesAsRead()` | Mark all messages read (with lock) |
| `clearMailbox()` | Reset to `[]` |

### Structured protocol messages

The mailbox carries many typed JSON messages beyond plain text:

| Type | Purpose |
|------|---------|
| `idle_notification` | Teammate finished, went idle |
| `permission_request` / `permission_response` | Tool permission delegation |
| `sandbox_permission_request` / `sandbox_permission_response` | Network access delegation |
| `shutdown_request` / `shutdown_approved` / `shutdown_rejected` | Graceful shutdown protocol |
| `plan_approval_request` / `plan_approval_response` | Plan mode workflow |
| `task_assignment` | Task assigned from task list |
| `team_permission_update` | Broadcast permission rule to all teammates |
| `mode_set_request` | Leader sets a teammate's permission mode |

`isStructuredProtocolMessage()` checks the `type` field to determine if
a message should be routed by `useInboxPoller` rather than consumed as
raw text by the LLM.

---

## 11. Leader Permission Bridge (`swarm/leaderPermissionBridge.ts`)

A module-level singleton that lets in-process teammates reuse the
leader's native permission UI.

The REPL registers two functions:
- `setToolUseConfirmQueue` -- pushes a `ToolUseConfirm` entry into the
  React queue, showing the standard permission dialog with a worker badge.
- `setToolPermissionContext` -- writes back permission updates so
  "always allow" applies to the leader's shared context.

When the bridge is available, in-process teammates get the same
BashPermissionRequest / FileEditToolDiff UI as the leader's own tools.
When unavailable (e.g., non-interactive mode), falls back to
mailbox-based permission polling.

---

## 12. In-Process Runner (`swarm/inProcessRunner.ts`)

The main execution loop for in-process teammates. Architecture:

```
startInProcessTeammate(config)
  -> runInProcessTeammate(config)     // fire-and-forget
       -> while (!aborted && !shouldExit):
            1. runAgent() within runWithTeammateContext + runWithAgentContext
            2. Mark task idle, send idle notification via mailbox
            3. waitForNextPromptOrShutdown()
               - Polls mailbox every 500ms
               - Checks in-memory pendingUserMessages
               - Tries to claim unclaimed tasks from task list
               - Prioritizes: shutdown > team-lead messages > peer messages
            4. On new message: wrap in XML, loop again
            5. On shutdown request: pass to model for decision
            6. On abort: exit loop
```

### Compaction support

When accumulated messages exceed the auto-compact threshold, the runner
compacts the history via `compactConversation()`, then resets
`allMessages` and the content replacement state.

### Permission handling

`createInProcessCanUseTool()` returns a CanUseToolFn that:
1. Runs `hasPermissionsToUseTool()` first.
2. If result is `allow` or `deny`, passes through.
3. If `ask`:
   a. For bash, tries classifier auto-approval first.
   b. If bridge available: pushes to leader's `ToolUseConfirm` queue
      with a `workerBadge`.
   c. If bridge unavailable: sends `permission_request` to leader's
      mailbox, polls own mailbox for response at 500ms intervals.

---

## 13. Teammate Initialization (`swarm/teammateInit.ts`)

Called early in session startup for teammates. Does two things:

1. **Applies team-wide permissions**: Reads `teamAllowedPaths` from the
   team file and calls `applyPermissionUpdate()` for each path.

2. **Registers Stop hook**: When the teammate's session stops, the hook:
   - Sets `isActive = false` in the team config.
   - Sends an `idle_notification` to the leader's mailbox.
   - Includes the last peer DM summary for context.

---

## 14. Team Helpers (`swarm/teamHelpers.ts`)

CRUD operations for team files plus lifecycle management:

### File operations
- `readTeamFile()` / `readTeamFileAsync()` -- parse config.json
- `writeTeamFile()` / `writeTeamFileAsync()` -- serialize and write
- `removeTeammateFromTeamFile()` -- remove by agentId or name
- `removeMemberFromTeam()` -- remove by paneId
- `removeMemberByAgentId()` -- remove by agentId (for in-process)

### Lifecycle management
- `setMemberActive()` -- toggle isActive flag
- `setMemberMode()` / `setMultipleMemberModes()` -- update permission mode
- `syncTeammateMode()` -- teammate syncs its own mode to config
- `addHiddenPaneId()` / `removeHiddenPaneId()` -- pane visibility

### Cleanup
- `registerTeamForSessionCleanup()` -- track teams created this session
- `cleanupSessionTeams()` -- on leader exit, kill orphaned panes + remove dirs
- `cleanupTeamDirectories()` -- remove team dir, tasks dir, git worktrees
- `destroyWorktree()` -- `git worktree remove --force` with rm fallback

---

## 15. Spawn Flow (In-Process) (`swarm/spawnInProcess.ts`)

`spawnInProcessTeammate()`:

1. Generates deterministic `agentId = name@team`.
2. Creates independent AbortController (not linked to leader's query).
3. Creates `TeammateContext` for AsyncLocalStorage isolation.
4. Registers in Perfetto tracing.
5. Builds `InProcessTeammateTaskState` with initial status `running`.
6. Registers cleanup handler (aborts on leader shutdown).
7. Calls `registerTask()` to add to AppState.

`killInProcessTeammate()`:

1. Aborts the AbortController.
2. Updates task status to `killed`.
3. Removes from `teamContext.teammates` in AppState.
4. Removes from team file via `removeMemberByAgentId()`.
5. Emits SDK task-terminated event.
6. Schedules task eviction after display timeout.

---

## Summary: Message Flow

```
Leader                          Mailbox                     Teammate
  |                               |                            |
  |-- TeamCreateTool ------------>| (writes config.json)       |
  |                               |                            |
  |-- spawn() ------------------>| (writes initial prompt)    |
  |                               |-------- prompt ----------->|
  |                               |                            |
  |                               |<--- idle_notification -----|
  |<-- inbox poll ----------------|                            |
  |                               |                            |
  |-- SendMessage(to: "X") ----->|                            |
  |                               |-------- message ---------->|
  |                               |                            |
  |-- shutdown_request --------->|                            |
  |                               |-------- shutdown_req ----->|
  |                               |                            |
  |                               |<--- shutdown_approved -----|
  |<-- inbox poll ----------------|                            |
  |                               |                            |
  |-- kill() ------------------->| (abort controller)         X
```
