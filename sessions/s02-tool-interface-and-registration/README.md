# Session 02: Tool Interface & Registration

## Learning Objectives

By the end of this session you will understand:

1. **The `Tool` type** -- the 30+ field interface that every tool in Claude Code must satisfy, and why each field exists.
2. **`ToolUseContext`** -- the massive context object threaded through every tool call (abort control, file cache, app state, permissions, MCP clients, agent IDs, and more).
3. **`buildTool` and `ToolDef`** -- how Claude Code supplies fail-closed defaults so tool authors only define what they need.
4. **Feature-gated registration** -- how `getAllBaseTools()` in `tools.ts` conditionally loads tools using `feature()` flags (Bun dead-code elimination), `process.env` checks, and runtime helpers.
5. **MCP integration** -- how external tools are merged alongside built-in tools, deduplicated, sorted for prompt-cache stability, and filtered by deny rules.

## Real Source Files Covered

| File | Lines | Role |
|------|-------|------|
| `src/Tool.ts` | 792 | Tool interface, ToolUseContext, ValidationResult, ToolResult, buildTool |
| `src/tools.ts` | 389 | getAllBaseTools(), getTools(), assembleToolPool(), feature gates |
| `src/tools/FileReadTool/FileReadTool.ts` | 1,184 | Example tool: read files, images, PDFs, notebooks |
| `src/tools/BashTool/BashTool.tsx` | 900+ | Example tool: shell execution with security analysis |

## What shareAI-lab Gets Wrong

The [shareAI-lab/learn-claude-code](https://github.com/shareAI-lab/learn-claude-code) course models tools as a **simple dictionary of handler functions** -- `{"bash": bash_handler, "read": read_handler}`. That misses nearly everything that makes Claude Code's tool system production-grade:

| Aspect | shareAI-lab (inferred) | Actual source |
|--------|----------------------|---------------|
| Tool shape | `dict[str, Callable]` | 30+ field `Tool` generic interface with typed Input/Output/Progress |
| Registration | Static list | `getAllBaseTools()` with `feature()` gates, env checks, lazy `require()` |
| Schema | Passed to API call | Per-tool `inputSchema` (Zod) + optional `inputJSONSchema` (raw JSON Schema for MCP) |
| Validation | Not present | `validateInput()` runs before permission check, returns structured errors with codes |
| Permissions | Not covered | `checkPermissions()` per tool, plus `preparePermissionMatcher()` for hook pattern matching |
| Concurrency | Sequential | `isConcurrencySafe(input)` determines if a tool can run in parallel |
| Read-only | Not tracked | `isReadOnly(input)` -- drives concurrent/serial partitioning |
| Defaults | None | `buildTool()` supplies 7 fail-closed defaults so tools only override what they need |
| Context | Bare cwd | `ToolUseContext` with ~40 fields (abort, file state, MCP clients, agent ID, permissions, etc.) |
| MCP tools | Not covered | Merged via `assembleToolPool()`, deduplicated, sorted for cache stability |

## Quick Start

```bash
# Read the deep-dive analysis
cat sessions/s02-tool-interface-and-registration/SOURCE_ANALYSIS.md

# Run the Python reimplementation
python sessions/s02-tool-interface-and-registration/reimplementation.py
```

## Files in This Session

- **`README.md`** -- This overview
- **`SOURCE_ANALYSIS.md`** -- Annotated walkthrough of Tool.ts and tools.ts with code snippets
- **`reimplementation.py`** -- Runnable Python reimplementation (~250 LOC) with Tool ABC, ToolRegistry, feature gates, and 4 example tools
