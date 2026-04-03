# Agent Reimplementations

Each Python file in this directory reimplements one chapter's subsystem as a runnable agent. The files are independent — each can be run standalone — but they follow a progressive arc where later chapters build on concepts from earlier ones.

## Running

```bash
pip install -r ../requirements.txt
cp ../.env.example ../.env  # add your ANTHROPIC_API_KEY

python s01_agent_loop.py     # any individual chapter
python s_full.py             # combined agent with all subsystems
```

## Chapter Map

| File | Chapter | What It Adds |
|------|---------|-------------|
| `s01_agent_loop.py` | 01 — The Agent Loop | Generator-based query loop, QueryEngine, bootstrap fast path |
| `s02_tool_system.py` | 02 — The Tool System | Tool ABC with 30+ fields, ToolRegistry, feature-gated registration |
| `s03_tool_orchestration.py` | 03 — Tool Orchestration | `partition_tool_calls()`, concurrent/serial batching, bounded parallelism |
| `s04_system_prompt.py` | 04 — The System Prompt | Cached/uncached sections, CLAUDE.md hierarchy, MEMORY.md truncation |
| `s05_permissions.py` | 05 — Permissions | Rule parsing, permission modes, bash classifier, denial circuit breaker |
| `s06_context_compaction.py` | 06 — Context Compaction | Four-layer compaction (micro, session, LLM, manual), thresholds, warnings |
| `s07_skills.py` | 07 — Skills | Two-layer loading, frontmatter parsing, budget-aware prompt injection |
| `s08_subagents.py` | 08 — Subagents | CacheSafeParams, context isolation, sidechain recording, built-in types |
| `s09_task_system.py` | 09 — The Task System | Task DAG, disk output, lifecycle state machine, background execution |
| `s10_hooks.py` | 10 — Hooks | 27 event types, shell execution, JSON I/O, session-scoped hooks |
| `s11_mcp.py` | 11 — MCP Integration | Transport abstraction, tool wrapping, output truncation, connection management |
| `s12_state_management.py` | 12 — State Management | Store primitive, AppState, message normalization, side-effect reactor |
| `s13_teams.py` | 13 — Teams and Swarms | Backend abstraction, file mailbox, coordinator mode, teammate lifecycle |
| `s14_worktrees.py` | 14 — Worktree Isolation | Slug validation, git worktree creation, settings propagation, safe cleanup |
| `s_full.py` | All | Combined agent with tool registry, permissions, compaction, and prompt assembly |

## Shared Library

The `lib/` directory provides shared types and utilities used across all agents:

- `lib/types.py` — Message, Tool, Task, and Permission data types
- `lib/utils.py` — API key loading, token estimation, constants from the real source
