# Source Analysis

This directory contains annotated walkthroughs of the **actual Claude Code TypeScript source code** (v2.1.88).

Each file covers one subsystem of the Claude Code architecture, with:

- **File paths and line references** into the real source
- **Design rationale** explaining *why* the code is structured the way it is
- **Key data structures and algorithms** that power each feature
- **Architecture diagrams** showing how components connect

## Source

The analysis references the Claude Code source recovered from the `@anthropic-ai/claude-code` npm package by [AprilNEA/claude-code-source](https://github.com/AprilNEA/claude-code-source). This repository does not redistribute that source — it provides annotated analysis for educational purposes.

## Sessions

| # | Topic | Primary Source Files |
|---|-------|---------------------|
| 01 | [Bootstrap & Agent Loop](01-bootstrap-and-agent-loop.md) | `cli.tsx`, `main.tsx`, `QueryEngine.ts`, `query.ts` |
| 02 | [Tool Interface](02-tool-interface.md) | `Tool.ts` (792 LOC), `tools.ts` (389 LOC) |
| 03 | [Tool Orchestration](03-tool-orchestration.md) | `toolOrchestration.ts`, `toolExecution.ts` |
| 04 | [System Prompt](04-system-prompt.md) | `prompts.ts` (914 LOC), `systemPromptSections.ts` |
| 05 | [Permissions](05-permissions.md) | `permissions.ts` (1,486 LOC), `PermissionRule.ts` |
| 06 | [Context Compaction](06-context-compaction.md) | `autoCompact.ts` (351 LOC), `compact.ts`, `microCompact.ts` |
| 07 | [Skills](07-skills.md) | `loadSkillsDir.ts`, `SkillTool.ts` (1,100 LOC) |
| 08 | [Subagents](08-subagents.md) | `AgentTool.tsx`, `runAgent.ts`, `forkSubagent.ts` |
| 09 | [Task System](09-task-system.md) | `Task.ts`, `TaskCreate/Update/List/Get/Output/Stop` |
| 10 | [Hooks](10-hooks.md) | `hooks.ts`, `hookHelpers.ts`, `hooksConfigManager.ts` |
| 11 | [MCP Integration](11-mcp.md) | `mcp/client.ts`, `MCPTool`, `ListMcpResourcesTool` |
| 12 | [State Management](12-state-management.md) | `AppState.tsx`, `AppStateStore.ts`, message types |
| 13 | [Teams & Swarms](13-teams.md) | `swarm/*`, `coordinatorMode.ts`, `SendMessageTool` |
| 14 | [Worktree Isolation](14-worktrees.md) | `worktree.ts`, `EnterWorktreeTool`, `ExitWorktreeTool` |

## How to Read

You can read these in order (each builds on the previous) or jump to any topic that interests you. If you want the corresponding Python reimplementation, check the matching file in [`agents/`](../agents/).
