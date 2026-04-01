[English](./README.md) | [中文](./README-zh.md)

# Learn the Actual Claude Code

A 14-session course teaching Claude Code's harness engineering from the **actual TypeScript source code** — annotated deep-dives into the real implementation, paired with simplified Python reimplementations you can run and modify.

Documentation available in [English](./docs/en/) | [中文](./docs/zh/).

## Inspiration

This course is inspired by [shareAI-lab/learn-claude-code](https://github.com/shareAI-lab/learn-claude-code), an excellent progressive curriculum that teaches agent harness engineering through hands-on Python sessions. We follow the same session-based pedagogical approach they pioneered.

Where this course goes further: since the [Claude Code source](https://github.com/AprilNEA/claude-code-source) has been publicly recovered, we can ground every architectural claim in specific file paths and line numbers from the real TypeScript implementation. This means our reimplementations reflect the actual design patterns — streaming generators, concurrent/serial tool partitioning, prompt cache sharing, and more — rather than inferences from external behavior.

## What You'll Learn

Claude Code is a production AI coding agent built by Anthropic. This course walks you through its architecture — the agent loop, tool system, permission engine, context compaction, and more — using the real source as the ground truth.

Each session covers one subsystem. You get two artifacts:

1. **Source Analysis** — Annotated walkthrough of the real TypeScript code with file paths, line references, and design rationale
2. **Python Agent** — A simplified, runnable reimplementation grounded in the actual architecture

## Choose Your Path

**Want to build agents?** Start with the learning guides and Python code:

```
docs/en/          -- Progressive learning guides (start here)
agents/            -- Runnable Python reimplementations
```

**Want to understand Claude Code's internals?** Dive into the source analysis:

```
source-analysis/   -- Deep-dive annotated walkthroughs of the TypeScript source
```

## Quick Start

```bash
git clone https://github.com/yueyang0120/learn-the-actual-claude-code.git
cd learn-the-actual-claude-code
pip install -r requirements.txt
cp .env.example .env  # Add your ANTHROPIC_API_KEY

# Run any session
python agents/s01_agent_loop.py

# Run the complete agent (all sessions combined)
python agents/s_full.py
```

## 14-Session Curriculum

### Foundation (Sessions 01-05): The Core Engine

| # | Session | Key Insight |
|---|---------|-------------|
| 01 | [The Agent Loop](docs/en/s01-the-agent-loop.md) | The loop is a streaming generator, not a while loop |
| 02 | [Tool System](docs/en/s02-tool-system.md) | 15+ fields per tool, feature-gated conditional registration |
| 03 | [Tool Orchestration](docs/en/s03-tool-orchestration.md) | Read-only tools run concurrently; write tools run serially |
| 04 | [System Prompt](docs/en/s04-system-prompt.md) | Cached/uncached sections with CLAUDE.md memory hierarchy |
| 05 | [Permissions](docs/en/s05-permissions.md) | Rule-based system with 4 sources, pattern matching, bash classifier |

### Intermediate (Sessions 06-10): Scaling Up

| # | Session | Key Insight |
|---|---------|-------------|
| 06 | [Context Compaction](docs/en/s06-context-compaction.md) | Threshold = context window - 13,000 buffer; circuit breaker after 3 failures |
| 07 | [Skills](docs/en/s07-skills.md) | ~100 token summary in prompt, full body only on invocation |
| 08 | [Subagents](docs/en/s08-subagents.md) | Cache-safe prompt sharing, sidechain transcript recording |
| 09 | [Task System](docs/en/s09-task-system.md) | 7 task types, dependency DAG, disk-backed output streaming |
| 10 | [Hooks](docs/en/s10-hooks.md) | 27 event types, shell execution, structured JSON I/O |

### Advanced (Sessions 11-14): Production Patterns

| # | Session | Key Insight |
|---|---------|-------------|
| 11 | [MCP Integration](docs/en/s11-mcp.md) | 3 transports, tools + resources + prompts as first-class |
| 12 | [State Management](docs/en/s12-state-management.md) | Zustand-like store, 8+ message types, immutable/mutable partitions |
| 13 | [Teams & Swarms](docs/en/s13-teams.md) | Backend-agnostic swarm, coordinator delegation, permission bridging |
| 14 | [Worktree Isolation](docs/en/s14-worktrees.md) | Slug validation, settings symlinks, hook integration |

## Architecture Overview

```
+-----------------------------------------------+
|            cli.tsx (bootstrap)                 |
|  version check -> feature gates -> fast path  |
+----------------------+------------------------+
                       |
+----------------------v------------------------+
|            main.tsx (full init)                |
|  config, auth, tools, MCP, plugins, skills    |
+----------------------+------------------------+
                       |
+----------------------v-------------------------------+
|                 QueryEngine.ts                        |
|  orchestrates: query loop, tool exec, compaction      |
+------+----------+----------+-----------+-------------+
       |          |          |           |
+------v---+ +---v------+ +-v-----+ +--v-----------+
| query.ts | | compact/ | | state | | permissions  |
| agent    | | auto/    | | store | | rules/       |
| loop     | | micro/   | | msg   | | classifier   |
| stream   | | session  | | types | | patterns     |
+------+---+ +----------+ +-------+ +--------------+
       |
+------v-----------------------+
|   toolOrchestration.ts       |
| partition -> concurrent /    |
| serial -> hooks -> results   |
+------+-----------------------+
       |
+------v-----------------------------------------------+
|                   Tool Registry                       |
|                                                       |
| +------+ +------+ +-------+ +------+ +-------------+ |
| | Bash | | Read | | Agent | | Skill| | MCP Tools   | |
| | Edit | | Write| | Task* | | Hooks| | (dynamic)   | |
| | Glob | | Grep | | Plan  | | LSP  | | 3 transports| |
| +------+ +------+ +-------+ +------+ +-------------+ |
+-------------------------------------------------------+
```

## Prerequisites

- Python 3.10+
- An Anthropic API key
- Basic understanding of LLMs and tool use

## Project Structure

```
learn-the-actual-claude-code/
  agents/            Python reimplementations (one per session + combined)
  docs/en/           Progressive learning guides (English)
  docs/zh/           Progressive learning guides (中文)
  source-analysis/   Annotated TypeScript source walkthroughs
  skills/            Example skill definitions
  architecture/      System architecture reference
  lib/               Shared Python library (types, utilities)
  tests/             Smoke tests
```

## References & Sources

### Primary Source

All source analysis references **Claude Code v2.1.88** TypeScript source code from [AprilNEA/claude-code-source](https://github.com/AprilNEA/claude-code-source), recovered by extracting the source map file (`cli.js.map`) from the `@anthropic-ai/claude-code` npm package.

Key source files referenced (total ~6,000+ LOC across core modules):

| File | Lines | Role |
|------|-------|------|
| `src/QueryEngine.ts` | 1,295 | Main agent loop orchestrator |
| `src/constants/prompts.ts` | 914 | System prompt assembly |
| `src/Tool.ts` | 792 | Tool interface definitions |
| `src/tools.ts` | 389 | Tool registration with feature gates |
| `src/utils/permissions/permissions.ts` | 1,486 | Permission decision engine |
| `src/services/compact/autoCompact.ts` | 351 | Auto-compaction with thresholds |
| `src/services/tools/toolOrchestration.ts` | 188 | Concurrent/serial tool dispatch |

### Claude Code Official Resources

- [Claude Code](https://docs.anthropic.com/en/docs/claude-code) — Anthropic's official CLI tool
- Built by Anthropic PBC (proprietary, all rights reserved)

> **Disclaimer**: This is an educational project. The source analysis references decompiled code publicly distributed via npm. This repository does not redistribute the original source code — it provides annotated analysis and independent Python reimplementations for learning purposes.

## Acknowledgments

- [AprilNEA](https://github.com/AprilNEA) for recovering and publishing the Claude Code source
- [shareAI-lab/learn-claude-code](https://github.com/shareAI-lab/learn-claude-code) for pioneering the progressive session-based approach to teaching agent engineering
- [Anthropic](https://anthropic.com) for building Claude Code

## License

MIT
