# Claude Code Architecture

A comprehensive map of how Claude Code works, derived from the real source.

---

## 1. System Overview

Claude Code is an **Ink-based React terminal application** that connects an
interactive REPL (or headless SDK) to the Anthropic Messages API. It gives
Claude a set of tools (file I/O, shell, search, web, MCP, etc.) and an
agentic loop that calls those tools until the task is done.

```
+------------------------------------------------------+
|  Terminal / IDE / SDK                                 |
|  +-----------+  +-------------+  +-----------------+ |
|  | Ink React |  | Commander   |  | Agent SDK       | |
|  | TUI       |  | CLI parser  |  | (headless JSON) | |
|  +-----------+  +-------------+  +-----------------+ |
|        |              |                  |            |
|        +------+-------+------ -----------+            |
|               v                                       |
|         [ main.tsx ]  -- boot, parse args, render     |
|               |                                       |
|         [ QueryEngine / query.ts ]                    |
|               |                                       |
|    +----------+-----------+                           |
|    | Anthropic Messages   |                           |
|    | API (streaming)      |                           |
|    +----------+-----------+                           |
|               |                                       |
|    +----------v-----------+                           |
|    | Tool Dispatch Loop   |                           |
|    | (BashTool, FileEdit, |                           |
|    |  Grep, Glob, MCP...) |                           |
|    +----------------------+                           |
+------------------------------------------------------+
```

Key technologies:
- **Runtime**: Bun (bundled binary with embedded tools)
- **UI framework**: React via Ink (terminal renderer)
- **CLI parsing**: Commander.js
- **State**: Custom Zustand-like store (`createStore`)
- **API**: Anthropic SDK (`@anthropic-ai/sdk`)
- **Build**: Bun bundler with `feature()` flags for dead-code elimination

---

## 2. Boot Sequence

```
cli.tsx (entrypoint)
  |
  +-- Fast paths: --version, --dump-system-prompt, daemon, bridge, bg
  |
  +-- Normal path:
        |
        import main.tsx
              |
              +-- profileCheckpoint('main_tsx_entry')
              +-- startMdmRawRead()          // MDM settings prefetch
              +-- startKeychainPrefetch()     // OAuth token prefetch
              +-- Commander option parsing    // --model, --print, --resume, etc.
              +-- init()                      // enableConfigs, sinks, preflights
              +-- initBundledSkills()
              +-- loadPolicyLimits()
              +-- getCommands() + getTools()  // assemble tool pool
              +-- getMcpToolsCommandsAndResources()
              |
              +-- Branch:
                   |
                   +-- [--print / SDK] --> QueryEngine.submitMessage()
                   |                       (headless, JSON streaming)
                   |
                   +-- [interactive]  --> launchRepl()
                                          Ink render(<App />) with REPL loop
```

### cli.tsx (302 lines)
The outermost entrypoint. Contains fast paths that avoid importing the full
CLI module tree (e.g. `--version` has zero module loading). Falls through to
`main.tsx` for normal operation.

### main.tsx (4683 lines)
The full CLI entry. Parses Commander options, initializes subsystems, then
either enters the REPL (interactive) or runs QueryEngine (headless).

### entrypoints/init.ts
One-time initialization: `enableConfigs()`, analytics sinks, preflight
checks, cleanup registration.

---

## 3. Core Subsystems

### 3.1 Agent Loop

```
QueryEngine.submitMessage(userText)
  |
  +-- processUserInput()           // slash commands, attachments
  +-- fetchSystemPromptParts()     // build system prompt sections
  +-- loop:
  |     |
  |     +-- query()                // call Anthropic API (streaming)
  |     |     |
  |     |     +-- normalizeMessagesForAPI()
  |     |     +-- prependUserContext() / appendSystemContext()
  |     |     +-- claude.sendMessage() --> stream response
  |     |     +-- for each tool_use block:
  |     |           +-- findToolByName()
  |     |           +-- canUseTool()  (permission check)
  |     |           +-- tool.call()   (execute)
  |     |           +-- append tool_result
  |     |
  |     +-- check stop conditions:
  |           - end_turn / max_tokens
  |           - auto-compact threshold
  |           - max turns / budget
  |
  +-- recordTranscript() / flushSessionStorage()
```

**QueryEngine** (`QueryEngine.ts`, 1295 lines) owns the conversation
lifecycle for SDK/headless mode. It wraps `query()` in a turn loop with
usage tracking, permission replays, and session persistence.

**query()** (`query.ts`, 1729 lines) is the single-turn API call that
streams a response and dispatches tool calls. It handles:
- Message normalization for the API
- Tool dispatch (parallel when `isConcurrencySafe`)
- Auto-compact and micro-compact triggers
- Interruption handling (user Ctrl+C)
- Error retry with fallback model

### 3.2 Tool System

```
Tool interface (Tool.ts, 792 lines)
  |
  +-- name, inputSchema (Zod), description, prompt
  +-- call(args, context, canUseTool, parentMsg, onProgress)
  +-- checkPermissions(input, context) -> PermissionResult
  +-- isReadOnly / isDestructive / isConcurrencySafe
  +-- renderToolUseMessage / renderToolResultMessage (React)
  +-- maxResultSizeChars, validateInput, preparePermissionMatcher
  |
  +-- buildTool(def) -> fills defaults (Tool.ts line 757)

Tool Registry (tools.ts, 389 lines)
  |
  +-- getAllBaseTools() -> canonical list of ~30+ built-in tools
  +-- getTools(permCtx) -> filter by deny rules, --bare mode
  +-- assembleToolPool(permCtx, mcpTools) -> built-in + MCP, deduped
  +-- filterToolsByDenyRules() -> strip blanket-denied tools

Built-in tools (src/tools/*):
  BashTool, FileReadTool, FileEditTool, FileWriteTool,
  GlobTool, GrepTool, WebFetchTool, WebSearchTool,
  NotebookEditTool, AgentTool, SkillTool, TodoWriteTool,
  TaskCreateTool, TaskGetTool, TaskUpdateTool, TaskListTool,
  TaskOutputTool, TaskStopTool, SendMessageTool,
  EnterWorktreeTool, ExitWorktreeTool, EnterPlanModeTool,
  ExitPlanModeTool, AskUserQuestionTool, ToolSearchTool,
  ListMcpResourcesTool, ReadMcpResourceTool, ConfigTool,
  BriefTool, SyntheticOutputTool, LSPTool, ...
```

Every tool follows the `Tool` interface. `buildTool()` provides safe
defaults (fail-closed: `isConcurrencySafe=false`, `isReadOnly=false`).
Tools self-describe with Zod schemas and render their own Ink UI.

### 3.3 System Prompt

```
getSystemPrompt(tools, model) --> string[]
  |
  +-- resolveSystemPromptSections(sections)
  |     |
  |     +-- systemPromptSection(name, compute)
  |     |     Memoized: computed once, cached until /clear or /compact
  |     |
  |     +-- DANGEROUS_uncachedSystemPromptSection(name, compute, reason)
  |           Recomputed every turn -- breaks prompt cache
  |
  +-- Sections include:
        - CLI prefix ("You are Claude Code...")
        - Tool prompts (tool.prompt())
        - CLAUDE.md content (getUserContext)
        - Git status snapshot (getSystemContext)
        - Output style instructions
        - Date, environment, OS info
        - Skill catalog (when_to_use listings)
        - Memory instructions

CLAUDE.md loading (context.ts / claudemd.ts):
  ~/.claude/CLAUDE.md       (user-level)
  .claude/CLAUDE.md         (project-level, walked up to home)
  --add-dir paths           (explicit additional directories)
  Conditional rules         (path-scoped, activated on file touch)
```

The prompt is split into **sections** for cache efficiency. Cached sections
keep the same bytes across turns, maximizing prompt-cache hits. Only
`DANGEROUS_uncachedSystemPromptSection` sections break the cache.

### 3.4 Permission System

```
Permission evaluation flow:
  |
  canUseTool(toolName, input)
    |
    +-- Step 1: Check permission rules (3 sources)
    |     |
    |     +-- alwaysAllowRules  --> allow immediately
    |     +-- alwaysDenyRules   --> deny immediately
    |     +-- alwaysAskRules    --> prompt user
    |
    +-- Step 2: Tool-specific checkPermissions()
    |     (e.g., BashTool checks dangerous patterns)
    |
    +-- Step 3: Classifier (auto-mode)
    |     yoloClassifier / bashClassifier
    |     Analyzes command safety for auto-approve
    |
    +-- Step 4: User prompt (if needed)
          "Allow / Deny / Always Allow"

Permission modes:
  - default    : ask for write operations
  - plan       : read-only tools auto-allowed
  - auto (YOLO): classifier decides, user fallback
  - bypass     : allow everything (requires trust dialog)

Rule sources (PermissionRule.ts):
  - settings.json (user/project/managed)
  - CLAUDE.md allowed_tools
  - CLI --allowedTools
  - Session-scoped (user grants during conversation)

Key files:
  utils/permissions/permissions.ts     (main logic)
  utils/permissions/PermissionRule.ts  (rule types)
  utils/permissions/yoloClassifier.ts  (auto-mode classifier)
  utils/permissions/bashClassifier.ts  (shell command classifier)
  utils/permissions/filesystem.ts      (path validation)
  hooks/useCanUseTool.tsx              (React hook)
```

### 3.5 Context Management

```
Context compaction strategies:
  |
  +-- Auto-compact (services/compact/autoCompact.ts)
  |     Triggers when token usage exceeds threshold
  |     Summarizes older messages, preserves recent ones
  |     Boundary message marks the compact point
  |
  +-- Micro-compact (services/compact/microCompact.ts)
  |     API-level compaction for single large messages
  |     Lighter weight than full compact
  |
  +-- Session compact (services/compact/sessionMemoryCompact.ts)
  |     Memory extraction during /compact command
  |     Extracts key learnings into session memory
  |
  +-- Reactive compact (services/compact/reactiveCompact.ts)
  |     Feature-gated: responds to token pressure dynamically
  |
  +-- Tool result storage (utils/toolResultStorage.ts)
        Large results persisted to disk
        Preview + file path sent to model instead
        Per-tool max: DEFAULT_MAX_RESULT_SIZE_CHARS = 50,000
        Per-message budget: MAX_TOOL_RESULTS_PER_MESSAGE_CHARS = 200,000
```

### 3.6 Skills System

```
Two-layer loading:

Layer 1: Disk-based skills (loadSkillsDir.ts)
  |
  +-- ~/.claude/skills/           (user-level)
  +-- .claude/skills/             (project-level)
  +-- managed policy skills       (enterprise)
  +-- legacy /commands/ dirs      (backward compat)
  |
  +-- Each skill: skill-name/SKILL.md
  +-- Frontmatter: description, when_to_use, allowed-tools, model, hooks
  +-- Dynamic discovery: skills found by file path proximity
  +-- Conditional skills: activated when matching file paths touched

Layer 2: Bundled skills (bundledSkills.ts)
  |
  +-- registerBundledSkill(definition)
  +-- Compiled into CLI binary
  +-- initBundledSkills() called at startup
  +-- Reference files extracted to disk on first invocation

Invocation path:
  /skill-name args --> SkillTool.call()
    |
    +-- Find matching Command
    +-- command.getPromptForCommand(args, context)
    +-- Inject prompt as user message
    +-- Optional: fork context, override model, allowed-tools
```

### 3.7 Subagents

```
AgentTool.call(input, context)
  |
  +-- loadAgentsDir() -> AgentDefinition[]
  |     Built-in agents (explore, verify)
  |     Custom agents (.claude/agents/*.md)
  |
  +-- createSubagentContext(parentContext)
  |     Clone fileStateCache
  |     Create child AbortController
  |     Isolate mutable state
  |     Share parent's prompt cache (CacheSafeParams)
  |
  +-- Fork strategies:
  |     |
  |     +-- Standard: forkedAgent.ts
  |     |     Runs query() loop in same process
  |     |     Tracks usage via tengu_fork_agent_query event
  |     |
  |     +-- Fork subagent: forkSubagent.ts
  |           Feature-gated
  |           Shares parent's rendered system prompt
  |
  +-- Tool filtering:
  |     ALL_AGENT_DISALLOWED_TOOLS blocks:
  |       TaskOutput, ExitPlanMode, EnterPlanMode, AskUserQuestion
  |     ASYNC_AGENT_ALLOWED_TOOLS for background agents
  |
  +-- Report: summarized result returned to parent
```

### 3.8 Task System

```
Task lifecycle (Task.ts):
  |
  +-- TaskType: local_bash | local_agent | remote_agent
  |             | in_process_teammate | dream | local_workflow
  |
  +-- TaskStatus: pending -> running -> completed | failed | killed
  |
  +-- TaskStateBase:
  |     id, type, status, description, startTime, endTime
  |     outputFile (disk-persisted output), outputOffset
  |
  +-- generateTaskId(type) -> prefixed ID (b=bash, a=agent, t=teammate...)

Task tools:
  TaskCreateTool  -> spawn new background task
  TaskGetTool     -> read task output
  TaskUpdateTool  -> send message to running task
  TaskListTool    -> list all tasks
  TaskStopTool    -> kill running task
  TaskOutputTool  -> read task disk output

Task implementations (src/tasks/):
  LocalShellTask/         -> bash command in background
  LocalAgentTask/         -> agent subquery in background
  RemoteAgentTask/        -> remote agent
  InProcessTeammateTask/  -> in-process teammate
  DreamTask/              -> background auto-dream
```

### 3.9 Hooks

```
Hook lifecycle (utils/hooks.ts):
  |
  +-- Hook events:
  |     PreToolUse     -> before tool execution
  |     PostToolUse    -> after tool execution
  |     Notification   -> on notifications
  |     Stop           -> on session stop
  |     SessionStart   -> on session start
  |     PreCompact     -> before compaction
  |     PostCompact    -> after compaction
  |
  +-- Hook configuration (settings.json):
  |     hooks:
  |       PreToolUse:
  |         - matcher: { tool_name: "Bash", input_contains: "rm" }
  |           hook: "echo 'blocked' | jq ..."
  |
  +-- Matchers:
  |     tool_name      -> exact or glob match
  |     input_contains -> substring in serialized input
  |
  +-- Execution:
  |     Shell spawn (bash/zsh/powershell)
  |     Receives JSON via stdin (hook input)
  |     Returns JSON via stdout (hook output)
  |     Can: approve, deny, modify input, add messages
  |
  +-- Sources:
        settings.json (user/project/managed)
        Skill-level hooks (per-skill frontmatter)
        Plugin hooks
```

### 3.10 MCP (Model Context Protocol)

```
MCP subsystem (services/mcp/):
  |
  +-- Transports:
  |     stdio      -> child process (command + args)
  |     sse        -> server-sent events (url)
  |     websocket  -> WebSocket transport
  |     in-process -> InProcessTransport (internal)
  |
  +-- MCPServerConnection (types.ts):
  |     name, transport, tools[], resources[], prompts[]
  |     status: connecting | connected | error
  |
  +-- MCPConnectionManager (MCPConnectionManager.tsx):
  |     Manages lifecycle of all MCP server connections
  |     Auto-reconnect on failure
  |     Channel permissions and approval
  |
  +-- Tool integration:
  |     MCP tools -> normalized to Tool interface (MCPTool/)
  |     Name prefixed: mcp__servername__toolname
  |     Merged with built-in tools via assembleToolPool()
  |
  +-- Resource integration:
  |     ListMcpResourcesTool -> list available resources
  |     ReadMcpResourceTool  -> read specific resource
  |
  +-- Skill integration:
        MCP servers can provide skills (mcpSkillBuilders.ts)
        Skills loaded from MCP server prompts
```

### 3.11 State Management

```
State architecture (state/):
  |
  +-- createStore(initialState, onChange) -> Store<T>  (store.ts)
  |     Minimal Zustand-like implementation
  |     getState / setState / subscribe
  |     Object.is equality check to skip no-ops
  |
  +-- AppState (AppStateStore.ts):
  |     settings, verbose, mainLoopModel
  |     toolPermissionContext
  |     messages (conversation history)
  |     tasks (background task registry)
  |     mcp (server connections, tools, resources)
  |     plugins, agent definitions
  |     speculation state
  |     attribution state, file history
  |     denial tracking, session hooks
  |
  +-- AppStateProvider (AppState.tsx):
  |     React context provider
  |     Bridges store to React tree
  |     useSettingsChange for live config reload
  |
  +-- Selectors (selectors.ts):
        Derived state computations
        Memoized for render performance
```

### 3.12 Teams and Swarms

```
Team architecture (utils/swarm/):
  |
  +-- Swarm backends (utils/swarm/backends/):
  |     TmuxBackend      -> tmux panes
  |     ITermBackend      -> iTerm2 split panes
  |     InProcessBackend  -> in-process (no terminal split)
  |     PaneBackendExecutor -> abstract pane executor
  |     registry.ts       -> backend selection
  |
  +-- Team tools:
  |     TeamCreateTool -> create a new team of agents
  |     TeamDeleteTool -> delete a team
  |     SendMessageTool -> message between agents
  |
  +-- Coordinator mode (coordinator/coordinatorMode.ts):
  |     Feature-gated orchestrator pattern
  |     Coordinator agent dispatches to worker agents
  |     COORDINATOR_MODE_ALLOWED_TOOLS restricts coordinator
  |
  +-- In-process teammates:
  |     spawnInProcess.ts -> spawn teammate in same process
  |     inProcessRunner.ts -> run teammate agent loop
  |     teammateInit.ts -> initialize teammate context
  |     teammatePromptAddendum.ts -> teammate-specific prompts
  |     permissionSync.ts -> sync permissions across teammates
  |
  +-- Messaging:
        utils/mailbox.ts -> inter-agent message passing
        utils/directMemberMessage.ts -> direct messaging
        context/mailbox.tsx -> React mailbox context
```

### 3.13 Worktrees

```
Worktree system:
  |
  +-- EnterWorktreeTool / ExitWorktreeTool
  |     Create/destroy git worktrees for isolation
  |
  +-- utils/worktree.ts:
  |     createWorktree(slug) -> .claude/worktrees/<slug>
  |     removeWorktree(slug)
  |     execIntoTmuxWorktree(args) -> fast-path for --tmux
  |     Validates slug (no path traversal)
  |     Copies settings to worktree
  |
  +-- utils/worktreeModeEnabled.ts:
  |     isWorktreeModeEnabled() -> checks setting
  |
  +-- Hooks integration:
  |     WorktreeCreate hook -> custom VCS isolation
  |     WorktreeRemove hook -> custom cleanup
  |
  +-- Settings propagation:
        Copies .claude/ settings to worktree
        Session state scoped to worktree
```

---

## 4. Data Flow: User Query to Response

```
User types: "Fix the bug in auth.ts"
         |
         v
[1] PromptInput (React component)
         |
         v
[2] processUserInput(text)
    - Parse slash commands (/commit, /compact, etc.)
    - Extract @ mentions (files, URLs)
    - Create attachment messages
         |
         v
[3] QueryEngine.submitMessage() / REPL ask()
    - Build messages array
    - Fetch system prompt sections
    - Prepend user context (CLAUDE.md, git status)
         |
         v
[4] query(messages, systemPrompt, tools, ...)
    |
    +---> [4a] normalizeMessagesForAPI()
    |           Strip UI-only messages
    |           Apply tool result budgets
    |
    +---> [4b] claude.sendMessage() --> Anthropic API
    |           Streaming response
    |           Extended thinking (if enabled)
    |
    +---> [4c] Process response blocks:
    |     |
    |     +-- text block     --> render to terminal
    |     +-- tool_use block --> dispatch:
    |           |
    |           +-- canUseTool() check
    |           |     Rules -> Classifier -> User prompt
    |           |
    |           +-- tool.call(args, context)
    |           |     e.g., FileReadTool reads auth.ts
    |           |     Returns ToolResult<data>
    |           |
    |           +-- tool_result --> append to messages
    |
    +---> [4d] Check auto-compact threshold
    |           If exceeded: compact older messages
    |
    +---> [4e] Check stop condition
              end_turn? -> done
              tool_use? -> loop back to [4b]
         |
         v
[5] Render final response
    - Ink components update
    - Session persisted to ~/.claude/projects/
    - Attribution state updated (commit info)
```

---

## 5. Key Constants

| Constant | Value | File | Purpose |
|----------|-------|------|---------|
| `DEFAULT_MAX_RESULT_SIZE_CHARS` | 50,000 | constants/toolLimits.ts | Max chars per tool result before disk persist |
| `MAX_TOOL_RESULT_TOKENS` | 100,000 | constants/toolLimits.ts | Max tokens per tool result |
| `MAX_TOOL_RESULTS_PER_MESSAGE_CHARS` | 200,000 | constants/toolLimits.ts | Aggregate budget per user message |
| `BYTES_PER_TOKEN` | 4 | constants/toolLimits.ts | Conservative bytes-per-token estimate |
| `TOOL_SUMMARY_MAX_LENGTH` | 50 | constants/toolLimits.ts | Max chars for tool summary in compact views |
| `API_IMAGE_MAX_BASE64_SIZE` | 5 MB | constants/apiLimits.ts | Max base64 image size (API enforced) |
| `IMAGE_TARGET_RAW_SIZE` | 3.75 MB | constants/apiLimits.ts | Target raw image size (pre-encoding) |
| `IMAGE_MAX_WIDTH` | 2,000 px | constants/apiLimits.ts | Client-side max image width |
| `IMAGE_MAX_HEIGHT` | 2,000 px | constants/apiLimits.ts | Client-side max image height |
| `PDF_TARGET_RAW_SIZE` | 20 MB | constants/apiLimits.ts | Max raw PDF size |
| `API_PDF_MAX_PAGES` | 100 | constants/apiLimits.ts | Max PDF pages (API enforced) |
| `PDF_MAX_PAGES_PER_READ` | 20 | constants/apiLimits.ts | Max pages per Read tool call |
| `PDF_EXTRACT_SIZE_THRESHOLD` | 3 MB | constants/apiLimits.ts | PDF size threshold for page extraction |
| `PDF_AT_MENTION_INLINE_THRESHOLD` | 10 pages | constants/apiLimits.ts | Max pages for inline @ mention |
| `API_MAX_MEDIA_PER_REQUEST` | 100 | constants/apiLimits.ts | Max images + PDFs per API request |
| `MAX_STATUS_CHARS` | 2,000 | context.ts | Git status truncation limit |
| `MAX_WORKTREE_SLUG_LENGTH` | 64 | utils/worktree.ts | Max worktree name length |

---

## 6. Directory Structure Overview

```
src/
  entrypoints/
    cli.tsx              Boot entrypoint, fast paths
    init.ts              One-time initialization
    sdk/                 Agent SDK entrypoints
  main.tsx               Full CLI entry, Commander parsing
  QueryEngine.ts         SDK/headless conversation engine
  query.ts               Single-turn API call + tool dispatch
  Tool.ts                Tool interface + buildTool()
  tools.ts               Tool registry + assembly
  Task.ts                Task types + lifecycle
  tasks.ts               Task registry
  commands.ts            Slash command registry
  context.ts             User/system context (CLAUDE.md, git)
  tools/
    BashTool/            Shell execution
    FileReadTool/        File reading
    FileEditTool/        File editing (diff-based)
    FileWriteTool/       File writing
    GlobTool/            File pattern search
    GrepTool/            Content search (ripgrep)
    AgentTool/           Subagent spawning
    SkillTool/           Skill invocation
    MCPTool/             MCP tool wrapper
    WebFetchTool/        URL fetching
    WebSearchTool/       Web search
    TaskCreateTool/      Background task creation
    SendMessageTool/     Inter-agent messaging
    EnterWorktreeTool/   Worktree creation
    TodoWriteTool/       Todo list management
    NotebookEditTool/    Jupyter notebook editing
    ...
  services/
    api/                 Anthropic API client
    compact/             Context compaction strategies
    mcp/                 MCP server management
    analytics/           Telemetry + GrowthBook
    policyLimits/        Enterprise policy enforcement
    plugins/             Plugin system
    PromptSuggestion/    Prompt autocomplete
    SessionMemory/       Session memory persistence
    tools/               Tool-related services
  constants/
    prompts.ts           System prompt builder (914 lines)
    systemPromptSections.ts  Section caching framework
    apiLimits.ts         API size limits
    toolLimits.ts        Tool result size limits
    tools.ts             Tool allow/deny lists
    system.ts            System prefix + attribution
  state/
    store.ts             Zustand-like store implementation
    AppState.tsx         React context provider
    AppStateStore.ts     AppState type + defaults
    selectors.ts         Derived state
  skills/
    bundledSkills.ts     Built-in skill registry
    loadSkillsDir.ts     Disk-based skill loading
    bundled/             Individual bundled skills
  hooks/
    useCanUseTool.tsx    Permission check hook
    useSettings.ts       Settings change detection
    ...80+ React hooks
  utils/
    permissions/         Permission rules + classifiers
    swarm/               Team/swarm agent backends
    hooks.ts             Shell hook execution
    systemPrompt.ts      System prompt helpers
    worktree.ts          Git worktree management
    forkedAgent.ts       Subagent fork helpers
    messages.ts          Message creation + normalization
    config.ts            Configuration management
    ...500+ utility files
  components/
    App.tsx              Root React component
    Messages.tsx         Message list renderer
    PromptInput/         User input component
    diff/                Diff visualization
    ...150+ React components
  types/
    message.ts           Message type definitions
    permissions.ts       Permission type definitions
    hooks.ts             Hook type definitions
  coordinator/
    coordinatorMode.ts   Coordinator agent orchestration
  tasks/
    LocalShellTask/      Background shell tasks
    LocalAgentTask/      Background agent tasks
    InProcessTeammateTask/  In-process teammate tasks
```
