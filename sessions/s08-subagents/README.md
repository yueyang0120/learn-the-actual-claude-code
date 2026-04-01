# Session 08 -- Subagents

## Overview

Claude Code's subagent system is one of its most sophisticated subsystems. When
the main agent decides a task is better handled by a specialist -- code search,
planning, verification, or a user-defined worker -- it spawns a **subagent**
through the `Agent` tool. Each subagent runs its own `query()` loop with its own
system prompt, tool set, and permission mode, yet it can share the parent's
**prompt cache** to avoid re-processing thousands of context tokens.

This session walks through the real implementation from top to bottom: tool
definition, orchestration, context forking, built-in agent types, custom agents
loaded from `.claude/agents/`, the fork-subagent experiment, and sidechain
transcript recording.

## Learning Objectives

1. **Understand the AgentTool lifecycle** -- how `AgentTool.tsx` validates
   input, selects an agent definition, assembles a tool pool, and delegates to
   `runAgent()`.
2. **Trace the runAgent orchestration** -- model resolution, system prompt
   construction, context forking, permission mode overrides, hook registration,
   and the inner `query()` loop.
3. **Explain CacheSafeParams** -- the struct that carries the five cache-key
   components (system prompt, user context, system context, tool-use context,
   fork context messages) so child agents reuse the parent's prompt cache.
4. **Compare to shareAI-style forks** -- open-source "agent swarm" projects
   typically fork the full message list; real Claude Code shares the **prompt
   cache prefix** via `CacheSafeParams` and creates an isolated
   `ToolUseContext`, cloning only the file-state cache and replacement state.
5. **Load and parse agent definitions** -- markdown frontmatter and JSON
   schemas, resolution priority (built-in < plugin < user < project < managed),
   and the `getSystemPrompt()` closure pattern.
6. **Know the built-in agent types** -- Explore, Plan, general-purpose,
   verification, fork, and their tool/permission constraints.
7. **Record sidechain transcripts** -- how each subagent's messages are
   persisted with `recordSidechainTranscript()` for resume and debugging.

## Source Files

| File | Role |
|------|------|
| `src/tools/AgentTool/AgentTool.tsx` | Tool definition: schema, permission check, sync/async dispatch |
| `src/tools/AgentTool/runAgent.ts` | Core orchestration: context build, query loop, transcript recording |
| `src/utils/forkedAgent.ts` | `CacheSafeParams` type, `createSubagentContext()`, `runForkedAgent()` |
| `src/tools/AgentTool/loadAgentsDir.ts` | Parse agents from `.claude/agents/*.md` and JSON settings |
| `src/tools/AgentTool/forkSubagent.ts` | Fork experiment: `FORK_AGENT`, `buildForkedMessages()` |
| `src/tools/AgentTool/agentToolUtils.ts` | Tool filtering, result finalization, async lifecycle driver |
| `src/tools/AgentTool/built-in/*.ts` | Explore, Plan, general-purpose, verification definitions |

## Key Concept: CacheSafeParams vs. Message-List Forking

### shareAI-lab approach (message-list fork)

Open-source multi-agent frameworks typically clone the entire conversation:

```
parent_messages = [msg1, msg2, ..., msgN]
child_messages  = parent_messages.copy() + [new_user_prompt]
```

Every child re-sends the full prefix to the API. If the prefix is 100k tokens,
each child pays 100k input tokens from scratch -- no cache sharing.

### Real Claude Code approach (CacheSafeParams)

Claude Code threads the five components that form the Anthropic API cache key:

```typescript
type CacheSafeParams = {
  systemPrompt:        SystemPrompt       // identical bytes
  userContext:         { [k: string]: string }
  systemContext:       { [k: string]: string }
  toolUseContext:      ToolUseContext      // same tools, model, thinking config
  forkContextMessages: Message[]          // parent prefix messages
}
```

Because the child's API request starts with byte-identical system prompt, tools,
and message prefix, the Anthropic API returns a **cache read** instead of
re-processing. Fork children additionally use `useExactTools: true` to inherit
the parent's exact tool pool and thinking config, making the prefix
byte-identical.

The child gets an **isolated `ToolUseContext`** via `createSubagentContext()` --
cloned file-state cache, fresh abort controller linked to the parent, no-op
`setAppState` (unless explicitly shared), and its own `agentId`.

## Session Files

| File | Description |
|------|-------------|
| `README.md` | This overview |
| `SOURCE_ANALYSIS.md` | Annotated deep-dive into every source file |
| `reimplementation.py` | Runnable Python reimplementation (~250 LOC) |
