# Session 11 -- MCP Integration

s01 > s02 > s03 > s04 > s05 | s06 > s07 > s08 > s09 > s10 | **s11** > s12 > s13 > s14

> "Any sufficiently advanced tool system eventually becomes a protocol."
>
> *Harness layer: MCP is the bridge between the agent and the outside world. Claude Code multiplexes three transports, clones a template tool per remote capability, and truncates output to protect the context window.*

---

## Problem

An agent that can only call its own built-in tools has a fixed surface area. Every new integration -- GitHub, filesystem, databases -- requires shipping new code inside the agent itself.

What if external processes could **expose** tools, resources, and prompts through a standard protocol, and the agent could discover and call them at runtime?

That is exactly what MCP (Model Context Protocol) does. But wiring it up raises several questions:

1. **Transport diversity** -- Some servers run as child processes (stdio), others as remote HTTP endpoints (SSE, StreamableHTTP). How does the client handle all of them uniformly?
2. **Naming collisions** -- If two MCP servers both expose a `read` tool, how do you tell them apart?
3. **Context budget** -- MCP tools can return megabytes of data. How do you keep responses from blowing up the context window?
4. **Permissions** -- A remote MCP tool named `Write` must not accidentally match the built-in `Write` tool's permission rules.

---

## Solution

Claude Code's MCP subsystem solves this with three layers: a transport multiplexer, a template-clone tool wrapper, and output truncation.

```
                +-----------+    +-----------+    +-----------+
                |  Server A |    |  Server B |    |  Server C |
                |  (stdio)  |    |   (SSE)   |    |  (HTTP)   |
                +-----+-----+    +-----+-----+    +-----+-----+
                      |                |                |
               +------v------+  +-----v------+  +------v------+
               | StdioTransp |  | SSETransp  |  | HTTPTransp  |
               +------+------+  +-----+------+  +------+------+
                      |                |                |
               +------v----------------v----------------v------+
               |          McpConnectionManager                  |
               |   clients: { "github": McpClient,             |
               |              "filesystem": McpClient, ... }    |
               +------+-----+---------+------------------------+
                      |     |         |
                 tools[]  resources[]  skills[]
                      |     |         |
               +------v-----v---------v------------------------+
               |              Agent Tool Registry               |
               |   mcp__github__list_issues                     |
               |   mcp__github__create_issue                    |
               |   mcp__filesystem__read_file                   |
               +------------------------------------------------+
```

The naming convention `mcp__<server>__<tool>` is the key insight. It makes every MCP tool globally unique and prevents permission rules from colliding with built-in tools.

---

## How It Works

### 1. Transport Configuration

Every MCP server starts as a config object describing which transport to use. The real source defines separate Zod schemas per transport type; our reimplementation unifies them into a single dataclass:

```python
class TransportType(str, Enum):
    STDIO = "stdio"
    SSE = "sse"
    HTTP = "http"           # StreamableHTTP
    WS = "ws"               # WebSocket
    SSE_IDE = "sse-ide"     # IDE extension via SSE
    SDK = "sdk"             # In-process SDK

@dataclass
class McpServerConfig:
    name: str
    transport: TransportType = TransportType.STDIO
    command: str = ""           # stdio only
    args: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)
    url: str = ""               # SSE / HTTP / WS
    headers: dict[str, str] = field(default_factory=dict)
    scope: str = "local"        # local | user | project | dynamic
```

Source: `src/services/mcp/types.ts` -- `McpStdioServerConfigSchema`, `McpSSEServerConfigSchema`, etc.

### 2. Tool Naming Convention

Two functions handle the collision-safe naming:

```python
def normalize_name_for_mcp(name: str) -> str:
    """Real source: src/services/mcp/normalization.ts normalizeNameForMCP"""
    return re.sub(r"[^a-zA-Z0-9_]", "_", name)

def build_mcp_tool_name(server_name: str, tool_name: str) -> str:
    """mcp__<server>__<tool> format for internal identification."""
    return f"mcp__{normalize_name_for_mcp(server_name)}__{normalize_name_for_mcp(tool_name)}"
```

This means a `read` tool on server `github` becomes `mcp__github__read`, while the same tool on `filesystem` becomes `mcp__filesystem__read`. No collision.

Source: `src/services/mcp/mcpStringUtils.ts`

### 3. Template Cloning -- WrappedMcpTool

The real source has a single `MCPTool` class that gets **cloned** once per discovered tool. Each clone overrides the name, description, prompt, and call method. Our reimplementation captures this as `WrappedMcpTool`:

```python
@dataclass
class WrappedMcpTool:
    internal_name: str          # mcp__server__tool
    display_name: str           # server - tool (MCP)
    description: str
    server_name: str
    tool_name: str
    input_schema: dict[str, Any]
    _call_fn: Callable

    def call(self, arguments: dict) -> str:
        """Execute the MCP tool and apply output truncation."""
        raw_result = self._call_fn(self.tool_name, arguments)
        text_parts = []
        for block in raw_result.get("content", []):
            if block.get("type") == "text":
                text_parts.append(block["text"])
        full_text = "\n".join(text_parts)
        return truncate_mcp_content(full_text)
```

Notice how every call passes through `truncate_mcp_content` -- the context budget guard.

Source: `src/tools/MCPTool/MCPTool.ts`, `client.ts`

### 4. Output Truncation

MCP servers can return arbitrarily large responses. The truncation function uses a two-tier check: a fast character-length estimate, then (in the real source) an exact token count:

```python
MAX_MCP_OUTPUT_TOKENS = 25_000
TOKEN_THRESHOLD_FACTOR = 0.5

def truncate_mcp_content(content: str, max_tokens: int = MAX_MCP_OUTPUT_TOKENS) -> str:
    est = estimate_tokens(content)
    if est <= max_tokens * TOKEN_THRESHOLD_FACTOR:
        return content
    max_chars = max_tokens * 4
    if len(content) <= max_chars:
        return content
    return content[:max_chars] + (
        f"\n\n[OUTPUT TRUNCATED - exceeded {max_tokens} token limit]\n"
        "Use pagination or filtering to retrieve specific portions."
    )
```

The truncation message is not just informational -- it tells the model to use pagination next time.

Source: `src/utils/mcpValidation.ts`

### 5. Connection Manager

All MCP clients are managed through `McpConnectionManager`, which maintains the lifecycle and aggregates capabilities across servers:

```python
class McpConnectionManager:
    def __init__(self) -> None:
        self.clients: dict[str, McpClient] = {}

    def get_all_tools(self) -> list[WrappedMcpTool]:
        """Aggregate tools across all connected servers."""
        tools = []
        for client in self.clients.values():
            tools.extend(client.tools)
        return tools

    def get_all_resources(self) -> dict[str, list[McpResource]]:
        return {
            name: client.resources
            for name, client in self.clients.items()
        }
```

The real source is a React hook (`useManageMCPConnections`) that watches config changes and maintains client lifecycle in `AppState.mcp`.

Source: `src/services/mcp/MCPConnectionManager.tsx`

### 6. Prompts Become Skills

MCP servers can also expose **prompts** -- reusable instruction templates. Claude Code converts these into slash commands:

```python
@dataclass
class McpSkillCommand:
    name: str
    description: str
    server_name: str
    prompt_name: str
    arguments: list[dict[str, Any]]

    def format_command(self) -> str:
        return f"/{self.server_name}:{self.prompt_name}"
```

So a prompt named `review_pr` on the `github` server becomes the slash command `/github:review_pr`.

Source: `src/skills/mcpSkillBuilders.ts`

---

## What Changed

| Component | Before | After |
|-----------|--------|-------|
| Tool surface | Fixed set of built-in tools only | Dynamic: built-ins + any MCP server's tools |
| Transport | N/A | 3+ transports (stdio, SSE, HTTP) behind one client |
| Tool naming | Simple function names (`Read`, `Bash`) | Namespace-qualified: `mcp__server__tool` |
| Output safety | No external output budget | 25,000-token cap with truncation message |
| Permissions | Tool name matched directly | Fully-qualified MCP name prevents false matches |
| Resource access | Files only (via Read/Glob) | MCP resources with URI scheme (`github://...`) |
| Slash commands | Built-in skills only | MCP prompts auto-register as `/server:prompt` |

---

## Try It

```bash
# Run the MCP integration demo
python agents/s11_mcp.py
```

What to watch for in the output:

1. **Two servers connect** -- `github` (with tools + resources + prompts) and `filesystem` (with a read tool)
2. **Tool names use the `mcp__` prefix** -- e.g., `mcp__github__list_issues`
3. **Tool call result** -- the `list_issues` call returns data and it flows through truncation
4. **Truncation kicks in** -- a 200,000-character string gets trimmed to the token limit
5. **No name collision** -- `mcp__github__read` and `mcp__filesystem__read` are distinct

Try modifying the code to add a third server and observe how `get_all_tools()` seamlessly aggregates tools across all three connections.
