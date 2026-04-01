# Source Map: Real Source to Session Coverage

This table maps every major file in the Claude Code source to the session
that covers it. Line counts are approximate and reflect the source at the
time of analysis.

---

## Entrypoints and Boot

| Source File | Lines | Session | Description |
|-------------|-------|---------|-------------|
| `src/entrypoints/cli.tsx` | 302 | s01 | Bootstrap entrypoint, fast paths (--version, daemon, bridge) |
| `src/entrypoints/init.ts` | ~200 | s01 | One-time init: enableConfigs, sinks, preflight checks |
| `src/entrypoints/mcp.ts` | ~100 | s11 | MCP server entrypoint |
| `src/entrypoints/sdk/` | ~300 | s01 | Agent SDK type definitions and entrypoints |
| `src/main.tsx` | 4683 | s01 | Full CLI entry: Commander parsing, option handling, launch |
| `src/setup.ts` | ~100 | s01 | Setup wizard for first run |

## Agent Loop

| Source File | Lines | Session | Description |
|-------------|-------|---------|-------------|
| `src/QueryEngine.ts` | 1295 | s01 | SDK/headless conversation engine, turn loop |
| `src/query.ts` | 1729 | s01 | Single-turn API call, tool dispatch, compact triggers |
| `src/context.ts` | 189 | s04 | User context (CLAUDE.md) and system context (git status) |

## Tool Interface and Registration

| Source File | Lines | Session | Description |
|-------------|-------|---------|-------------|
| `src/Tool.ts` | 792 | s02 | Tool interface, ToolUseContext, buildTool(), ToolPermissionContext |
| `src/tools.ts` | 389 | s02 | Tool registry: getAllBaseTools, getTools, assembleToolPool |
| `src/commands.ts` | 754 | s02 | Slash command registry and loading |

## Built-in Tools

| Source File | Lines | Session | Description |
|-------------|-------|---------|-------------|
| `src/tools/BashTool/` | ~800 | s03 | Shell command execution |
| `src/tools/FileReadTool/` | ~400 | s03 | File reading with line ranges |
| `src/tools/FileEditTool/` | ~600 | s03 | Diff-based file editing |
| `src/tools/FileWriteTool/` | ~300 | s03 | Full file writing |
| `src/tools/GlobTool/` | ~250 | s03 | File pattern matching (glob) |
| `src/tools/GrepTool/` | ~350 | s03 | Content search via ripgrep |
| `src/tools/AgentTool/` | ~1500 | s08 | Subagent spawning and management |
| `src/tools/SkillTool/` | ~300 | s07 | Skill invocation tool |
| `src/tools/MCPTool/` | ~400 | s11 | MCP tool wrapper |
| `src/tools/WebFetchTool/` | ~300 | s03 | URL content fetching |
| `src/tools/WebSearchTool/` | ~300 | s03 | Web search integration |
| `src/tools/NotebookEditTool/` | ~400 | s03 | Jupyter notebook editing |
| `src/tools/TodoWriteTool/` | ~300 | s03 | Todo list management |
| `src/tools/TaskCreateTool/` | ~200 | s09 | Background task creation |
| `src/tools/TaskGetTool/` | ~150 | s09 | Task output reading |
| `src/tools/TaskUpdateTool/` | ~150 | s09 | Send message to running task |
| `src/tools/TaskListTool/` | ~150 | s09 | List all tasks |
| `src/tools/TaskStopTool/` | ~150 | s09 | Kill running task |
| `src/tools/TaskOutputTool/` | ~200 | s09 | Read task disk output |
| `src/tools/SendMessageTool/` | ~200 | s13 | Inter-agent messaging |
| `src/tools/EnterWorktreeTool/` | ~200 | s14 | Create git worktree |
| `src/tools/ExitWorktreeTool/` | ~200 | s14 | Destroy git worktree |
| `src/tools/EnterPlanModeTool/` | ~150 | s05 | Enter plan (read-only) mode |
| `src/tools/ExitPlanModeTool/` | ~200 | s05 | Exit plan mode |
| `src/tools/AskUserQuestionTool/` | ~150 | s03 | Ask user a question |
| `src/tools/ToolSearchTool/` | ~200 | s03 | Search for deferred tools |
| `src/tools/ListMcpResourcesTool/` | ~150 | s11 | List MCP resources |
| `src/tools/ReadMcpResourceTool/` | ~150 | s11 | Read MCP resource |
| `src/tools/SyntheticOutputTool/` | ~150 | s03 | Structured output for SDK |
| `src/tools/TeamCreateTool/` | ~200 | s13 | Create agent team |
| `src/tools/TeamDeleteTool/` | ~150 | s13 | Delete agent team |
| `src/tools/BriefTool/` | ~200 | s03 | Brief mode output |
| `src/tools/ConfigTool/` | ~150 | s03 | Runtime config inspection |
| `src/tools/LSPTool/` | ~200 | s03 | Language Server Protocol integration |
| `src/tools/shared/` | ~200 | s03 | Shared tool utilities |
| `src/tools/utils.ts` | ~100 | s03 | Tool helper functions |

## System Prompt

| Source File | Lines | Session | Description |
|-------------|-------|---------|-------------|
| `src/constants/prompts.ts` | 914 | s04 | System prompt builder: getSystemPrompt() |
| `src/constants/systemPromptSections.ts` | 68 | s04 | Section caching framework |
| `src/constants/system.ts` | 95 | s04 | CLI prefix, attribution header |
| `src/constants/outputStyles.ts` | ~100 | s04 | Output style instructions |
| `src/constants/cyberRiskInstruction.ts` | ~50 | s04 | Security instructions |

## Permission System

| Source File | Lines | Session | Description |
|-------------|-------|---------|-------------|
| `src/utils/permissions/permissions.ts` | ~500 | s05 | Main permission evaluation logic |
| `src/utils/permissions/PermissionRule.ts` | ~200 | s05 | Rule type definitions |
| `src/utils/permissions/PermissionResult.ts` | ~80 | s05 | Result type definitions |
| `src/utils/permissions/PermissionMode.ts` | ~50 | s05 | Mode enum (default, plan, auto) |
| `src/utils/permissions/yoloClassifier.ts` | ~300 | s05 | Auto-mode safety classifier |
| `src/utils/permissions/bashClassifier.ts` | ~200 | s05 | Shell command classifier |
| `src/utils/permissions/permissionsLoader.ts` | ~200 | s05 | Load rules from settings |
| `src/utils/permissions/permissionSetup.ts` | ~200 | s05 | Initial permission setup |
| `src/utils/permissions/filesystem.ts` | ~300 | s05 | Path validation and sandboxing |
| `src/utils/permissions/dangerousPatterns.ts` | ~100 | s05 | Dangerous command patterns |
| `src/utils/permissions/shellRuleMatching.ts` | ~150 | s05 | Shell glob rule matching |
| `src/utils/permissions/denialTracking.ts` | ~100 | s05 | Track repeated denials |
| `src/hooks/useCanUseTool.tsx` | ~300 | s05 | React permission check hook |

## Context Compaction

| Source File | Lines | Session | Description |
|-------------|-------|---------|-------------|
| `src/services/compact/autoCompact.ts` | ~200 | s06 | Auto-compact trigger logic |
| `src/services/compact/compact.ts` | ~300 | s06 | Full compaction implementation |
| `src/services/compact/microCompact.ts` | ~150 | s06 | Micro-compact for large single messages |
| `src/services/compact/apiMicrocompact.ts` | ~100 | s06 | API-level micro-compact |
| `src/services/compact/sessionMemoryCompact.ts` | ~200 | s06 | Memory extraction during compact |
| `src/services/compact/prompt.ts` | ~100 | s06 | Compact summary prompt |
| `src/services/compact/grouping.ts` | ~100 | s06 | Message grouping for compact |
| `src/services/compact/postCompactCleanup.ts` | ~80 | s06 | Post-compact state cleanup |

## Skills

| Source File | Lines | Session | Description |
|-------------|-------|---------|-------------|
| `src/skills/loadSkillsDir.ts` | 1087 | s07 | Disk-based skill loading, dynamic discovery |
| `src/skills/bundledSkills.ts` | 221 | s07 | Bundled skill registry and extraction |
| `src/skills/bundled/` | ~2000 | s07 | Individual bundled skill implementations |
| `src/skills/mcpSkillBuilders.ts` | ~50 | s07 | MCP-to-skill bridge |

## Subagents

| Source File | Lines | Session | Description |
|-------------|-------|---------|-------------|
| `src/tools/AgentTool/AgentTool.ts` | ~500 | s08 | Agent tool implementation |
| `src/tools/AgentTool/loadAgentsDir.ts` | ~300 | s08 | Agent definition loading |
| `src/tools/AgentTool/constants.ts` | ~50 | s08 | Agent tool constants |
| `src/tools/AgentTool/forkSubagent.ts` | ~200 | s08 | Fork-based subagent strategy |
| `src/tools/AgentTool/builtInAgents.ts` | ~100 | s08 | Built-in agent registry |
| `src/utils/forkedAgent.ts` | ~300 | s08 | Forked agent query loop helper |
| `src/utils/sideQuery.ts` | ~200 | s08 | Side query for parallel work |

## Task System

| Source File | Lines | Session | Description |
|-------------|-------|---------|-------------|
| `src/Task.ts` | 125 | s09 | Task types, status, lifecycle |
| `src/tasks.ts` | 39 | s09 | Task type registry |
| `src/tasks/types.ts` | ~100 | s09 | Extended task state types |
| `src/tasks/LocalShellTask/` | ~300 | s09 | Background shell task |
| `src/tasks/LocalAgentTask/` | ~300 | s09 | Background agent task |
| `src/tasks/RemoteAgentTask/` | ~200 | s09 | Remote agent task |
| `src/tasks/InProcessTeammateTask/` | ~300 | s09 | In-process teammate task |
| `src/tasks/DreamTask/` | ~200 | s09 | Background auto-dream task |
| `src/tasks/stopTask.ts` | ~50 | s09 | Generic task stop handler |
| `src/utils/task/` | ~200 | s09 | Task disk output utilities |

## Hooks

| Source File | Lines | Session | Description |
|-------------|-------|---------|-------------|
| `src/utils/hooks.ts` | ~800 | s10 | Shell hook execution engine |
| `src/types/hooks.ts` | ~200 | s10 | Hook type definitions |
| `src/hooks/useCanUseTool.tsx` | ~300 | s10 | Hook integration with permissions |
| `src/hooks/fileSuggestions.ts` | ~100 | s10 | File suggestion hooks |
| `src/hooks/toolPermission/` | ~300 | s10 | Tool permission hook helpers |
| `src/utils/hooks/` | ~300 | s10 | Hook helper utilities |

## MCP Integration

| Source File | Lines | Session | Description |
|-------------|-------|---------|-------------|
| `src/services/mcp/client.ts` | ~500 | s11 | MCP client: tool/resource fetching |
| `src/services/mcp/types.ts` | ~200 | s11 | MCP types: server, connection, config |
| `src/services/mcp/config.ts` | ~200 | s11 | MCP server configuration loading |
| `src/services/mcp/MCPConnectionManager.tsx` | ~400 | s11 | Connection lifecycle management |
| `src/services/mcp/InProcessTransport.ts` | ~150 | s11 | In-process MCP transport |
| `src/services/mcp/normalization.ts` | ~100 | s11 | Tool name normalization |
| `src/services/mcp/auth.ts` | ~200 | s11 | MCP OAuth authentication |
| `src/services/mcp/channelPermissions.ts` | ~150 | s11 | Channel-level permissions |
| `src/services/mcp/utils.ts` | ~100 | s11 | MCP utility functions |
| `src/services/mcp/officialRegistry.ts` | ~100 | s11 | Official MCP server registry |

## State Management

| Source File | Lines | Session | Description |
|-------------|-------|---------|-------------|
| `src/state/store.ts` | 35 | s12 | Zustand-like store: getState, setState, subscribe |
| `src/state/AppState.tsx` | ~200 | s12 | React context provider, settings change bridge |
| `src/state/AppStateStore.ts` | ~300 | s12 | AppState type definition, defaults |
| `src/state/selectors.ts` | ~100 | s12 | Derived state selectors |
| `src/state/onChangeAppState.ts` | ~100 | s12 | AppState change handlers |

## Teams and Swarms

| Source File | Lines | Session | Description |
|-------------|-------|---------|-------------|
| `src/utils/swarm/inProcessRunner.ts` | ~300 | s13 | In-process teammate runner |
| `src/utils/swarm/spawnInProcess.ts` | ~200 | s13 | Spawn in-process teammate |
| `src/utils/swarm/teammateInit.ts` | ~200 | s13 | Teammate initialization |
| `src/utils/swarm/teammatePromptAddendum.ts` | ~150 | s13 | Teammate-specific prompt additions |
| `src/utils/swarm/permissionSync.ts` | ~150 | s13 | Cross-teammate permission sync |
| `src/utils/swarm/reconnection.ts` | ~150 | s13 | Team reconnection logic |
| `src/utils/swarm/teamHelpers.ts` | ~150 | s13 | Team utility functions |
| `src/utils/swarm/constants.ts` | ~50 | s13 | Swarm constants |
| `src/utils/swarm/backends/TmuxBackend.ts` | ~300 | s13 | Tmux pane backend |
| `src/utils/swarm/backends/ITermBackend.ts` | ~300 | s13 | iTerm2 pane backend |
| `src/utils/swarm/backends/InProcessBackend.ts` | ~200 | s13 | In-process backend |
| `src/utils/swarm/backends/registry.ts` | ~100 | s13 | Backend selection registry |
| `src/utils/swarm/backends/types.ts` | ~80 | s13 | Backend type definitions |
| `src/coordinator/coordinatorMode.ts` | ~200 | s13 | Coordinator orchestration mode |
| `src/utils/teammate.ts` | ~200 | s13 | Teammate utility functions |

## Worktree Isolation

| Source File | Lines | Session | Description |
|-------------|-------|---------|-------------|
| `src/utils/worktree.ts` | ~600 | s14 | Git worktree create/remove/validate |
| `src/utils/worktreeModeEnabled.ts` | ~30 | s14 | Feature flag check |
| `src/utils/getWorktreePaths.ts` | ~100 | s14 | Worktree path resolution |
| `src/tools/EnterWorktreeTool/` | ~200 | s14 | Enter worktree tool |
| `src/tools/ExitWorktreeTool/` | ~200 | s14 | Exit worktree tool |

## Constants

| Source File | Lines | Session | Description |
|-------------|-------|---------|-------------|
| `src/constants/common.ts` | 33 | s01 | Shared constants (date formatting) |
| `src/constants/apiLimits.ts` | 94 | s04 | API size limits (images, PDFs, media) |
| `src/constants/toolLimits.ts` | 56 | s03 | Tool result size limits |
| `src/constants/tools.ts` | 112 | s02 | Tool allow/deny sets for agents |
| `src/constants/keys.ts` | ~50 | s01 | API key constants |
| `src/constants/figures.ts` | ~30 | s12 | Unicode figures for UI |
| `src/constants/xml.ts` | ~30 | s01 | XML tag constants |
| `src/constants/errorIds.ts` | ~30 | s01 | Error ID constants |
| `src/constants/betas.ts` | ~50 | s01 | Beta feature flags |

## Key Utilities

| Source File | Lines | Session | Description |
|-------------|-------|---------|-------------|
| `src/utils/messages.ts` | ~500 | s01 | Message creation, normalization, helpers |
| `src/utils/systemPrompt.ts` | ~200 | s04 | System prompt construction helpers |
| `src/utils/config.ts` | ~300 | s01 | Global config (trust dialog, auto-updater) |
| `src/utils/claudemd.ts` | ~300 | s04 | CLAUDE.md file loading and parsing |
| `src/utils/settings/` | ~1000 | s12 | Settings loading from all sources |
| `src/utils/model/` | ~300 | s01 | Model selection and configuration |
| `src/utils/auth.ts` | ~400 | s01 | Authentication (OAuth, API keys) |
| `src/utils/sessionStorage.ts` | ~300 | s01 | Session persistence to disk |
| `src/utils/toolResultStorage.ts` | ~300 | s06 | Large result disk persistence |
| `src/utils/attachments.ts` | ~300 | s04 | File/URL attachment handling |
| `src/utils/thinking.ts` | ~100 | s01 | Extended thinking configuration |

## React Components (Selected)

| Source File | Lines | Session | Description |
|-------------|-------|---------|-------------|
| `src/components/App.tsx` | ~500 | s12 | Root application component |
| `src/components/Messages.tsx` | ~400 | s12 | Message list renderer |
| `src/components/PromptInput/` | ~800 | s12 | User input component |
| `src/components/StatusLine.tsx` | ~200 | s12 | Status bar component |
| `src/components/permissions/` | ~400 | s05 | Permission dialog components |
| `src/components/diff/` | ~300 | s03 | Diff visualization |
| `src/components/mcp/` | ~300 | s11 | MCP server UI components |
| `src/components/skills/` | ~200 | s07 | Skill UI components |
| `src/components/tasks/` | ~300 | s09 | Task management UI |
| `src/components/teams/` | ~200 | s13 | Team/swarm UI components |
