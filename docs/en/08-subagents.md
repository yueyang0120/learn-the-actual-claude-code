# Chapter 8: Subagents

Subagents allow Claude Code to decompose work into isolated, concurrent units of execution. The `AgentTool` in `src/tools/AgentTool/` (~1,500 lines) handles invocation, while `runAgent.ts` orchestrates the full lifecycle from model resolution through context assembly to cleanup. This chapter builds on Chapter 7: skills with `context: fork` execute as subagents, and the agent system itself uses many of the same patterns -- frontmatter definitions, tool resolution, permission scoping -- that the skill system establishes.

## The Problem

A single-threaded agent loop works for simple tasks but breaks down when work is naturally parallel or requires different capabilities. A code review might benefit from a fast read-only search agent running concurrently with a planning agent that reasons about architecture. A verification step should not share the main agent's write permissions. A long-running background task should not block the user's interactive session.

The core challenge is isolation: a child agent needs enough shared state to be useful (file caches, permissions, context) but enough independence that its failures, permission prompts, and side effects do not corrupt the parent. Additionally, the Anthropic API uses prefix caching, so a forked child that shares its parent's conversation prefix can get cache hits only if its system prompt, tools, and message prefix are byte-identical to the parent's.

## How Claude Code Solves It

### Agent Invocation

The `Agent` tool accepts a structured input:

```typescript
{
  description:       string    // 3-5 word task label
  prompt:            string    // the task for the agent
  subagent_type?:    string    // which agent definition to use
  model?:            'sonnet' | 'opus' | 'haiku'
  run_in_background?: boolean
}
```

The `call()` method validates permissions (checking deny rules for the specific agent type), selects the agent definition from `options.agentDefinitions.activeAgents`, assembles the tool pool, and dispatches either synchronously (yielding messages to the parent) or asynchronously (via `runAsyncAgentLifecycle()` in a detached promise).

### The runAgent Orchestration

`runAgent()` is an `AsyncGenerator<Message, void>` that yields messages as they arrive from the inner query loop. Its orchestration follows a strict sequence:

**Model resolution.** Each agent definition declares a preferred model. The resolution function respects the hierarchy:

```typescript
const resolvedAgentModel = getAgentModel(
  agentDefinition.model,                  // from definition, e.g. "haiku"
  toolUseContext.options.mainLoopModel,    // parent's model
  model,                                  // override from tool input
  permissionMode,
)
```

The value `"inherit"` means use the parent's model. Explore agents use `"haiku"` for speed; Plan and Verification agents use `"inherit"` for quality.

**Context message assembly.** Fork children receive a filtered copy of the parent's conversation. The filter removes assistant messages with orphaned `tool_use` blocks (those lacking matching `tool_result`) to prevent API errors:

```typescript
const contextMessages = forkContextMessages
  ? filterIncompleteToolCalls(forkContextMessages)
  : []
const initialMessages = [...contextMessages, ...promptMessages]
```

Fresh (non-fork) agents start with an empty context and only the prompt messages.

**File state cache.** Fork children clone the parent's file state cache to maintain cache-hit identity. Fresh agents start with an empty cache:

```typescript
const agentReadFileState = forkContextMessages !== undefined
  ? cloneFileStateCache(toolUseContext.readFileState)
  : createFileStateCacheWithSizeLimit(READ_FILE_STATE_CACHE_SIZE)
```

**System prompt construction.** For fork children, the parent's already-rendered system prompt bytes are passed via `override.systemPrompt` to ensure byte-exact cache matching. For other agents, the system prompt is built from the agent definition and enhanced with environment details:

```typescript
const agentSystemPrompt = override?.systemPrompt
  ? override.systemPrompt
  : asSystemPrompt(
      await getAgentSystemPrompt(
        agentDefinition, toolUseContext,
        resolvedAgentModel, additionalWorkingDirectories,
        resolvedTools,
      ),
    )
```

**CLAUDE.md omission.** Explore and Plan agents set `omitClaudeMd: true`. These read-only agents do not need project-specific commit, PR, or lint rules. Omitting CLAUDE.md saves approximately 5-15 Gtok/week across 34M+ Explore spawns fleet-wide.

**Permission mode override.** Each agent can run in a different permission mode than its parent:

```typescript
const agentGetAppState = () => {
  const state = toolUseContext.getAppState()
  let toolPermissionContext = state.toolPermissionContext
  if (agentPermissionMode) {
    toolPermissionContext = {
      ...toolPermissionContext,
      mode: agentPermissionMode,
    }
  }
  if (shouldAvoidPrompts) {
    toolPermissionContext = {
      ...toolPermissionContext,
      shouldAvoidPermissionPrompts: true,
    }
  }
  return { ...state, toolPermissionContext }
}
```

Async agents automatically set `shouldAvoidPermissionPrompts: true` since they have no terminal to prompt. Fork children use `permissionMode: 'bubble'` to surface prompts to the parent's terminal.

**Tool resolution.** Fork children use the exact same tool list as the parent (byte-identical for cache parity). Other agents go through `resolveAgentTools()`, which handles wildcard expansion (`tools: ['*']`), removal of `ALL_AGENT_DISALLOWED_TOOLS`, and `disallowedTools` filtering.

**The query loop.** After all setup, the agent enters the same `query()` function used by the main loop. Each message is recorded to the sidechain transcript and yielded to the caller:

```typescript
for await (const message of query({
  messages: initialMessages,
  systemPrompt: agentSystemPrompt,
  canUseTool,
  toolUseContext: agentToolUseContext,
  maxTurns: maxTurns ?? agentDefinition.maxTurns,
})) {
  await recordSidechainTranscript([message], agentId, lastRecordedUuid)
  yield message
}
```

**Cleanup.** The `finally` block is thorough, preventing memory leaks in sessions that spawn hundreds of agents:

```typescript
finally {
  await mcpCleanup()                         // agent-specific MCP servers
  clearSessionHooks(rootSetAppState, agentId) // frontmatter hooks
  cleanupAgentTracking(agentId)              // prompt cache tracking
  agentToolUseContext.readFileState.clear()   // release memory
  initialMessages.length = 0                 // release context messages
  killShellTasksForAgent(agentId, ...)       // kill background bash tasks
}
```

### Subagent Context Isolation

The function `createSubagentContext()` in `src/utils/forkedAgent.ts` builds an isolated `ToolUseContext` for the child. The pattern is consistent: mutable state (`readFileState`, `contentReplacementState`) is cloned for isolation. The abort controller is a new child linked to the parent -- parent abort propagates downward, but the child can abort independently. `setAppState` is a no-op for async agents (they must not mutate parent UI), though `setAppStateForTasks` still reaches the root store. Read-only fields (`options`, `fileReadingLimits`) are inherited by reference. A fresh `queryTracking` object with incremented depth enables analytics on agent nesting.

### CacheSafeParams

For fork children that need to share the parent's API prefix cache, the system captures five components that must be byte-identical between parent and child requests:

```typescript
export type CacheSafeParams = {
  systemPrompt:        SystemPrompt
  userContext:         { [k: string]: string }
  systemContext:       { [k: string]: string }
  toolUseContext:      ToolUseContext
  forkContextMessages: Message[]
}
```

The `saveCacheSafeParams()` / `getLastCacheSafeParams()` singleton pattern lets post-turn forks (prompt suggestions, background summarization) grab the main loop's cache-safe parameters without explicit threading.

### Built-In Agent Types

Four built-in agent types serve different roles:

**Explore** -- fast, read-only codebase search. Uses the `haiku` model externally for speed. Disallows Agent, Edit, and Write tools. Sets `omitClaudeMd: true`. The system prompt emphasizes parallel tool calls for throughput.

**Plan** -- software architecture and implementation planning. Inherits the parent's model for quality. Read-only, same tool restrictions as Explore. Outputs step-by-step plans with a "Critical Files for Implementation" section.

**General-Purpose** -- the default worker for multi-step tasks. Full tool access (`tools: ['*']`). No explicit model override. Brief, action-oriented prompt.

**Verification** -- post-implementation correctness checking. Inherits model, runs in the background (`background: true`), read-only for project files but can write to `/tmp`. Uses a detailed adversarial testing protocol with PASS/FAIL/PARTIAL verdict format. A critical system reminder is re-injected at every user turn.

### Custom Agent Definitions

Users can define agents in `.claude/agents/` using markdown or JSON format:

```markdown
---
name: my-researcher
description: "Searches codebase for patterns"
tools:
  - Glob
  - Grep
  - Read
model: haiku
maxTurns: 25
background: true
---

You are a research specialist. Your job is to...
```

The markdown body becomes the agent's system prompt. Agent definitions are merged from multiple sources with the same priority order as permission rules: built-in < plugin < userSettings < projectSettings < flagSettings < policySettings. Later sources override earlier ones, and `getActiveAgentsFromList()` deduplicates by agent type, keeping the highest-priority definition.

### Sidechain Transcript Recording

Every subagent's messages are persisted to disk for two purposes:

1. **Resume.** If a background agent is interrupted, `resumeAgentBackground()` reads the transcript back via `getAgentTranscript()` and continues from where it left off.
2. **Debugging.** Each agent gets a subdirectory under `subagents/` with metadata (agent type, worktree path, description).

Messages are recorded incrementally with UUID chain linkage:

```typescript
// Initial messages recorded before the query loop
void recordSidechainTranscript(initialMessages, agentId)

// Each subsequent message with parent UUID for ordering
await recordSidechainTranscript(
  [message], agentId, lastRecordedUuid
)
```

The `lastRecordedUuid` chain maintains parent-child ordering so the transcript can be reassembled in sequence. For fork children, transcript grouping uses a `transcriptSubdir` to cluster related agents.

## Key Design Decisions

**Clone mutable state, link immutable state.** File state caches and content replacement state (both mutable) are cloned. Options and file reading limits (both read-only) are inherited by reference. This prevents concurrent modification bugs without duplicating large immutable structures.

**No-op setAppState for async agents.** Async agents cannot safely mutate the parent's UI state. However, `setAppStateForTasks` is still shared so that task registration and kill operations reach the root store.

**Byte-identical prefix for cache parity.** Fork children receive the parent's exact system prompt bytes, tool list, and context messages. Any deviation invalidates the API cache and doubles cost. This explains `useExactTools: true` and the system prompt override pattern.

**Thorough cleanup in finally.** The cleanup block releases caches, clears context messages, kills shell tasks, unregisters hooks, and cleans up transcript routing -- defensive against leaks in sessions spawning hundreds of agents.

**CLAUDE.md omission for read-only agents.** Explore agents account for 34M+ spawns fleet-wide. Omitting CLAUDE.md saves significant token volume without affecting functionality.

## In Practice

When a user asks Claude Code to "research how authentication works in this codebase," the model spawns an Explore agent on the haiku model with read-only tools, executes parallel searches, and returns a summary as a tool result. For more complex work, the model might chain a Plan agent (design), a General-Purpose agent (implement), and a Verification agent (check) -- each running in its own context with appropriate tools and permissions. Background agents run concurrently without blocking the terminal. Custom agents in `.claude/agents/` let teams encode organizational workflows as reusable definitions.

## Summary

- Subagents decompose work into isolated units with independent models, tool sets, permission modes, and context windows.
- `createSubagentContext()` clones mutable state and links immutable state, with a no-op `setAppState` for async agents and a linked abort controller for cancellation propagation.
- Fork children share the parent's API prefix cache through byte-identical system prompts, tool lists, and context messages via `CacheSafeParams`.
- Four built-in agent types (Explore, Plan, General-Purpose, Verification) cover the spectrum from fast read-only search to adversarial correctness checking.
- Sidechain transcript recording enables resume of interrupted background agents and provides debugging visibility into agent execution chains.
