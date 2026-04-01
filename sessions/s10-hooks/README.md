# Session 10: The Hooks System

## Overview

Claude Code's hook system is a powerful extensibility layer that lets users (and skills)
inject arbitrary shell commands, LLM prompts, HTTP calls, or agent verifiers at
well-defined lifecycle points. Hooks can **observe** events (logging, analytics),
**gate** tool execution (approve/deny/modify), and **feed context** back into the
conversation -- all without modifying Claude Code's core source.

This session dissects the real implementation across ~5,000 lines of TypeScript.

---

## Learning Objectives

| # | Objective |
|---|-----------|
| 1 | Enumerate every hook event type and understand when each fires |
| 2 | Understand how hooks are **defined** in `.claude/settings.json` with matchers |
| 3 | Trace the execution path through `executePreToolHooks()` / `executePostToolHooks()` |
| 4 | Master the structured JSON I/O protocol (input schema, output schema, exit codes) |
| 5 | Learn how hook responses control flow: allow, deny, modify input, inject context |
| 6 | Understand `registerFrontmatterHooks()` for skill-scoped hooks |
| 7 | Explore session hooks (`sessionHooks.ts`) for ephemeral per-session state |
| 8 | Study `AsyncHookRegistry` for non-blocking background hooks |
| 9 | Recognize the four hook **types**: `command`, `prompt`, `agent`, `http` |

---

## Key Source Files

| File | Purpose |
|------|---------|
| `src/utils/hooks.ts` | Main engine: ~5k LOC, executeHooks(), executePreToolHooks(), matching, shell spawning |
| `src/types/hooks.ts` | Type definitions: HookResult, AggregatedHookResult, HookCallback, HookJSONOutput schemas |
| `src/schemas/hooks.ts` | Zod schemas: HookCommandSchema (discriminated union), HookMatcherSchema, HooksSchema |
| `src/utils/hooks/hookHelpers.ts` | Shared helpers: argument substitution, structured output enforcement |
| `src/utils/hooks/hooksConfigManager.ts` | Event metadata, grouping hooks by event+matcher for config UI |
| `src/utils/hooks/sessionHooks.ts` | Session-scoped hooks: addSessionHook(), addFunctionHook(), FunctionHook type |
| `src/utils/hooks/registerFrontmatterHooks.ts` | Skill/agent frontmatter hook registration into session scope |
| `src/utils/hooks/AsyncHookRegistry.ts` | Background hook registry: register, poll, finalize async hooks |
| `src/entrypoints/sdk/coreTypes.ts` | HOOK_EVENTS constant (27 event types) |

---

## What shareAI-lab (and Most Clones) Miss

The hook system is **completely absent** from open-source replicas. This is significant
because hooks are the primary way enterprises and power users customize Claude Code
behavior without forking the codebase:

1. **No hook event model** -- No concept of lifecycle events like PreToolUse/PostToolUse.
   Clones typically hardcode any interception logic directly in tool execution.

2. **No external command integration** -- No ability to shell out to user scripts that
   can approve/deny/modify tool calls. Everything is baked into the source.

3. **No JSON I/O protocol** -- No structured communication between the AI agent and
   external processes. The exit-code-based control flow (0=success, 2=blocking error)
   is entirely absent.

4. **No matcher system** -- No pattern matching to selectively fire hooks based on tool
   name, notification type, or other event-specific fields.

5. **No session-scoped hooks** -- No way for skills or agents to register temporary
   hooks that live only for their execution scope and get cleaned up automatically.

6. **No async hook support** -- No background hook execution with polling. All
   processing is synchronous and blocking.

7. **No four-type hook taxonomy** -- The distinction between `command` (shell),
   `prompt` (LLM evaluation), `agent` (agentic verifier), and `http` (webhook)
   hooks does not exist.

8. **No `if` condition filtering** -- No pre-spawn permission-rule-syntax filter
   to skip irrelevant hooks without incurring process spawn overhead.

This means enterprises cannot:
- Enforce custom security policies via PreToolUse hooks
- Log tool usage to external systems via PostToolUse hooks
- Auto-approve safe operations with PermissionRequest hooks
- Run verification agents after task completion via Stop hooks
- Inject dynamic context via SessionStart hooks

---

## Session Files

| File | Description |
|------|-------------|
| `README.md` | This overview |
| `SOURCE_ANALYSIS.md` | Deep annotated walkthrough of the real implementation |
| `reimplementation.py` | Runnable Python reimplementation (~250 LOC) |

---

## Prerequisites

- Session 2 (Tool Interface) -- hooks wrap tool execution
- Session 5 (Permission System) -- hooks integrate with permission decisions
- Session 7 (Skills) -- skills register their own hooks via frontmatter
- Session 8 (Subagents) -- SubagentStart/SubagentStop hook events
