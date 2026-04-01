# Session 13 -- Teams and Swarms

## Focus

How Claude Code orchestrates multiple cooperating agent instances (a
"swarm"), including the backend-agnostic pane system, the in-process
execution path, coordinator mode, and inter-agent messaging via
file-based mailboxes.

## Key Concepts

| Concept | Purpose |
|---------|---------|
| **PaneBackend** | Abstract interface for terminal-pane management (tmux / iTerm2) |
| **TeammateExecutor** | Unified lifecycle interface (spawn, sendMessage, terminate, kill) across all backends |
| **InProcessBackend** | Runs teammates in the same Node.js process with AsyncLocalStorage isolation |
| **PaneBackendExecutor** | Adapts a PaneBackend into a TeammateExecutor for pane-based teammates |
| **Backend registry** | Auto-detects the best backend (tmux > iTerm2 > in-process) and caches the result |
| **Coordinator mode** | Strips the leader down to Agent/SendMessage/TaskStop tools; injects a system prompt focused on orchestration |
| **TeamCreateTool** | Creates a team (writes config.json, sets up task list, updates AppState) |
| **SendMessageTool** | Routes messages -- plain text via mailbox, structured shutdown/plan/permission protocols, broadcast to all |
| **File-based mailbox** | JSON files under `~/.claude/teams/{team}/inboxes/{agent}.json` with file locking |
| **Leader permission bridge** | Lets in-process teammates show the leader's native permission dialog rather than polling via mailbox |
| **Teammate init hooks** | Registers a Stop hook so teammates send idle notifications when they finish work |

## Source Directories

```
src/utils/swarm/
  backends/          -- TmuxBackend, ITermBackend, InProcessBackend,
                        PaneBackendExecutor, registry, detection, types
  constants.ts       -- TEAM_LEAD_NAME, socket names, env vars
  inProcessRunner.ts -- runInProcessTeammate loop (prompt -> run -> idle -> wait)
  leaderPermissionBridge.ts
  permissionSync.ts
  spawnInProcess.ts  -- spawnInProcessTeammate + killInProcessTeammate
  teammateInit.ts    -- idle-notification hooks
  teamHelpers.ts     -- TeamFile CRUD, cleanup, worktree teardown
  teammatePromptAddendum.ts

src/tools/
  TeamCreateTool/    -- creates team, writes config, registers in AppState
  SendMessageTool/   -- routes plain / structured messages, broadcast

src/coordinator/
  coordinatorMode.ts -- isCoordinatorMode(), system prompt, worker tool list

src/utils/
  teammateMailbox.ts -- readMailbox, writeToMailbox, structured message types
```

## Session Files

| File | Description |
|------|-------------|
| `SOURCE_ANALYSIS.md` | Annotated walkthrough of every subsystem |
| `reimplementation.py` | Runnable Python demo (~250 LOC): in-process backend, mailbox, coordinator delegation |
