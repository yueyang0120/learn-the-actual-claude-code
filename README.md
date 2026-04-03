[English](./README.md) | [中文](./README-zh.md)

# Learn the Actual Claude Code

How does a production AI coding agent actually work? This repository answers that question by tracing Claude Code's architecture through its real TypeScript source code, then rebuilding each subsystem in Python.

Fourteen chapters. Each one takes a subsystem — the agent loop, the tool dispatcher, the permission engine — explains the engineering problem it solves, walks through the actual implementation with file paths and line references, and provides a runnable Python version.

## Quick Start

```bash
git clone https://github.com/yueyang0120/learn-the-actual-claude-code.git
cd learn-the-actual-claude-code
pip install -r requirements.txt
cp .env.example .env  # add your ANTHROPIC_API_KEY

python agents/s01_agent_loop.py       # run any chapter
python agents/s_full.py               # run the combined agent
```

## Curriculum

### Foundation: The Core Engine

| # | Chapter | What It Covers |
|---|---------|----------------|
| 01 | [The Agent Loop](docs/en/01-the-agent-loop.md) | The streaming generator at the heart of Claude Code — why it yields events instead of returning strings, how the state machine drives continuation, and how tools execute during model streaming |
| 02 | [The Tool System](docs/en/02-tool-system.md) | The `Tool` interface with 30+ fields, behavioral flags that depend on input, feature-gated registration, and why the tool pool is sorted for cache stability |
| 03 | [Tool Orchestration](docs/en/03-tool-orchestration.md) | How multiple tool calls get partitioned into concurrent and serial batches, bounded parallelism, and the 13-step per-tool execution pipeline |
| 04 | [The System Prompt](docs/en/04-system-prompt.md) | Static vs. dynamic prompt sections, the cache boundary marker, the CLAUDE.md hierarchy, and how ~100 prompt segments get assembled into one API call |
| 05 | [Permissions](docs/en/05-permissions.md) | Rule-based access control with 4 sources, pattern matching, a bash command classifier, and a denial circuit breaker |

### Scaling Up

| # | Chapter | What It Covers |
|---|---------|----------------|
| 06 | [Context Compaction](docs/en/06-context-compaction.md) | Four-layer compaction cascade — micro-compact, session memory, LLM summarization, manual — with thresholds, progressive warnings, and a circuit breaker |
| 07 | [Skills](docs/en/07-skills.md) | Two-layer loading: ~100-token summaries in the prompt, full bodies on demand. Frontmatter parsing, budget-aware listing, conditional activation |
| 08 | [Subagents](docs/en/08-subagents.md) | Forking isolated agents that share the parent's prompt cache. CacheSafeParams, sidechain transcripts, built-in agent types |
| 09 | [The Task System](docs/en/09-task-system.md) | Seven task types with a DAG dependency graph, append-only disk output, offset-based streaming reads, and background execution |
| 10 | [Hooks](docs/en/10-hooks.md) | 27 event types, shell-based extensibility, structured JSON I/O, matcher patterns, and session-scoped hook lifecycle |

### Production Patterns

| # | Chapter | What It Covers |
|---|---------|----------------|
| 11 | [MCP Integration](docs/en/11-mcp.md) | Three transports, tool name namespacing, output truncation, prompt-to-skill conversion, and connection lifecycle management |
| 12 | [State Management](docs/en/12-state-management.md) | A 35-line Zustand-like store, ~100-field AppState, a side-effect reactor, message normalization pipeline, and the bootstrap/runtime state split |
| 13 | [Teams and Swarms](docs/en/13-teams.md) | Backend-agnostic teammate execution, file-based mailbox IPC, coordinator mode, and the shutdown protocol |
| 14 | [Worktree Isolation](docs/en/14-worktrees.md) | Git worktree creation with slug validation, settings propagation, change detection, and safe cleanup |

## Architecture

```
+-----------------------------------------------+
|            cli.tsx (bootstrap)                 |
|  version check → feature gates → fast path    |
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
| partition → concurrent /     |
| serial → hooks → results     |
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

## Project Structure

```
docs/en/           Chapter text (English)
docs/zh/           Chapter text (中文)
agents/            Runnable Python reimplementations (one per chapter + combined)
architecture/      System architecture reference and source file map
skills/            Example skill definitions
lib/               Shared Python library (types, utilities)
tests/             Smoke tests
```

## Prerequisites

- Python 3.10+
- An Anthropic API key
- Basic familiarity with LLMs and tool use

## License

MIT — see [SOURCES.md](SOURCES.md) for source attribution and credits.
