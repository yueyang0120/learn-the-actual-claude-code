# s11: MCP Integration

s01 > s02 > s03 > s04 > s05 | s06 > s07 > s08 > s09 > s10 | **[ s11 ]** s12 > s13 > s14

> "Any sufficiently advanced tool system eventually becomes a protocol."

## Problem

An agent with only built-in tools has a fixed surface area. Every new integration -- GitHub, databases, filesystem servers -- requires shipping new code inside the agent. MCP (Model Context Protocol) lets external processes expose tools, resources, and prompts through a standard protocol that the agent discovers at runtime.

## Solution

Claude Code multiplexes three transports behind one connection manager, namespaces every tool to prevent collisions, and truncates output to protect the context window.

```
  +----------+    +----------+    +----------+
  | Server A |    | Server B |    | Server C |
  |  (stdio) |    |  (SSE)   |    |  (HTTP)  |
  +----+-----+    +----+-----+    +----+-----+
       |               |               |
  +----v---------------v---------------v----+
  |        McpConnectionManager             |
  |   clients: { "github": ...,             |
  |              "filesystem": ... }        |
  +----+----------+----------+--------------+
       |          |          |
    tools[]   resources[]  skills[]
       |          |          |
  +----v----------v----------v--------------+
  |          Agent Tool Registry            |
  |   mcp__github__list_issues              |
  |   mcp__filesystem__read_file            |
  +------------------------------------------+
```

The naming convention `mcp__<server>__<tool>` makes every MCP tool globally unique.

## How It Works

### 1. Transport config

Each server declares its transport type. The connection manager picks the right client.

```python
# agents/s11_mcp.py (simplified)

class TransportType(str, Enum):
    STDIO = "stdio"
    SSE   = "sse"
    HTTP  = "http"

@dataclass
class McpServerConfig:
    name: str
    transport: TransportType = TransportType.STDIO
    command: str = ""        # stdio only
    url: str = ""            # SSE / HTTP
```

### 2. Collision-safe naming

Two functions build the namespace-qualified tool name. A `read` tool on server `github` becomes `mcp__github__read`; the same tool on `filesystem` becomes `mcp__filesystem__read`.

```python
def normalize_name_for_mcp(name: str) -> str:
    return re.sub(r"[^a-zA-Z0-9_]", "_", name)

def build_mcp_tool_name(server_name: str, tool_name: str) -> str:
    return f"mcp__{normalize_name_for_mcp(server_name)}__{normalize_name_for_mcp(tool_name)}"
```

### 3. Template cloning

For each tool discovered on a server, the client clones a wrapper that routes calls and applies output truncation.

```python
@dataclass
class WrappedMcpTool:
    internal_name: str        # mcp__server__tool
    server_name: str
    tool_name: str
    _call_fn: Callable

    def call(self, arguments: dict) -> str:
        raw = self._call_fn(self.tool_name, arguments)
        text = "\n".join(
            b["text"] for b in raw.get("content", [])
            if b.get("type") == "text"
        )
        return truncate_mcp_content(text)
```

### 4. Output truncation

MCP servers can return megabytes. A two-tier check caps output at 25,000 tokens and tells the model to paginate next time.

```python
MAX_MCP_OUTPUT_TOKENS = 25_000

def truncate_mcp_content(content: str, max_tokens=MAX_MCP_OUTPUT_TOKENS):
    if estimate_tokens(content) <= max_tokens * 0.5:
        return content
    max_chars = max_tokens * 4
    if len(content) <= max_chars:
        return content
    return content[:max_chars] + "\n\n[OUTPUT TRUNCATED]"
```

### 5. Prompts become slash commands

MCP servers can also expose prompts -- reusable instruction templates. Claude Code converts each one into a slash command like `/github:review_pr`.

```python
@dataclass
class McpSkillCommand:
    server_name: str
    prompt_name: str

    def format_command(self) -> str:
        return f"/{self.server_name}:{self.prompt_name}"
```

## What Changed

| Component | Before (s10) | After (s11) |
|-----------|-------------|-------------|
| Tool surface | Fixed set of built-in tools | Dynamic: built-ins + any MCP server's tools |
| Transport | N/A | stdio, SSE, HTTP behind one manager |
| Tool naming | Simple names (`Read`, `Bash`) | Namespaced: `mcp__server__tool` |
| Output safety | No external output budget | 25k-token cap with truncation |
| Permissions | Name matched directly | Fully-qualified name prevents false matches |
| Slash commands | Built-in skills only | MCP prompts auto-register as `/server:prompt` |

## Try It

```bash
cd learn-the-actual-claude-code
python agents/s11_mcp.py
```

Watch for:

- Two servers connect (`github` via stdio, `filesystem` via stdio)
- Tool names use the `mcp__` prefix (e.g. `mcp__github__list_issues`)
- A 200k-character string gets truncated to the token limit
- `mcp__github__read` and `mcp__filesystem__read` stay distinct

Try adding a third server config and see how `get_all_tools()` aggregates tools across all connections.
