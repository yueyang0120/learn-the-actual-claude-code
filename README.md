# Learn the Actual Claude Code

> **The other repo guesses. We show you the actual code.**

A 14-session course teaching Claude Code's harness engineering from the **real source code** — not reverse-engineered guesses, but annotated deep-dives into the actual TypeScript implementation with simplified Python reimplementations.

## Why This Exists

[shareAI-lab/learn-claude-code](https://github.com/shareAI-lab/learn-claude-code) is a valuable 12-session course (46k+ stars) that teaches agent harness engineering. But it reverse-engineers Claude Code from the outside — inferring architecture from observable behavior.

We don't have to guess. The [source code is available](https://github.com/AprilNEA/claude-code-source), and it reveals an architecture far more sophisticated than external observation suggests.

### What You Learn Here vs. There

| Concept | shareAI-lab (inferred) | Here (from actual source) |
|---------|----------------------|---------------------------|
| Agent loop | Simple `while` loop | Generator-based streaming pipeline with `QueryEngine` orchestration |
| Tool system | `dict` of handlers | 15+ field `Tool` interface with feature gates, schema validation, `ToolUseContext` |
| Tool dispatch | Sequential execution | Read-only concurrent / write serial partitioning with max concurrency = 10 |
| System prompt | One-line string | Cached/uncached sections, CLAUDE.md hierarchy, memory attachment, tool prompt deferral |
| Permissions | Not covered | Rule-based system with 4 sources, pattern matching, bash classifier, denial tracking |
| Compaction | 3 simplified layers | Threshold from context window minus 13,000 buffer, circuit breaker (max 3 failures) |
| Skills | Load full body upfront | Two-layer: ~100 tokens summary in prompt, ~2000 tokens body on demand only |
| Subagents | Fork message list | `CacheSafeParams` for prompt cache sharing, sidechain transcripts, agent definitions |
| Tasks | Simple dependency graph | 6 task types, lifecycle with kill dispatch, output streaming from disk |
| Hooks | Not covered | 8 event types, shell execution, structured JSON I/O |
| MCP | Not covered | 3 transports, tools + resources + prompts integration |
| State mgmt | Not covered | Zustand store, 8+ message types, immutable/mutable partitions |
| Teams | Mailbox messaging | Backend-agnostic swarm (tmux/iTerm/in-process), coordinator mode |
| Worktrees | Basic git worktree | Slug validation, settings symlinks, hook integration |

## Each Session Delivers Two Artifacts

1. **`SOURCE_ANALYSIS.md`** — Annotated walkthrough of the real TypeScript source with file paths, line references, and design rationale
2. **`reimplementation.py`** — Simplified, runnable Python reimplementation grounded in the actual architecture

## 14-Session Curriculum

### Foundation (Sessions 01-05): The Core Engine

| # | Session | Real Source Files | Key Insight |
|---|---------|-------------------|-------------|
| 01 | [Bootstrap & Agent Loop](sessions/s01-bootstrap-and-agent-loop/) | `cli.tsx`, `main.tsx`, `QueryEngine.ts`, `query.ts` | The loop is a streaming generator, not a while loop |
| 02 | [Tool Interface & Registration](sessions/s02-tool-interface-and-registration/) | `Tool.ts` (792 LOC), `tools.ts` (389 LOC) | 15+ fields per tool, feature-gated conditional registration |
| 03 | [Tool Orchestration](sessions/s03-tool-orchestration/) | `toolOrchestration.ts`, `toolExecution.ts` | Read-only tools run concurrently; write tools run serially |
| 04 | [System Prompt Construction](sessions/s04-system-prompt-construction/) | `prompts.ts` (914 LOC), `systemPromptSections.ts` | Cached/uncached sections with CLAUDE.md memory hierarchy |
| 05 | [Permission System](sessions/s05-permission-system/) | `permissions.ts` (1,486 LOC), `PermissionRule.ts` | Rule sources, pattern matching, bash command classification |

### Intermediate (Sessions 06-10): Scaling Up

| # | Session | Real Source Files | Key Insight |
|---|---------|-------------------|-------------|
| 06 | [Context Compaction](sessions/s06-context-compaction/) | `autoCompact.ts` (351 LOC), `compact.ts`, `microCompact.ts` | Threshold = context window - 13,000; circuit breaker after 3 failures |
| 07 | [Skills: Two-Layer Loading](sessions/s07-skills-two-layer-loading/) | `loadSkillsDir.ts`, `SkillTool.ts` (1,100 LOC) | Summary tokens in prompt, full body only on invocation |
| 08 | [Subagents](sessions/s08-subagents/) | `AgentTool.tsx`, `runAgent.ts`, `forkSubagent.ts` | Cache-safe prompt sharing, sidechain transcript recording |
| 09 | [Task System](sessions/s09-task-system/) | `Task.ts`, `TaskCreate/Update/List/Get/Output/Stop` tools | 6 task types, dependency DAG, output streaming |
| 10 | [Hooks](sessions/s10-hooks/) | `hooks.ts`, `hookHelpers.ts`, `hooksConfigManager.ts` | 8 event types, shell execution, structured JSON I/O |

### Advanced (Sessions 11-14): Production Patterns

| # | Session | Real Source Files | Key Insight |
|---|---------|-------------------|-------------|
| 11 | [MCP Integration](sessions/s11-mcp-integration/) | `mcp/client.ts`, `MCPTool`, `ListMcpResourcesTool` | 3 transports, tools + resources + prompts as first-class |
| 12 | [State Management](sessions/s12-state-management/) | `AppState.tsx`, `AppStateStore.ts`, message types | Zustand store, 8+ message types, immutable/mutable partitions |
| 13 | [Teams & Swarms](sessions/s13-teams-and-swarms/) | `swarm/*`, `coordinatorMode.ts`, `SendMessageTool` | Backend-agnostic swarm, coordinator delegation, permission bridging |
| 14 | [Worktree Isolation](sessions/s14-worktree-isolation/) | `worktree.ts`, `EnterWorktreeTool`, `ExitWorktreeTool` | Slug validation, settings symlinks, hook integration |

## Architecture Overview

```
                         ┌──────────────────────────────────────────────┐
                         │              cli.tsx (bootstrap)             │
                         │  version check → feature gates → fast path  │
                         └───────────────────┬──────────────────────────┘
                                             │
                         ┌───────────────────▼──────────────────────────┐
                         │             main.tsx (full init)             │
                         │  config, auth, tools, MCP, plugins, skills  │
                         └───────────────────┬──────────────────────────┘
                                             │
              ┌──────────────────────────────▼──────────────────────────────┐
              │                    QueryEngine.ts                           │
              │  orchestrates: query loop, tool exec, compaction, sessions  │
              └──────┬────────────┬───────────┬──────────┬─────────────────┘
                     │            │           │          │
           ┌─────────▼──┐  ┌─────▼────┐  ┌───▼───┐  ┌──▼──────────┐
           │   query.ts  │  │ compact/ │  │ state │  │ permissions │
           │ agent loop  │  │ auto/    │  │ store │  │ rules/      │
           │ streaming   │  │ micro/   │  │ msg   │  │ classifier  │
           │ generator   │  │ session  │  │ types │  │ patterns    │
           └──────┬──────┘  └──────────┘  └───────┘  └─────────────┘
                  │
     ┌────────────▼──────────────┐
     │    toolOrchestration.ts   │
     │ partition → concurrent /  │
     │ serial → hooks → results  │
     └────────────┬──────────────┘
                  │
    ┌─────────────▼──────────────────────────────────────────┐
    │                     Tool Registry                       │
    │  ┌──────┐ ┌──────┐ ┌───────┐ ┌──────┐ ┌─────────────┐ │
    │  │ Bash │ │ Read │ │ Agent │ │ Skill│ │ MCP Tools   │ │
    │  │ Edit │ │ Write│ │ Task* │ │ Hooks│ │ (dynamic)   │ │
    │  │ Glob │ │ Grep │ │ Plan  │ │ LSP  │ │ 3 transports│ │
    │  └──────┘ └──────┘ └───────┘ └──────┘ └─────────────┘ │
    └────────────────────────────────────────────────────────┘
```

## Quick Start

```bash
git clone https://github.com/yueyang0120/learn-the-actual-claude-code.git
cd learn-the-actual-claude-code
pip install -r requirements.txt
cp .env.example .env  # Add your ANTHROPIC_API_KEY

# Run any session
python sessions/s01-bootstrap-and-agent-loop/reimplementation.py

# Run the complete agent (all sessions combined)
python full_agent.py
```

## Prerequisites

- Python 3.10+
- An Anthropic API key
- Basic understanding of LLMs and tool use

## References & Sources

### Primary Source: Claude Code Leaked Source

All source analysis in this repository references the **Claude Code v2.1.88** TypeScript source code from:

- **Repository**: [AprilNEA/claude-code-source](https://github.com/AprilNEA/claude-code-source)
- **How it was obtained**: The source was recovered by extracting and decompiling the source map file (`cli.js.map`) bundled inside the `@anthropic-ai/claude-code` npm package. This is **not** an official Anthropic release.
- **Original package**: `@anthropic-ai/claude-code` v2.1.88
- **Language**: 100% TypeScript, built with Bun, UI via React/Ink

Key source files referenced throughout this curriculum (total ~6,000+ LOC across core modules):

| File | Lines | Role | Sessions |
|------|-------|------|----------|
| `src/QueryEngine.ts` | 1,295 | Main agent loop orchestrator | s01 |
| `src/constants/prompts.ts` | 914 | System prompt assembly | s04 |
| `src/Tool.ts` | 792 | Tool interface definitions | s02 |
| `src/tools.ts` | 389 | Tool registration with feature gates | s02 |
| `src/utils/permissions/permissions.ts` | 1,486 | Permission decision engine | s05 |
| `src/services/compact/autoCompact.ts` | 351 | Auto-compaction with thresholds | s06 |
| `src/services/tools/toolOrchestration.ts` | 188 | Concurrent/serial tool dispatch | s03 |
| `src/skills/loadSkillsDir.ts` | 400+ | Skill discovery and loading | s07 |
| `src/tools/AgentTool/runAgent.ts` | 400+ | Subagent execution | s08 |
| `src/utils/hooks.ts` | 400+ | Hook lifecycle system | s10 |

### Pedagogical Inspiration

The progressive session-based curriculum structure is inspired by:

- **Repository**: [shareAI-lab/learn-claude-code](https://github.com/shareAI-lab/learn-claude-code) (46k+ stars)
- **What they do**: 12 progressive Python sessions teaching harness engineering by reverse-engineering Claude Code's behavior from outside observation
- **What we add**: Every architectural claim in our repo is backed by specific file paths and line numbers from the actual source. We cover 5 additional subsystems they don't (permissions, system prompt, hooks, MCP, state management), and our reimplementations reflect the real architecture rather than inferences.

### Claude Code Official Resources

- **Claude Code**: [Anthropic's official CLI tool](https://docs.anthropic.com/en/docs/claude-code)
- **Built by**: Anthropic PBC
- **License of original**: Proprietary ("All rights reserved" by Anthropic PBC)

> **Disclaimer**: This is an educational project. The source analysis references decompiled code that was publicly distributed via npm. This repository does not redistribute the original source code — it provides annotated analysis and independent Python reimplementations for learning purposes.

## Acknowledgments

- [AprilNEA](https://github.com/AprilNEA) for recovering and publishing the Claude Code source
- [shareAI-lab](https://github.com/shareAI-lab) for pioneering the session-based pedagogical approach
- [Anthropic](https://anthropic.com) for building Claude Code

## License

MIT
