#!/usr/bin/env python3
"""
Session 11 – MCP Integration reimplementation.

Mirrors the real Claude Code MCP subsystem:
  - src/services/mcp/client.ts        (client connection + tool/resource/prompt discovery)
  - src/services/mcp/types.ts          (transport config schemas)
  - src/services/mcp/mcpStringUtils.ts (tool naming: mcp__server__tool)
  - src/tools/MCPTool/MCPTool.ts       (template tool cloned per MCP tool)
  - src/utils/mcpValidation.ts         (output truncation)
  - src/utils/mcpOutputStorage.ts      (binary result persistence)
  - src/skills/mcpSkillBuilders.ts     (prompt -> skill conversion)

Run:  python sessions/s11-mcp-integration/reimplementation.py
"""
from __future__ import annotations

import json
import re
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Optional

# ---------------------------------------------------------------------------
# Transport types  (mirrors src/services/mcp/types.ts TransportSchema)
# ---------------------------------------------------------------------------

class TransportType(str, Enum):
    STDIO = "stdio"
    SSE = "sse"
    HTTP = "http"           # StreamableHTTP
    WS = "ws"               # WebSocket
    SSE_IDE = "sse-ide"     # IDE extension via SSE
    SDK = "sdk"             # In-process SDK


@dataclass
class McpServerConfig:
    """
    Unified config for any MCP server.
    Real source has separate schemas per transport type:
      McpStdioServerConfigSchema, McpSSEServerConfigSchema, etc.
    """
    name: str
    transport: TransportType = TransportType.STDIO
    command: str = ""           # stdio only
    args: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)
    url: str = ""               # SSE / HTTP / WS
    headers: dict[str, str] = field(default_factory=dict)
    scope: str = "local"        # local | user | project | dynamic


# ---------------------------------------------------------------------------
# MCP name utilities  (mirrors src/services/mcp/mcpStringUtils.ts)
# ---------------------------------------------------------------------------

def normalize_name_for_mcp(name: str) -> str:
    """Real source: src/services/mcp/normalization.ts normalizeNameForMCP"""
    return re.sub(r"[^a-zA-Z0-9_]", "_", name)


def build_mcp_tool_name(server_name: str, tool_name: str) -> str:
    """mcp__<server>__<tool> format for internal identification."""
    return f"mcp__{normalize_name_for_mcp(server_name)}__{normalize_name_for_mcp(tool_name)}"


def parse_mcp_tool_name(full_name: str) -> tuple[str, str] | None:
    """Inverse of build_mcp_tool_name.  Returns (server, tool) or None."""
    parts = full_name.split("__")
    if len(parts) < 3 or parts[0] != "mcp":
        return None
    server = parts[1]
    tool = "__".join(parts[2:])
    return (server, tool)


def get_mcp_display_name(full_name: str, server_name: str) -> str:
    """Strip prefix for user-facing display."""
    prefix = f"mcp__{normalize_name_for_mcp(server_name)}__"
    return full_name.replace(prefix, "")


def user_facing_name(server_name: str, tool_name: str) -> str:
    """e.g. 'github - list_issues (MCP)'"""
    return f"{server_name} - {tool_name} (MCP)"


# ---------------------------------------------------------------------------
# MCP Tool, Resource, Prompt data  (from MCP SDK types)
# ---------------------------------------------------------------------------

@dataclass
class McpToolDef:
    """A tool definition discovered from an MCP server."""
    name: str
    description: str
    input_schema: dict[str, Any] = field(default_factory=dict)


@dataclass
class McpResource:
    """A resource exposed by an MCP server."""
    uri: str
    name: str
    description: str = ""
    mime_type: str = "text/plain"


@dataclass
class McpPrompt:
    """A prompt template exposed by an MCP server."""
    name: str
    description: str = ""
    arguments: list[dict[str, Any]] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Mock MCP Server  (simulates what a real server provides)
# ---------------------------------------------------------------------------

class MockMcpServer:
    """
    In-process mock that stands in for a real MCP server.
    Provides tools, resources, and prompts for testing.
    """

    def __init__(self, name: str):
        self.name = name
        self._tools: dict[str, Callable] = {}
        self._resources: dict[str, str] = {}
        self._prompts: dict[str, McpPrompt] = {}

    def register_tool(self, name: str, description: str, handler: Callable) -> None:
        self._tools[name] = handler

    def register_resource(self, uri: str, name: str, content: str) -> None:
        self._resources[uri] = content

    def register_prompt(self, prompt: McpPrompt) -> None:
        self._prompts[prompt.name] = prompt

    # --- MCP protocol methods (simulate JSON-RPC) ---

    def list_tools(self) -> list[McpToolDef]:
        return [
            McpToolDef(name=n, description=f"Tool: {n}")
            for n in self._tools
        ]

    def call_tool(self, name: str, arguments: dict) -> dict:
        handler = self._tools.get(name)
        if not handler:
            return {"isError": True, "content": [{"type": "text", "text": f"Unknown tool: {name}"}]}
        result = handler(**arguments)
        return {"content": [{"type": "text", "text": str(result)}]}

    def list_resources(self) -> list[McpResource]:
        return [
            McpResource(uri=uri, name=uri.split("/")[-1])
            for uri in self._resources
        ]

    def read_resource(self, uri: str) -> dict:
        content = self._resources.get(uri, "")
        return {"contents": [{"uri": uri, "text": content}]}

    def list_prompts(self) -> list[McpPrompt]:
        return list(self._prompts.values())


# ---------------------------------------------------------------------------
# Output management  (mirrors src/utils/mcpValidation.ts)
# ---------------------------------------------------------------------------

MAX_MCP_OUTPUT_TOKENS = 25_000
TOKEN_THRESHOLD_FACTOR = 0.5

def estimate_tokens(text: str) -> int:
    return max(1, len(text) // 4)


def truncate_mcp_content(content: str, max_tokens: int = MAX_MCP_OUTPUT_TOKENS) -> str:
    """
    Real source: mcpValidation.ts truncateMcpContentIfNeeded()
    Two-tier: fast estimate then exact count. We simplify to estimate-only.
    """
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


# ---------------------------------------------------------------------------
# Wrapped MCP Tool  (mirrors MCPTool clone in client.ts)
# ---------------------------------------------------------------------------

@dataclass
class WrappedMcpTool:
    """
    A concrete tool instance produced by cloning MCPTool per discovered tool.
    Real source: client.ts creates one per server tool, overriding name/call/etc.
    """
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

    @property
    def is_mcp(self) -> bool:
        return True


# ---------------------------------------------------------------------------
# Skill from MCP Prompt  (mirrors src/skills/mcpSkillBuilders.ts)
# ---------------------------------------------------------------------------

@dataclass
class McpSkillCommand:
    """
    A slash command derived from an MCP prompt.
    Real source uses createSkillCommand() from loadSkillsDir.ts.
    """
    name: str
    description: str
    server_name: str
    prompt_name: str
    arguments: list[dict[str, Any]]

    def format_command(self) -> str:
        return f"/{self.server_name}:{self.prompt_name}"


# ---------------------------------------------------------------------------
# MCP Client  (mirrors src/services/mcp/client.ts)
# ---------------------------------------------------------------------------

class McpClient:
    """
    The central MCP client that connects to a server, discovers its
    capabilities, and wraps them for the agent system.

    Real source responsibilities:
    - Transport selection (stdio/SSE/HTTP/WS)
    - MCP SDK Client creation and handshake
    - Tool wrapping with MCPTool clones
    - Resource and prompt discovery
    - Output truncation and binary handling
    - OAuth flow for authenticated servers
    - Reconnection with exponential backoff
    """

    def __init__(self, config: McpServerConfig, server: MockMcpServer):
        self.config = config
        self.server = server  # In real code, this is an SDK Client over a transport
        self.server_name = config.name
        self.tools: list[WrappedMcpTool] = []
        self.resources: list[McpResource] = []
        self.skills: list[McpSkillCommand] = []
        self.connected = False

    def connect(self) -> None:
        """
        Simulate: client.connect(transport)
        Real source: creates transport, calls client.connect(), discovers capabilities
        """
        print(f"  [MCP] Connecting to '{self.server_name}' via {self.config.transport.value}...")
        self.connected = True
        self._discover_tools()
        self._discover_resources()
        self._discover_prompts()
        print(f"  [MCP] Connected: {len(self.tools)} tools, "
              f"{len(self.resources)} resources, {len(self.skills)} skills")

    def _discover_tools(self) -> None:
        """
        Real source: client.listTools() then wraps each as MCPTool clone.
        The clone overrides: name, description, prompt, call, userFacingName.
        """
        for tool_def in self.server.list_tools():
            internal_name = build_mcp_tool_name(self.server_name, tool_def.name)
            display = user_facing_name(self.server_name, tool_def.name)

            wrapped = WrappedMcpTool(
                internal_name=internal_name,
                display_name=display,
                description=tool_def.description,
                server_name=self.server_name,
                tool_name=tool_def.name,
                input_schema=tool_def.input_schema,
                _call_fn=self.server.call_tool,
            )
            self.tools.append(wrapped)

    def _discover_resources(self) -> None:
        """Real source: client.listResources(), stored in AppState.mcp.resources"""
        self.resources = self.server.list_resources()

    def _discover_prompts(self) -> None:
        """
        Real source: client.listPrompts(), then converted to skills via
        getMCPSkillBuilders().createSkillCommand()
        """
        for prompt in self.server.list_prompts():
            skill = McpSkillCommand(
                name=prompt.name,
                description=prompt.description,
                server_name=self.server_name,
                prompt_name=prompt.name,
                arguments=prompt.arguments,
            )
            self.skills.append(skill)

    def disconnect(self) -> None:
        """Real source: client.close() + transport cleanup"""
        self.connected = False
        print(f"  [MCP] Disconnected from '{self.server_name}'")


# ---------------------------------------------------------------------------
# MCP Connection Manager  (mirrors src/services/mcp/MCPConnectionManager.tsx)
# ---------------------------------------------------------------------------

class McpConnectionManager:
    """
    Manages multiple MCP server connections.
    Real source is a React hook (useManageMCPConnections) that watches
    config changes and maintains client lifecycle in AppState.mcp.
    """

    def __init__(self) -> None:
        self.clients: dict[str, McpClient] = {}

    def connect_server(self, config: McpServerConfig, server: MockMcpServer) -> McpClient:
        if config.name in self.clients:
            print(f"  [Manager] Already connected to '{config.name}'")
            return self.clients[config.name]

        client = McpClient(config, server)
        client.connect()
        self.clients[config.name] = client
        return client

    def disconnect_server(self, name: str) -> None:
        client = self.clients.pop(name, None)
        if client:
            client.disconnect()

    def get_all_tools(self) -> list[WrappedMcpTool]:
        """Aggregate tools across all connected servers."""
        tools = []
        for client in self.clients.values():
            tools.extend(client.tools)
        return tools

    def get_all_resources(self) -> dict[str, list[McpResource]]:
        """Resources keyed by server name."""
        return {
            name: client.resources
            for name, client in self.clients.items()
        }

    def get_all_skills(self) -> list[McpSkillCommand]:
        skills = []
        for client in self.clients.values():
            skills.extend(client.skills)
        return skills

    def disconnect_all(self) -> None:
        for name in list(self.clients.keys()):
            self.disconnect_server(name)


# ---------------------------------------------------------------------------
# Permission check for MCP tools  (mirrors mcpStringUtils.ts)
# ---------------------------------------------------------------------------

def get_tool_name_for_permission_check(tool: WrappedMcpTool) -> str:
    """
    Real source: getToolNameForPermissionCheck()
    Uses fully qualified mcp__server__tool name so deny rules on builtins
    (e.g., 'Write') don't accidentally match MCP tools with the same display name.
    """
    return tool.internal_name


# ---------------------------------------------------------------------------
# Demo
# ---------------------------------------------------------------------------

def main() -> None:
    print("=" * 70)
    print("Session 11: MCP Integration")
    print("=" * 70)

    # --- Create a mock MCP server with tools, resources, and prompts ---
    server = MockMcpServer("github")
    server.register_tool(
        "list_issues",
        "List open issues",
        lambda repo="test", state="open": f"Issues for {repo}: [{state}] #1 Bug, #2 Feature",
    )
    server.register_tool(
        "create_issue",
        "Create a new issue",
        lambda repo="test", title="New", body="": f"Created issue '{title}' in {repo}",
    )
    server.register_resource(
        "github://repos/anthropic/claude-code/readme",
        "readme",
        "# Claude Code\nThe official CLI for Claude.",
    )
    server.register_prompt(McpPrompt(
        name="review_pr",
        description="Review a pull request",
        arguments=[{"name": "pr_number", "required": True}],
    ))

    # --- Connect via manager ---
    print("\n--- Connecting MCP server ---")
    config = McpServerConfig(name="github", transport=TransportType.STDIO)
    manager = McpConnectionManager()
    client = manager.connect_server(config, server)

    # --- Inspect discovered tools ---
    print("\n--- Discovered Tools ---")
    for tool in manager.get_all_tools():
        parsed = parse_mcp_tool_name(tool.internal_name)
        perm_name = get_tool_name_for_permission_check(tool)
        print(f"  Internal: {tool.internal_name}")
        print(f"  Display:  {tool.display_name}")
        print(f"  Parsed:   server={parsed[0]}, tool={parsed[1]}" if parsed else "  (parse failed)")
        print(f"  PermName: {perm_name}")
        print()

    # --- Call a tool ---
    print("--- Tool Call: list_issues ---")
    tool = manager.get_all_tools()[0]
    result = tool.call({"repo": "claude-code", "state": "open"})
    print(f"  Result: {result}")

    # --- Test truncation ---
    print("\n--- Output Truncation ---")
    big_content = "x" * 200_000
    truncated = truncate_mcp_content(big_content, max_tokens=1000)
    print(f"  Input:     {len(big_content)} chars")
    print(f"  Truncated: {len(truncated)} chars")
    print(f"  Ends with: ...{truncated[-60:]}")

    # --- Resources ---
    print("\n--- Resources ---")
    for server_name, resources in manager.get_all_resources().items():
        for res in resources:
            print(f"  [{server_name}] {res.uri} ({res.mime_type})")

    # --- Skills from prompts ---
    print("\n--- Skills (from MCP prompts) ---")
    for skill in manager.get_all_skills():
        print(f"  Command: {skill.format_command()}")
        print(f"  Desc:    {skill.description}")
        args_str = ", ".join(a["name"] for a in skill.arguments)
        print(f"  Args:    {args_str}")

    # --- Add a second server ---
    print("\n--- Adding second server ---")
    server2 = MockMcpServer("filesystem")
    server2.register_tool(
        "read_file",
        "Read file contents",
        lambda path="/tmp/test.txt": f"Contents of {path}: hello world",
    )
    config2 = McpServerConfig(name="filesystem", transport=TransportType.STDIO)
    manager.connect_server(config2, server2)

    print("\n--- All Tools (cross-server) ---")
    for tool in manager.get_all_tools():
        print(f"  {tool.internal_name} -> {tool.display_name}")

    # --- Name collision safety ---
    print("\n--- Name Collision Safety ---")
    print("  If both servers had a 'read' tool:")
    name_a = build_mcp_tool_name("github", "read")
    name_b = build_mcp_tool_name("filesystem", "read")
    print(f"    github:     {name_a}")
    print(f"    filesystem: {name_b}")
    print(f"    Collision?  {name_a == name_b}  (should be False)")

    # --- Cleanup ---
    print("\n--- Cleanup ---")
    manager.disconnect_all()

    print("\n" + "=" * 70)
    print("Session 11 complete.")


if __name__ == "__main__":
    main()
