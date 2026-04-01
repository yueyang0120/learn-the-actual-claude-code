# Session 02 -- Source Analysis: Tool Interface & Registration

> All code snippets are from the real Claude Code source (v2.1.88).
> File paths are relative to the repo root (`src/`).

---

## 1. The `Tool` Interface (`src/Tool.ts`, lines 362-695)

The `Tool` type is a generic interface with three type parameters:

```typescript
export type Tool<
  Input extends AnyObject = AnyObject,       // Zod schema for input
  Output = unknown,                            // Tool-specific output type
  P extends ToolProgressData = ToolProgressData, // Progress event type
> = {
  // ... 30+ fields
}
```

Every tool in Claude Code -- Bash, FileRead, FileEdit, Agent, Skill, MCP wrappers -- must satisfy this interface. Here are the major field groups:

### 1.1 Identity & Metadata

```typescript
readonly name: string                    // Primary tool name
aliases?: string[]                       // Backwards-compat aliases
searchHint?: string                      // Keyword phrase for ToolSearch
readonly shouldDefer?: boolean           // Defer loading (use ToolSearch first)
readonly alwaysLoad?: boolean            // Never defer, always in prompt
isMcp?: boolean                          // Is this an MCP tool?
isLsp?: boolean                          // Is this an LSP tool?
mcpInfo?: { serverName: string; toolName: string }  // MCP provenance
```

**Design insight**: `searchHint` exists because when there are many tools (40+), Claude Code defers some tool definitions from the initial prompt. The model calls `ToolSearch` first, which uses keyword matching against `searchHint` to find the right tool. This saves prompt tokens.

### 1.2 Schema & Validation

```typescript
readonly inputSchema: Input              // Zod schema (strongly typed)
readonly inputJSONSchema?: ToolInputJSONSchema  // Raw JSON Schema (MCP tools)
outputSchema?: z.ZodType<unknown>        // Output schema (optional)
readonly strict?: boolean                // Strict mode for API

validateInput?(
  input: z.infer<Input>,
  context: ToolUseContext,
): Promise<ValidationResult>
```

`validateInput` runs **before** the permission check. It returns a structured result:

```typescript
export type ValidationResult =
  | { result: true }
  | { result: false; message: string; errorCode: number }
```

**Real example** from `FileReadTool.ts` (line 418-494): validates PDF page ranges, checks deny rules, detects blocked device paths like `/dev/zero`, and rejects binary extensions -- all before any filesystem I/O happens:

```typescript
async validateInput({ file_path, pages }, toolUseContext: ToolUseContext) {
  // 1. Validate pages parameter (pure string parsing, no I/O)
  if (pages !== undefined) {
    const parsed = parsePDFPageRange(pages)
    if (!parsed) {
      return { result: false, message: `Invalid pages parameter...`, errorCode: 7 }
    }
  }
  // 2. Path expansion + deny rule check (no I/O)
  const fullFilePath = expandPath(file_path)
  const denyRule = matchingRuleForInput(fullFilePath, ..., 'read', 'deny')
  if (denyRule !== null) {
    return { result: false, message: 'File is in a denied directory...', errorCode: 1 }
  }
  // 3. Binary extension check (string check only, no I/O)
  if (hasBinaryExtension(fullFilePath) && !isPDFExtension(ext) && !IMAGE_EXTENSIONS.has(...)) {
    return { result: false, message: `Cannot read binary files...`, errorCode: 4 }
  }
  // 4. Block device files that would hang
  if (isBlockedDevicePath(fullFilePath)) {
    return { result: false, message: `Cannot read device file...`, errorCode: 9 }
  }
  return { result: true }
}
```

### 1.3 Behavioral Flags

```typescript
isEnabled(): boolean                     // Is this tool currently available?
isReadOnly(input): boolean               // Does this invocation only read?
isConcurrencySafe(input): boolean        // Safe to run in parallel?
isDestructive?(input): boolean           // Irreversible operation?
interruptBehavior?(): 'cancel' | 'block' // What happens on user interrupt
```

These flags drive the **tool orchestration** layer (Session 03). The orchestrator partitions pending tool calls into two groups:
- **Concurrent batch**: tools where `isConcurrencySafe(input)` returns `true` (up to 10 in parallel)
- **Serial queue**: tools where it returns `false` (run one at a time, after the concurrent batch)

**Key design decision**: `isConcurrencySafe` takes the `input` as an argument, not just the tool. This matters for BashTool, where `ls` is read-only/concurrent-safe but `rm -rf` is not:

```typescript
// From BashTool.tsx, line 434-441:
isConcurrencySafe(input) {
  return this.isReadOnly?.(input) ?? false;
},
isReadOnly(input) {
  const compoundCommandHasCd = commandHasAnyCd(input.command);
  const result = checkReadOnlyConstraints(input, compoundCommandHasCd);
  return result.behavior === 'allow';
},
```

### 1.4 The `call()` Method

```typescript
call(
  args: z.infer<Input>,
  context: ToolUseContext,
  canUseTool: CanUseToolFn,
  parentMessage: AssistantMessage,
  onProgress?: ToolCallProgress<P>,
): Promise<ToolResult<Output>>
```

Five parameters. Not just `(input) => output`. The tool receives:
- **args**: parsed, validated input
- **context**: the full `ToolUseContext` (see Section 2)
- **canUseTool**: a callback to check if the tool is allowed (used by AgentTool to check nested tool permissions)
- **parentMessage**: the assistant message that triggered this tool call
- **onProgress**: streaming progress callback (for long-running operations like Bash)

The return type wraps the output with optional side effects:

```typescript
export type ToolResult<T> = {
  data: T
  newMessages?: (UserMessage | AssistantMessage | AttachmentMessage | SystemMessage)[]
  contextModifier?: (context: ToolUseContext) => ToolUseContext
  mcpMeta?: { _meta?: Record<string, unknown>; structuredContent?: Record<string, unknown> }
}
```

`contextModifier` is powerful: a tool can modify the context for subsequent tool calls (only honored for non-concurrent-safe tools). `newMessages` injects synthetic messages into the conversation.

### 1.5 Permission System Integration

```typescript
checkPermissions(
  input: z.infer<Input>,
  context: ToolUseContext,
): Promise<PermissionResult>

preparePermissionMatcher?(
  input: z.infer<Input>,
): Promise<(pattern: string) => boolean>

getPath?(input: z.infer<Input>): string
```

Every tool has a `checkPermissions` method. The default (from `buildTool`) auto-allows, deferring to the general permission system. Tools with special needs override it.

`preparePermissionMatcher` is fascinating: it's called once per hook-input pair and returns a **closure** used to match hook patterns. BashTool parses the command into subcommands so that `Bash(git *)` matches `FOO=bar git push`:

```typescript
// From BashTool.tsx, line 445-467:
async preparePermissionMatcher({ command }) {
  const parsed = await parseForSecurity(command);
  if (parsed.kind !== 'simple') {
    return () => true;  // Fail safe: run the hook for complex commands
  }
  const subcommands = parsed.commands.map(c => c.argv.join(' '));
  return pattern => {
    const prefix = permissionRuleExtractPrefix(pattern);
    return subcommands.some(cmd => {
      if (prefix !== null) return cmd === prefix || cmd.startsWith(`${prefix} `);
      return matchWildcardPattern(pattern, cmd);
    });
  };
},
```

### 1.6 UI & Rendering (7+ methods)

```typescript
userFacingName(input): string
renderToolUseMessage(input, options): React.ReactNode
renderToolResultMessage?(content, progressMessages, options): React.ReactNode
renderToolUseProgressMessage?(progressMessages, options): React.ReactNode
renderToolUseRejectedMessage?(input, options): React.ReactNode
renderToolUseErrorMessage?(result, options): React.ReactNode
renderGroupedToolUse?(toolUses, options): React.ReactNode
renderToolUseTag?(input): React.ReactNode
```

Tools own their own rendering. This is a React/Ink application -- every tool renders its own progress, results, errors, and rejection messages as React nodes.

### 1.7 Result Persistence

```typescript
maxResultSizeChars: number  // Threshold before result is persisted to disk
```

When a tool result exceeds this threshold, it's saved to a temp file and the model receives a preview with a file path. FileReadTool sets this to `Infinity` -- persisting a Read result would create a circular loop (Read -> file -> Read).

---

## 2. `ToolUseContext` (`src/Tool.ts`, lines 158-300)

This is the **context object threaded through every tool call**. It's massive (~40 fields). Here are the key groups:

### 2.1 Core Options

```typescript
options: {
  commands: Command[]                    // Available slash commands
  debug: boolean
  mainLoopModel: string                  // Which Claude model
  tools: Tools                           // All registered tools
  verbose: boolean
  thinkingConfig: ThinkingConfig
  mcpClients: MCPServerConnection[]      // Connected MCP servers
  mcpResources: Record<string, ServerResource[]>
  isNonInteractiveSession: boolean       // SDK/CI vs interactive REPL
  agentDefinitions: AgentDefinitionsResult
  maxBudgetUsd?: number                  // Spending limit
  customSystemPrompt?: string
  appendSystemPrompt?: string
  refreshTools?: () => Tools             // Live tool refresh (MCP mid-query)
}
```

### 2.2 State Management

```typescript
abortController: AbortController         // Cancellation signal
readFileState: FileStateCache             // Shared file read cache (dedup)
getAppState(): AppState                   // Read Zustand store
setAppState(f: (prev: AppState) => AppState): void  // Update store
setAppStateForTasks?: (...)               // Always-shared store for background tasks
messages: Message[]                       // Current conversation
```

### 2.3 UI Callbacks

```typescript
setToolJSX?: SetToolJSXFn               // Render custom UI during tool execution
addNotification?: (notif: Notification) => void
appendSystemMessage?: (msg: SystemMessage) => void
sendOSNotification?: (opts: {...}) => void
setStreamMode?: (mode: SpinnerMode) => void
openMessageSelector?: () => void
```

### 2.4 Tracking & Limits

```typescript
fileReadingLimits?: { maxTokens?: number; maxSizeBytes?: number }
globLimits?: { maxResults?: number }
toolDecisions?: Map<string, { source: string; decision: 'accept' | 'reject'; timestamp: number }>
queryTracking?: QueryChainTracking
setInProgressToolUseIDs: (f: (prev: Set<string>) => Set<string>) => void
setResponseLength: (f: (prev: number) => number) => void
```

### 2.5 Agent & Session Metadata

```typescript
agentId?: AgentId                         // Only set for subagents
agentType?: string                        // Subagent type name
contentReplacementState?: ContentReplacementState  // Tool result budget
renderedSystemPrompt?: SystemPrompt       // Parent's prompt (for cache sharing)
localDenialTracking?: DenialTrackingState // For async subagents
```

**Design insight**: `contentReplacementState` implements the "tool result budget" -- when the conversation context grows large, older tool results are replaced with references to persisted files on disk, keeping the context within model limits.

---

## 3. `buildTool` -- Default Factory (`src/Tool.ts`, lines 757-792)

Not every tool needs all 30+ fields. `buildTool` provides fail-closed defaults:

```typescript
const TOOL_DEFAULTS = {
  isEnabled: () => true,
  isConcurrencySafe: (_input?: unknown) => false,   // Assume NOT safe
  isReadOnly: (_input?: unknown) => false,           // Assume writes
  isDestructive: (_input?: unknown) => false,
  checkPermissions: (input, _ctx?) =>
    Promise.resolve({ behavior: 'allow', updatedInput: input }),
  toAutoClassifierInput: (_input?: unknown) => '',
  userFacingName: (_input?: unknown) => '',
}

export function buildTool<D extends AnyToolDef>(def: D): BuiltTool<D> {
  return {
    ...TOOL_DEFAULTS,
    userFacingName: () => def.name,  // Default: use the tool name
    ...def,                           // Tool overrides win
  } as BuiltTool<D>
}
```

**Key design decisions**:
- `isConcurrencySafe` defaults to `false` -- **fail closed**. A new tool runs serially until the author explicitly marks it concurrent-safe.
- `isReadOnly` defaults to `false` -- assume the tool writes until proven otherwise.
- `checkPermissions` defaults to `allow` -- the general permission system handles most cases. Tools override only for tool-specific logic.

Usage in FileReadTool:

```typescript
export const FileReadTool = buildTool({
  name: FILE_READ_TOOL_NAME,
  searchHint: 'read files, images, PDFs, notebooks',
  maxResultSizeChars: Infinity,           // Never persist (circular loop)
  strict: true,
  isConcurrencySafe() { return true },    // Override: reads are safe
  isReadOnly() { return true },           // Override: reads don't write
  // ... call, validateInput, checkPermissions, render methods
} satisfies ToolDef<InputSchema, Output>)
```

---

## 4. Tool Registration (`src/tools.ts`)

### 4.1 `getAllBaseTools()` -- The Master List (lines 193-251)

This function is the **single source of truth** for all built-in tools:

```typescript
export function getAllBaseTools(): Tools {
  return [
    AgentTool,
    TaskOutputTool,
    BashTool,
    // Embedded search tools replace Glob/Grep in ant-native builds
    ...(hasEmbeddedSearchTools() ? [] : [GlobTool, GrepTool]),
    ExitPlanModeV2Tool,
    FileReadTool,
    FileEditTool,
    FileWriteTool,
    NotebookEditTool,
    WebFetchTool,
    TodoWriteTool,
    WebSearchTool,
    TaskStopTool,
    AskUserQuestionTool,
    SkillTool,
    EnterPlanModeTool,
    // ... conditional tools follow
  ]
}
```

### 4.2 Feature-Gated Conditional Loading

Three gating mechanisms are used:

**Mechanism 1: `feature()` flags (Bun dead-code elimination)**

```typescript
import { feature } from 'bun:bundle'

const SleepTool = feature('PROACTIVE') || feature('KAIROS')
  ? require('./tools/SleepTool/SleepTool.js').SleepTool
  : null

const cronTools = feature('AGENT_TRIGGERS')
  ? [CronCreateTool, CronDeleteTool, CronListTool]
  : []

const WebBrowserTool = feature('WEB_BROWSER_TOOL')
  ? require('./tools/WebBrowserTool/WebBrowserTool.js').WebBrowserTool
  : null

const SnipTool = feature('HISTORY_SNIP')
  ? require('./tools/SnipTool/SnipTool.js').SnipTool
  : null
```

These `feature()` calls resolve at **bundle time**. When a flag is false, the `require()` call and all its transitive dependencies are eliminated from the bundle entirely. Feature flags seen in the source:

| Flag | Tools Gated |
|------|-------------|
| `PROACTIVE` | SleepTool |
| `KAIROS` | SleepTool, SendUserFileTool, PushNotificationTool |
| `KAIROS_PUSH_NOTIFICATION` | PushNotificationTool |
| `KAIROS_GITHUB_WEBHOOKS` | SubscribePRTool |
| `AGENT_TRIGGERS` | CronCreateTool, CronDeleteTool, CronListTool |
| `AGENT_TRIGGERS_REMOTE` | RemoteTriggerTool |
| `MONITOR_TOOL` | MonitorTool |
| `OVERFLOW_TEST_TOOL` | OverflowTestTool |
| `CONTEXT_COLLAPSE` | CtxInspectTool |
| `TERMINAL_PANEL` | TerminalCaptureTool |
| `WEB_BROWSER_TOOL` | WebBrowserTool |
| `COORDINATOR_MODE` | Coordinator mode tooling |
| `HISTORY_SNIP` | SnipTool |
| `UDS_INBOX` | ListPeersTool |
| `WORKFLOW_SCRIPTS` | WorkflowTool |

**Mechanism 2: `process.env` checks**

```typescript
const REPLTool = process.env.USER_TYPE === 'ant'
  ? require('./tools/REPLTool/REPLTool.js').REPLTool
  : null

// In getAllBaseTools():
...(process.env.USER_TYPE === 'ant' ? [ConfigTool] : []),
...(process.env.USER_TYPE === 'ant' ? [TungstenTool] : []),
...(process.env.NODE_ENV === 'test' ? [TestingPermissionTool] : []),
```

**Mechanism 3: Runtime helper functions**

```typescript
...(hasEmbeddedSearchTools() ? [] : [GlobTool, GrepTool]),
...(isTodoV2Enabled() ? [TaskCreateTool, TaskGetTool, TaskUpdateTool, TaskListTool] : []),
...(isWorktreeModeEnabled() ? [EnterWorktreeTool, ExitWorktreeTool] : []),
...(isAgentSwarmsEnabled() ? [getTeamCreateTool(), getTeamDeleteTool()] : []),
...(isToolSearchEnabledOptimistic() ? [ToolSearchTool] : []),
```

### 4.3 Lazy Requires to Break Cycles

Some tools have circular import dependencies. These use lazy getter functions:

```typescript
const getTeamCreateTool = () =>
  require('./tools/TeamCreateTool/TeamCreateTool.js').TeamCreateTool
const getTeamDeleteTool = () =>
  require('./tools/TeamDeleteTool/TeamDeleteTool.js').TeamDeleteTool
const getSendMessageTool = () =>
  require('./tools/SendMessageTool/SendMessageTool.js').SendMessageTool
```

### 4.4 `getTools()` -- Permission-Filtered View (lines 271-327)

`getTools()` applies three filters on top of `getAllBaseTools()`:

1. **Simple mode** (`CLAUDE_CODE_SIMPLE`): only Bash, FileRead, FileEdit
2. **Deny rules**: `filterToolsByDenyRules()` removes blanket-denied tools
3. **REPL mode**: hides primitive tools (Bash, Read, Edit) when REPL wraps them
4. **isEnabled()**: final filter removes disabled tools

```typescript
export const getTools = (permissionContext: ToolPermissionContext): Tools => {
  if (isEnvTruthy(process.env.CLAUDE_CODE_SIMPLE)) {
    const simpleTools: Tool[] = [BashTool, FileReadTool, FileEditTool]
    return filterToolsByDenyRules(simpleTools, permissionContext)
  }

  const tools = getAllBaseTools().filter(tool => !specialTools.has(tool.name))
  let allowedTools = filterToolsByDenyRules(tools, permissionContext)

  // REPL mode: hide primitive tools
  if (isReplModeEnabled()) {
    const replEnabled = allowedTools.some(tool => toolMatchesName(tool, REPL_TOOL_NAME))
    if (replEnabled) {
      allowedTools = allowedTools.filter(tool => !REPL_ONLY_TOOLS.has(tool.name))
    }
  }

  const isEnabled = allowedTools.map(_ => _.isEnabled())
  return allowedTools.filter((_, i) => isEnabled[i])
}
```

---

## 5. MCP Tool Integration (`src/tools.ts`, lines 345-389)

### 5.1 `assembleToolPool()` -- Merging Built-in + MCP

```typescript
export function assembleToolPool(
  permissionContext: ToolPermissionContext,
  mcpTools: Tools,
): Tools {
  const builtInTools = getTools(permissionContext)
  const allowedMcpTools = filterToolsByDenyRules(mcpTools, permissionContext)

  // Sort each partition for prompt-cache stability
  const byName = (a: Tool, b: Tool) => a.name.localeCompare(b.name)
  return uniqBy(
    [...builtInTools].sort(byName).concat(allowedMcpTools.sort(byName)),
    'name',
  )
}
```

**Why sort?** The API server places a cache breakpoint after the last built-in tool. If MCP tools interleave with built-ins, every new MCP tool invalidates all downstream cache keys. By sorting built-ins as a contiguous prefix, only the MCP suffix changes.

**Why `uniqBy('name')`?** Built-in tools take precedence over MCP tools with the same name. This prevents MCP servers from shadowing core tools.

---

## 6. Design Decisions: Why This Complexity?

### Q: Why not just `{ name: string, handler: Function }`?

The real system needs:
- **Input-dependent behavior**: `isConcurrencySafe(input)` -- whether `ls` vs `rm` can run in parallel
- **Progressive disclosure**: `shouldDefer` + `searchHint` -- show 15 tools upfront, defer 30 more behind search
- **Fail-closed defaults**: `buildTool` makes new tools serial/write/non-destructive until proven otherwise
- **Permission granularity**: `preparePermissionMatcher` compiles input-specific matchers for hook patterns
- **Result management**: `maxResultSizeChars` controls when results overflow to disk
- **UI ownership**: tools render their own progress, results, errors -- not a generic handler
- **MCP compatibility**: `inputJSONSchema` lets MCP tools bypass Zod, `mcpInfo` tracks provenance

### Q: Why are feature gates at bundle time, not runtime?

Bun's `feature()` from `bun:bundle` eliminates dead code at build time. When `feature('KAIROS')` is false, the entire SleepTool module (and its transitive dependencies) is stripped from the bundle. This reduces binary size and startup time for users who don't have access to unreleased features.

### Q: Why does `ToolUseContext` have 40 fields?

Because a tool call isn't just "run this function." It needs to:
- Respect cancellation (`abortController`)
- Deduplicate file reads (`readFileState`)
- Update global state (`setAppState`)
- Track spending (`maxBudgetUsd`)
- Render custom UI (`setToolJSX`)
- Integrate with MCP servers (`mcpClients`)
- Support subagent isolation (`agentId`, `contentReplacementState`)
- Drive permission decisions (`toolDecisions`, `localDenialTracking`)

The alternative would be a god object or global state. Threading context explicitly is verbose but makes dependencies visible and testable.

---

## Summary

The tool system in Claude Code is not a lookup table -- it's a **typed protocol** with behavioral contracts (concurrency, read-only, destructive), a **factory with fail-closed defaults** (`buildTool`), a **feature-gated registry** (`getAllBaseTools`), and a **merge pipeline** (`assembleToolPool`) that respects prompt caching. Every field exists because a real production need demanded it.
