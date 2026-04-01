# Session 11: MCP Integration

## Overview

The Model Context Protocol (MCP) is Claude Code's extensibility backbone. It lets
external servers expose **tools**, **resources**, and **prompts** that Claude Code
consumes as first-class citizens. This session dissects the full lifecycle: how
transport connections are established, how remote tools become callable tools in
the agent loop, how resources are listed and read, and how MCP prompts are
converted into Claude Code skills.

## Key Source Files

| File | Purpose |
|------|---------|
| `src/services/mcp/client.ts` | Central MCP client -- connects transports, discovers tools/resources/prompts, wraps them as Tool objects |
| `src/services/mcp/types.ts` | Config schemas for stdio, SSE, HTTP, WebSocket, SDK transport types |
| `src/services/mcp/config.ts` | Loads MCP server configs from .mcp.json, settings, plugins |
| `src/services/mcp/mcpStringUtils.ts` | Name parsing: `mcp__server__tool` format, display name extraction |
| `src/services/mcp/normalization.ts` | Normalizes names for MCP (slugification) |
| `src/tools/MCPTool/MCPTool.ts` | Template Tool definition -- overridden per-server by client.ts |
| `src/utils/mcpWebSocketTransport.ts` | Custom WebSocket transport (Bun + Node compat) |
| `src/utils/mcpValidation.ts` | Token-aware output truncation for MCP tool results |
| `src/utils/mcpOutputStorage.ts` | Persists large/binary MCP results to disk |
| `src/utils/plugins/mcpPluginIntegration.ts` | Loads MCP servers from plugin manifests |
| `src/skills/mcpSkillBuilders.ts` | Registry for converting MCP prompts into skills |

## Architecture

```
                   .mcp.json / settings / plugins
                              |
                      +-----------------+
                      |   config.ts     |  merge & validate configs
                      +-----------------+
                              |
                    +-----------------------+
                    | MCPConnectionManager  |  React hook: connects/disconnects
                    +-----------------------+
                              |
               +--------------+--------------+
               |              |              |
         StdioTransport  SSETransport  StreamableHTTP
               |              |              |
               +----- MCP SDK Client --------+
                              |
                    listTools / listResources / listPrompts
                              |
           +------------------+------------------+
           |                  |                  |
     MCPTool clones    ListResources tool   Skills (prompts)
     (first-class)     (resource listing)    (slash commands)
```

## Learning Objectives

1. **Transport multiplexing** -- Understand how Claude Code supports stdio,
   SSE, StreamableHTTP, WebSocket, and IDE-specific transports through a
   single MCP SDK Client abstraction.

2. **Tool wrapping** -- See how MCPTool.ts is a template that gets cloned
   per discovered tool, with `name`, `description`, `prompt`, `call`, and
   `userFacingName` overridden by client.ts.

3. **Naming conventions** -- The `mcp__serverName__toolName` format is
   used internally for permission matching and deduplication, while
   `serverName - toolName (MCP)` is the user-facing display.

4. **Output management** -- MCP results can be huge. The system uses
   token-aware truncation (`mcpValidation.ts`), binary content persistence
   (`mcpOutputStorage.ts`), and structured content handling to keep context
   windows manageable.

5. **Prompt-to-skill conversion** -- MCP servers can expose prompts that
   become `/skill` slash commands in Claude Code, bridging the MCP prompt
   protocol with the internal skill system.

## Exercises

1. Trace a tool call from model output through MCPTool dispatch to the
   MCP SDK `callTool` method and back.
2. Add a second MCP server to the Python reimplementation and observe
   how tool names are namespaced.
3. Implement resource template expansion (the real source supports URI
   templates for parameterized resources).
4. Compare the truncation strategy in `mcpValidation.ts` with the
   compaction strategy from Session 06.

## Running the Reimplementation

```bash
cd sessions/s11-mcp-integration
python reimplementation.py
```

The Python stub simulates the full MCP client lifecycle with an in-process
mock server -- no external MCP server required.
