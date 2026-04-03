# Chapter 11: MCP Integration

An agent with only built-in tools has a fixed surface area. Adding a GitHub integration, a database connector, or a filesystem server requires shipping new code inside the agent itself. The Model Context Protocol (MCP) eliminates this constraint by letting external processes expose tools, resources, and prompts through a standardized interface that the agent discovers at runtime. This chapter covers how Claude Code connects to MCP servers, namespaces their tools to prevent collisions, manages output size, and converts prompts into slash commands.

## The Problem

Tool integrations multiply faster than any single product team can ship. A GitHub tool appears one month, a Jira connector the next, then database access, then monitoring dashboards. Each integration carries its own authentication model, output format, and error semantics. Building all of them into the agent creates a maintenance burden that scales linearly with the number of integrations.

Even if the integrations are externalized, two problems remain. First, naming collisions: if a GitHub server and a filesystem server both expose a tool called `read`, the agent cannot distinguish them. Second, output volume: MCP servers can return megabytes of data from a single call, which would overwhelm the context window if passed through unmodified.

The integration layer must handle transport diversity (some servers communicate over stdio, others over HTTP), tool discovery (servers announce their capabilities dynamically), and lifecycle management (connections drop, servers restart, OAuth tokens expire). All of this must be invisible to the agent, which should see MCP tools as indistinguishable from built-in ones.

## How Claude Code Solves It

### Transport abstraction

Claude Code supports six transport types behind a unified connection manager. The manager maintains a map from server name to active client, handling the transport-specific details of connection establishment and message framing.

```typescript
// src/mcp/transport.ts
type McpTransportType =
  | "stdio"    // child process with stdin/stdout
  | "sse"      // server-sent events over HTTP
  | "http"     // streamable HTTP
  | "ws"       // WebSocket
  | "sse-ide"  // IDE-bridged SSE
  | "sdk";     // in-process SDK client

// src/mcp/McpConnectionManager.ts
class McpConnectionManager {
  private clients: Map<string, McpClient> = new Map();

  async connect(config: McpServerConfig): Promise<void> {
    const client = createClientForTransport(config.transport);
    await client.initialize();
    this.clients.set(config.name, client);
  }
}
```

The most common transport is `stdio`: the manager spawns the server as a child process and communicates over its standard streams. The `sse` and `http` transports connect to remote servers. The `sdk` transport runs the server in-process, useful for testing and tightly-coupled integrations.

### Collision-safe tool naming

Every MCP tool receives a fully-qualified name following the pattern `mcp__<server>__<tool>`, using double underscores as separators. A `read` tool on server `github` becomes `mcp__github__read`; the same tool name on server `filesystem` becomes `mcp__filesystem__read`. No collision is possible.

```typescript
// src/mcp/mcpUtils.ts
function normalize_name_for_mcp(name: string): string {
  return name.replace(/[^a-zA-Z0-9_]/g, "_");
}

function buildMcpToolName(serverName: string, toolName: string): string {
  const server = normalize_name_for_mcp(serverName);
  const tool = normalize_name_for_mcp(toolName);
  return `mcp__${server}__${tool}`;
}
```

The normalization function strips characters that are invalid in tool identifiers, replacing them with underscores. This ensures that server names containing hyphens or dots (common in domain names) produce valid tool names.

### Tool template wrapping

For each tool discovered on a server during initialization, the connection manager creates an `MCPTool` wrapper. This wrapper conforms to the same interface as built-in tools, making MCP tools indistinguishable from native tools in the tool registry and permission system.

```typescript
// src/mcp/MCPTool.ts
class MCPTool implements Tool {
  readonly name: string;            // mcp__server__tool
  readonly description: string;
  readonly inputSchema: JsonSchema;

  constructor(serverName: string, toolDef: McpToolDefinition) {
    this.name = buildMcpToolName(serverName, toolDef.name);
    this.description = toolDef.description;
    this.inputSchema = toolDef.inputSchema;
  }

  async execute(input: Record<string, unknown>): Promise<ToolResult> {
    const raw = await this.client.callTool(this.originalName, input);
    return this.processOutput(raw);
  }
}
```

The wrapping is transparent to the model. When the model generates a tool call for `mcp__github__list_issues`, the routing layer strips the prefix, identifies the `github` server, and dispatches the call to the correct client with the original tool name `list_issues`.

### Output truncation

MCP servers can return arbitrarily large responses. A database query might produce megabytes of rows. The output truncation system applies a two-tier check with a 25,000-token limit.

```typescript
// src/mcp/mcpUtils.ts
const MAX_MCP_OUTPUT_TOKENS = 25_000;

function truncateMcpContent(
  content: string,
  maxTokens: number = MAX_MCP_OUTPUT_TOKENS
): string {
  // Tier 1: fast character-length check (4 chars ≈ 1 token)
  if (content.length <= maxTokens * 2) return content;

  // Tier 2: precise token count
  const maxChars = maxTokens * 4;
  if (content.length <= maxChars) return content;

  return content.slice(0, maxChars) +
    "\n\n[OUTPUT TRUNCATED — request smaller pages or " +
    "narrower queries to see complete results]";
}
```

The first tier is a cheap character-length heuristic that avoids the cost of token counting for small responses. The second tier applies the hard limit. The truncation message instructs the model to paginate, creating a feedback loop that naturally constrains output size on subsequent calls.

Binary content (images, PDFs) follows a different path: the content is persisted to a temporary file on disk, and the tool result contains a reference with the MIME type rather than inline data.

### Resource and prompt discovery

Beyond tools, MCP servers can expose resources (files, database tables, API endpoints) and prompts (reusable instruction templates). Claude Code discovers these through the `listResources()` and `listPrompts()` protocol methods.

```typescript
// src/mcp/McpConnectionManager.ts
async discoverCapabilities(serverName: string): Promise<void> {
  const client = this.clients.get(serverName);

  // Resources → ListMcpResourcesTool, ReadMcpResourceTool
  const resources = await client.listResources();
  this.registerResources(serverName, resources);

  // Prompts → slash commands via skill builder registry
  const prompts = await client.listPrompts();
  for (const prompt of prompts) {
    const command = `/${serverName}:${prompt.name}`;
    skillBuilderRegistry.register(command, prompt);
  }
}
```

Prompts receive special treatment: each one is converted into a slash command accessible from the CLI. A `review_pr` prompt on the `github` server becomes the command `/github:review_pr`. This bridges the MCP prompt protocol with Claude Code's skill system (Chapter 7).

### Connection lifecycle

MCP connections are not static. Servers crash, networks partition, OAuth tokens expire. The connection manager watches for configuration changes, reconnects with exponential backoff, handles OAuth authorization flows, and registers cleanup functions that run on session exit.

```typescript
// src/mcp/McpConnectionManager.ts
async maintainConnection(serverName: string): Promise<void> {
  while (!this.shutdownRequested) {
    try {
      await this.connect(this.configs.get(serverName));
      await this.watchForDisconnect(serverName);
    } catch (err) {
      await backoff(this.retryCount++);
    }
  }
}

registerCleanup(serverName: string): void {
  process.on("exit", () => {
    this.clients.get(serverName)?.close();
  });
}
```

Plugin manifests can bundle MCP server configurations, including environment variable resolution for secrets. This allows organizations to distribute pre-configured MCP server setups as plugins that resolve credentials from the local environment at connection time.

## Key Design Decisions

**Double-underscore separator instead of single or dot.** Single underscores appear frequently in tool names (`list_issues`, `read_file`). Dots are valid in some contexts but not others. Double underscores provide an unambiguous delimiter that is vanishingly rare in natural tool or server names, making the prefix split reliable.

**25,000-token truncation limit instead of passing through everything.** The context window is a shared resource. A single MCP call returning 100,000 tokens would consume a large fraction of available context, degrading reasoning quality for all subsequent turns. The 25,000-token limit balances information density with context preservation.

**Prompts converted to slash commands instead of remaining protocol-level abstractions.** Users interact with Claude Code through a CLI. Slash commands are the established interaction pattern for invoking predefined behaviors. Converting MCP prompts to slash commands makes them immediately accessible without requiring users to learn a separate protocol.

**Six transport types instead of mandating one.** Different deployment environments favor different transports. Local development benefits from `stdio` (simple, no network). Remote servers require `http` or `sse`. IDE extensions use `sse-ide`. Supporting all six maximizes compatibility at the cost of additional transport code, which is well-isolated behind the client interface.

## In Practice

A developer configures a GitHub MCP server in their settings. On session start, the connection manager spawns the server process, discovers its tools (e.g., `list_issues`, `create_pr`, `read_file`), and registers them as `mcp__github__list_issues`, `mcp__github__create_pr`, `mcp__github__read_file`. The model sees these tools alongside built-in tools like `Bash` and `Read`, with no distinction in how it invokes them.

When the model calls `mcp__github__list_issues` with a filter argument, the connection manager routes the call to the GitHub server, receives the response, checks its size against the 25,000-token limit, and returns the (possibly truncated) result to the model. If the GitHub server also exposes a `review_pr` prompt, the user can invoke it directly as `/github:review_pr` from the CLI.

If the server crashes mid-session, the connection manager detects the disconnect, backs off, and reconnects. Tools from the crashed server are temporarily unavailable but reappear once the connection is restored.

## Summary

- Six transport types (stdio, sse, http, ws, sse-ide, sdk) are abstracted behind a unified McpConnectionManager that handles connection, reconnection, and cleanup.
- Collision-safe naming via `mcp__<server>__<tool>` double-underscore convention ensures that identically-named tools on different servers remain distinct.
- Output truncation at 25,000 tokens with a two-tier check protects the context window from oversized MCP responses, with a feedback message encouraging pagination.
- MCP prompts are automatically converted into slash commands, bridging the protocol with Claude Code's existing skill and command infrastructure.
- Connection lifecycle management includes configuration watching, exponential backoff reconnection, OAuth flows, and plugin-based server distribution with environment variable resolution.
