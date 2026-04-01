# Source Analysis -- Subagents

## 1. AgentTool -- How It Is Invoked

**File:** `src/tools/AgentTool/AgentTool.tsx`

The `Agent` tool is built with `buildTool()` and exposed to the model under the
name `Agent` (constant `AGENT_TOOL_NAME`). Its input schema accepts:

```typescript
// Simplified from the real lazy-evaluated Zod schema
{
  description:      string   // 3-5 word task label
  prompt:           string   // the task for the agent
  subagent_type?:   string   // which agent definition to use
  model?:           'sonnet' | 'opus' | 'haiku'
  run_in_background?: boolean
  isolation?:       'worktree' | 'remote'
  // Multi-agent fields (gated):
  name?:            string
  team_name?:       string
  mode?:            PermissionMode
}
```

When the fork-subagent experiment is active, `subagent_type` becomes truly
optional -- omitting it triggers the **fork path** where the child inherits the
parent's full conversation and system prompt. When the experiment is off,
omitting `subagent_type` defaults to `"general-purpose"`.

The `call()` method:

1. **Validates permissions** -- checks `getDenyRuleForAgent()` and
   `filterDeniedAgents()`.
2. **Selects agent definition** -- looks up `subagent_type` in
   `options.agentDefinitions.activeAgents`.
3. **Assembles tool pool** -- calls `assembleToolPool()` with the worker's
   permission context (independent of parent restrictions).
4. **Dispatches sync or async** -- background agents run via
   `runAsyncAgentLifecycle()` in a detached promise; sync agents yield messages
   directly.

---

## 2. runAgent.ts -- The Orchestration

**File:** `src/tools/AgentTool/runAgent.ts`

`runAgent()` is an `AsyncGenerator<Message, void>` -- it yields messages as
they arrive from the inner `query()` loop. The orchestration steps:

### 2a. Model Resolution

```typescript
const resolvedAgentModel = getAgentModel(
  agentDefinition.model,    // from frontmatter, e.g. "haiku", "inherit"
  toolUseContext.options.mainLoopModel,  // parent's model
  model,                    // override from tool input
  permissionMode,
)
```

`"inherit"` means use the parent's model. Explore uses `"haiku"` for speed;
Plan and verification use `"inherit"` for quality.

### 2b. Context Message Assembly

```typescript
// Fork parent context or start fresh
const contextMessages = forkContextMessages
  ? filterIncompleteToolCalls(forkContextMessages)
  : []
const initialMessages = [...contextMessages, ...promptMessages]
```

`filterIncompleteToolCalls()` removes assistant messages with orphaned
`tool_use` blocks (those without matching `tool_result`) to avoid API errors.

### 2c. File State Cache

```typescript
const agentReadFileState = forkContextMessages !== undefined
  ? cloneFileStateCache(toolUseContext.readFileState)
  : createFileStateCacheWithSizeLimit(READ_FILE_STATE_CACHE_SIZE)
```

Fork children clone the parent's cache (for cache-hit identity); fresh agents
start empty.

### 2d. System Prompt Construction

```typescript
const agentSystemPrompt = override?.systemPrompt
  ? override.systemPrompt
  : asSystemPrompt(
      await getAgentSystemPrompt(
        agentDefinition,
        toolUseContext,
        resolvedAgentModel,
        additionalWorkingDirectories,
        resolvedTools,
      ),
    )
```

`getAgentSystemPrompt()` calls `agentDefinition.getSystemPrompt()` then wraps
it with `enhanceSystemPromptWithEnvDetails()` which appends environment info
(cwd, platform, shell, enabled tool names).

For fork children, the parent's already-rendered system prompt bytes are passed
via `override.systemPrompt` for byte-exact cache matching.

### 2e. CLAUDE.md Omission for Read-Only Agents

```typescript
const shouldOmitClaudeMd =
  agentDefinition.omitClaudeMd &&
  !override?.userContext &&
  getFeatureValue_CACHED_MAY_BE_STALE('tengu_slim_subagent_claudemd', true)
```

Explore and Plan agents set `omitClaudeMd: true`. They are read-only -- they
do not need commit/PR/lint rules. This saves approximately 5-15 Gtok/week
across 34M+ Explore spawns fleet-wide.

### 2f. Permission Mode Override

```typescript
const agentGetAppState = () => {
  const state = toolUseContext.getAppState()
  let toolPermissionContext = state.toolPermissionContext
  if (agentPermissionMode && mode !== 'bypassPermissions' && mode !== 'acceptEdits') {
    toolPermissionContext = { ...toolPermissionContext, mode: agentPermissionMode }
  }
  if (shouldAvoidPrompts) {
    toolPermissionContext = {
      ...toolPermissionContext,
      shouldAvoidPermissionPrompts: true,
    }
  }
  // ... scoped tool permissions via allowedTools
  return { ...state, toolPermissionContext }
}
```

Async agents automatically set `shouldAvoidPermissionPrompts: true` since they
cannot show UI. The fork path uses `permissionMode: 'bubble'` to surface
prompts to the parent terminal.

### 2g. Tool Resolution

```typescript
const resolvedTools = useExactTools
  ? availableTools                       // fork path: byte-identical tool list
  : resolveAgentTools(agentDefinition, availableTools, isAsync).resolvedTools
```

`resolveAgentTools()` handles wildcard expansion (`tools: ['*']`), tool
filtering via `filterToolsForAgent()` (removes `ALL_AGENT_DISALLOWED_TOOLS`),
and `disallowedTools` removal.

### 2h. Hook and Skill Registration

```typescript
// Frontmatter hooks scoped to agent lifecycle
if (agentDefinition.hooks) {
  registerFrontmatterHooks(rootSetAppState, agentId, agentDefinition.hooks, ...)
}

// Preload skills from frontmatter
for (const { skillName, skill, content } of loaded) {
  initialMessages.push(createUserMessage({ content: [...content], isMeta: true }))
}
```

Hooks are registered with the agent's `agentId` as scope key and cleaned up in
the `finally` block. Skills are loaded concurrently and injected as user
messages.

### 2i. Subagent Context Creation

```typescript
const agentToolUseContext = createSubagentContext(toolUseContext, {
  options: agentOptions,
  agentId,
  agentType: agentDefinition.agentType,
  messages: initialMessages,
  readFileState: agentReadFileState,
  abortController: agentAbortController,
  getAppState: agentGetAppState,
  shareSetAppState: !isAsync,
  shareSetResponseLength: true,
  contentReplacementState,
})
```

See Section 4 for `createSubagentContext()` details.

### 2j. The Query Loop

```typescript
for await (const message of query({
  messages: initialMessages,
  systemPrompt: agentSystemPrompt,
  userContext: resolvedUserContext,
  systemContext: resolvedSystemContext,
  canUseTool,
  toolUseContext: agentToolUseContext,
  querySource,
  maxTurns: maxTurns ?? agentDefinition.maxTurns,
})) {
  // Record to sidechain transcript
  await recordSidechainTranscript([message], agentId, lastRecordedUuid)
  yield message
}
```

Each message is recorded incrementally (O(1) per message) and yielded to the
caller. Stream events (TTFT metrics) are forwarded to the parent's metrics
display.

### 2k. Cleanup (finally block)

```typescript
finally {
  await mcpCleanup()                         // agent-specific MCP servers
  clearSessionHooks(rootSetAppState, agentId) // frontmatter hooks
  cleanupAgentTracking(agentId)              // prompt cache tracking
  agentToolUseContext.readFileState.clear()   // release memory
  initialMessages.length = 0                 // release context messages
  unregisterPerfettoAgent(agentId)           // tracing
  clearAgentTranscriptSubdir(agentId)        // transcript routing
  // Remove orphaned todos entry
  rootSetAppState(prev => { ... })
  // Kill background bash tasks
  killShellTasksForAgent(agentId, ...)
}
```

Thorough cleanup prevents memory leaks in whale sessions that spawn hundreds of
agents.

---

## 3. CacheSafeParams -- Sharing the Parent's Prompt Cache

**File:** `src/utils/forkedAgent.ts`

```typescript
/**
 * Parameters that must be identical between fork and parent API requests
 * to share the parent's prompt cache. The Anthropic API cache key is composed of:
 * system prompt, tools, model, messages (prefix), and thinking config.
 */
export type CacheSafeParams = {
  systemPrompt:        SystemPrompt
  userContext:         { [k: string]: string }
  systemContext:       { [k: string]: string }
  toolUseContext:      ToolUseContext
  forkContextMessages: Message[]
}
```

The `saveCacheSafeParams()` / `getLastCacheSafeParams()` singleton pattern lets
post-turn forks (prompt suggestion, BTW commands, background summarization) grab
the main loop's cache-safe params without threading them explicitly.

`runForkedAgent()` uses these params to run an isolated query loop:

```typescript
export async function runForkedAgent({
  promptMessages, cacheSafeParams, canUseTool,
  querySource, forkLabel, maxTurns, ...
}): Promise<ForkedAgentResult> {
  const { systemPrompt, userContext, systemContext, toolUseContext, forkContextMessages } =
    cacheSafeParams
  const isolatedToolUseContext = createSubagentContext(toolUseContext, overrides)
  const initialMessages = [...forkContextMessages, ...promptMessages]
  // ... query loop with usage tracking
}
```

Usage is accumulated across all API calls and logged via
`tengu_fork_agent_query` with cache hit rate metrics.

---

## 4. Forked Context Creation -- createSubagentContext()

**File:** `src/utils/forkedAgent.ts`

```typescript
export function createSubagentContext(
  parentContext: ToolUseContext,
  overrides?: SubagentContextOverrides,
): ToolUseContext {
  // AbortController: override > share parent's > new child linked to parent
  const abortController =
    overrides?.abortController ??
    (overrides?.shareAbortController
      ? parentContext.abortController
      : createChildAbortController(parentContext.abortController))

  return {
    // CLONED state (isolation)
    readFileState: cloneFileStateCache(overrides?.readFileState ?? parentContext.readFileState),
    nestedMemoryAttachmentTriggers: new Set<string>(),
    toolDecisions: undefined,
    contentReplacementState: overrides?.contentReplacementState ??
      (parentContext.contentReplacementState
        ? cloneContentReplacementState(parentContext.contentReplacementState)
        : undefined),

    // ABORT
    abortController,

    // APP STATE
    getAppState,                           // wrapped to set shouldAvoidPermissionPrompts
    setAppState: overrides?.shareSetAppState ? parentContext.setAppState : () => {},
    setAppStateForTasks: parentContext.setAppStateForTasks ?? parentContext.setAppState,

    // NO-OP UI callbacks (subagents cannot control parent UI)
    addNotification: undefined,
    setToolJSX: undefined,
    setStreamMode: undefined,

    // INHERITED (read-only)
    options: overrides?.options ?? parentContext.options,
    fileReadingLimits: parentContext.fileReadingLimits,

    // FRESH tracking
    queryTracking: {
      chainId: randomUUID(),
      depth: (parentContext.queryTracking?.depth ?? -1) + 1,
    },
    agentId: overrides?.agentId ?? createAgentId(),
  }
}
```

Key design decisions:

| Concern | Default | Rationale |
|---------|---------|-----------|
| `readFileState` | Clone from parent | Prevents concurrent modification; fork children need identical replacement decisions for cache hits |
| `abortController` | New child linked to parent | Parent abort propagates, but child can abort independently |
| `setAppState` | No-op | Async agents should not mutate parent UI state |
| `setAppStateForTasks` | Share parent's | Task registration/kill must reach root store even when setAppState is no-op |
| `contentReplacementState` | Clone | Fork children make identical replacement decisions for cache-identical wire prefixes |
| `queryTracking.depth` | Parent + 1 | Tracks nesting depth for analytics |

---

## 5. Agent Definitions from .claude/agents/

**File:** `src/tools/AgentTool/loadAgentsDir.ts`

Agents can be defined in two formats:

### Markdown format (`.claude/agents/*.md`)

```markdown
---
name: my-researcher
description: "Searches codebase for patterns and reports findings"
tools:
  - Glob
  - Grep
  - Read
  - Bash
model: haiku
permissionMode: acceptEdits
maxTurns: 25
background: true
memory: project
isolation: worktree
hooks:
  SubagentStart:
    - command: "echo Starting agent"
---

You are a research specialist. Your job is to...
```

Frontmatter is parsed by `parseAgentFromMarkdown()`. The markdown body becomes
the system prompt via a closure:

```typescript
getSystemPrompt: () => {
  if (isAutoMemoryEnabled() && memory) {
    return systemPrompt + '\n\n' + loadAgentMemoryPrompt(agentType, memory)
  }
  return systemPrompt
}
```

### JSON format (via settings)

```json
{
  "my-researcher": {
    "description": "Searches codebase",
    "prompt": "You are a research specialist...",
    "tools": ["Glob", "Grep", "Read"],
    "model": "haiku",
    "permissionMode": "acceptEdits"
  }
}
```

Parsed by `parseAgentFromJson()` with Zod validation via `AgentJsonSchema`.

### Resolution Priority

Agents are merged with later sources overriding earlier ones:

```
built-in < plugin < userSettings < projectSettings < flagSettings < policySettings
```

The `getActiveAgentsFromList()` function deduplicates by `agentType`, keeping
the highest-priority definition.

### AgentDefinition Type Union

```typescript
type AgentDefinition =
  | BuiltInAgentDefinition    // source: 'built-in', dynamic getSystemPrompt()
  | CustomAgentDefinition     // source: SettingSource, closure-based prompt
  | PluginAgentDefinition     // source: 'plugin', plugin metadata
```

All variants share `BaseAgentDefinition` fields: `agentType`, `whenToUse`,
`tools`, `model`, `permissionMode`, `maxTurns`, `hooks`, `mcpServers`, etc.

---

## 6. Built-In Agent Types

**File:** `src/tools/AgentTool/builtInAgents.ts` and `built-in/*.ts`

### Explore Agent

- **Purpose:** Fast read-only codebase search
- **Model:** `haiku` (external) / `inherit` (internal)
- **Key constraints:** `omitClaudeMd: true`, disallows Agent/Edit/Write tools
- **Prompt emphasis:** "NOTE: You are meant to be a fast agent... spawn
  multiple parallel tool calls"

### Plan Agent

- **Purpose:** Software architecture and implementation planning
- **Model:** `inherit`
- **Key constraints:** `omitClaudeMd: true`, read-only, same disallowed tools
  as Explore
- **Output format:** Step-by-step plan with "Critical Files for Implementation"

### General-Purpose Agent

- **Purpose:** Default worker for multi-step tasks
- **Model:** Uses `getDefaultSubagentModel()` (no explicit model)
- **Key constraints:** `tools: ['*']` (all tools available)
- **Prompt:** Brief, action-oriented -- "Complete the task fully -- don't
  gold-plate, but don't leave it half-done"

### Verification Agent

- **Purpose:** Post-implementation correctness verification
- **Model:** `inherit`
- **Key constraints:** `background: true`, `color: 'red'`, read-only for
  project files (can write to /tmp)
- **Prompt:** Detailed adversarial testing protocol with PASS/FAIL/PARTIAL
  verdict format
- **Critical reminder:** Re-injected at every user turn via
  `criticalSystemReminder_EXPERIMENTAL`

### Fork Agent (Experiment)

- **Purpose:** Implicit context fork when `subagent_type` is omitted
- **Model:** `inherit` (cache parity)
- **Key constraints:** `tools: ['*']`, `permissionMode: 'bubble'`,
  `useExactTools: true`
- **Guard:** `isInForkChild()` checks for `<fork-boilerplate>` tag to prevent
  recursive forking

```typescript
export const FORK_AGENT = {
  agentType: 'fork',
  tools: ['*'],
  maxTurns: 200,
  model: 'inherit',
  permissionMode: 'bubble',
  source: 'built-in',
  getSystemPrompt: () => '',  // unused -- override.systemPrompt is threaded
} satisfies BuiltInAgentDefinition
```

---

## 7. Sidechain Transcript Recording

Every subagent's messages are persisted to disk via `recordSidechainTranscript()`
for two purposes:

1. **Resume:** If a background agent is interrupted, `resumeAgentBackground()`
   can read the transcript back with `getAgentTranscript()` and continue.
2. **Debugging:** Each agent gets a subdirectory under `subagents/` with
   metadata (agent type, worktree path, description).

```typescript
// Initial messages recorded before the query loop
void recordSidechainTranscript(initialMessages, agentId)

// Metadata written alongside
void writeAgentMetadata(agentId, {
  agentType: agentDefinition.agentType,
  ...(worktreePath && { worktreePath }),
  ...(description && { description }),
})

// Each message recorded incrementally with parent UUID linkage
await recordSidechainTranscript([message], agentId, lastRecordedUuid)
```

The `lastRecordedUuid` chain maintains parent-child ordering so the transcript
can be reassembled in order. Progress messages do not update the UUID chain
(they are interstitial).

For fork children, transcript grouping uses `transcriptSubdir` (e.g.,
`workflows/<runId>`) to cluster related agents.
