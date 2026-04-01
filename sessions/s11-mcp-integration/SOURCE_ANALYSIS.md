# Source Analysis: MCP Integration

## 1. Transport Layer

### Three Primary Transports (src/services/mcp/types.ts)

Claude Code defines transport types via Zod schemas:

```typescript
export const TransportSchema = z.enum(['stdio', 'sse', 'sse-ide', 'http', 'ws', 'sdk'])
```

Each maps to an MCP SDK transport class:

| Transport | Config Type | SDK Class | Use Case |
|-----------|------------|-----------|----------|
| `stdio` | `McpStdioServerConfigSchema` | `StdioClientTransport` | Local subprocess servers |
| `sse` | `McpSSEServerConfigSchema` | `SSEClientTransport` | Legacy remote servers |
| `http` | `McpHTTPServerConfigSchema` | `StreamableHTTPClientTransport` | Modern remote servers |
| `ws` | `McpWebSocketServerConfigSchema` | `WebSocketTransport` (custom) | WebSocket-based servers |
| `sse-ide` / `ws-ide` | IDE-specific schemas | IDE transport wrappers | VS Code / JetBrains integration |
| `sdk` | `McpSdkServerConfigSchema` | `SdkControlTransport` | In-process SDK mode |

### WebSocket Transport (src/utils/mcpWebSocketTransport.ts)

A custom transport because the MCP SDK does not ship one. Key design:

- Handles both Bun native WebSocket and Node.js `ws` package
- Uses `isBun` detection flag to switch event handler patterns
- Implements the `Transport` interface: `start()`, `close()`, `send()`, `onmessage`
- JSON-RPC message validation via `JSONRPCMessageSchema.parse()`
- Shared close handler that cleans up listeners to prevent memory leaks

### Transport Connection Flow (src/services/mcp/client.ts)

```
1. Config loaded from .mcp.json / settings / plugins
2. resolveTransport() picks the right SDK transport class
3. MCP SDK Client created: new Client({ name, version })
4. client.connect(transport) -- handshake + capability discovery
5. If capabilities.tools: listTools() -> wrap as MCPTool instances
6. If capabilities.resources: listResources() -> store in AppState.mcp.resources
7. If capabilities.prompts: listPrompts() -> convert to skills
```

## 2. Tool Discovery and Wrapping

### MCPTool Template (src/tools/MCPTool/MCPTool.ts)

The MCPTool is a **skeleton** -- its `name`, `description`, `prompt`, `call`,
and `userFacingName` fields are all placeholder stubs marked with comments
`// Overridden in mcpClient.ts`. The real values come from cloning:

```typescript
export const MCPTool = buildTool({
  isMcp: true,
  name: 'mcp',              // Overridden
  async description() { return DESCRIPTION },  // Overridden
  async prompt() { return PROMPT },            // Overridden
  async call() { return { data: '' } },        // Overridden
  userFacingName: () => 'mcp',                 // Overridden
  inputSchema: z.object({}).passthrough(),      // Accept any input
  maxResultSizeChars: 100_000,
  checkPermissions: () => ({ behavior: 'passthrough' }),
})
```

### Tool Name Construction (src/services/mcp/mcpStringUtils.ts)

Three-part format: `mcp__<normalizedServer>__<normalizedTool>`

```typescript
function buildMcpToolName(serverName: string, toolName: string): string {
  return `${getMcpPrefix(serverName)}${normalizeNameForMCP(toolName)}`
}

// Parsing the inverse:
function mcpInfoFromString(toolString: string): { serverName, toolName } | null {
  const [mcpPart, serverName, ...toolNameParts] = toolString.split('__')
  if (mcpPart !== 'mcp' || !serverName) return null
  return { serverName, toolName: toolNameParts.join('__') }
}
```

This naming is critical for:
- **Permission matching**: deny rules on built-in `Write` won't accidentally block
  an MCP tool that happens to also be called "Write"
- **Deduplication**: prevents name collisions across servers
- **Display**: `getMcpDisplayName()` strips the prefix for UI display

### Tool Call Flow in client.ts

When the model invokes an MCP tool:

1. The cloned MCPTool's `call()` executes `client.callTool({ name, arguments })`
2. Result arrives as `CallToolResult` (text content, images, or errors)
3. Result goes through `truncateMcpContentIfNeeded()` for token management
4. If result exceeds limits, it is persisted to disk and a read-pointer returned
5. Binary content (PDFs, images) detected via `isBinaryContentType()` and saved

## 3. Resource Listing

### ListMcpResourcesTool

A built-in tool that queries all connected MCP servers for their resources:

```
client.listResources() -> Resource[] per server
```

Resources are stored in `AppState.mcp.resources` keyed by server name.
Each resource has: `uri`, `name`, `description`, `mimeType`.

### ReadMcpResourceTool

Reads a specific resource by URI from a specific server, using
`client.readResource({ uri })`. Like tool results, resource content
goes through truncation and binary handling.

## 4. Output Management

### Token-Aware Truncation (src/utils/mcpValidation.ts)

```typescript
const DEFAULT_MAX_MCP_OUTPUT_TOKENS = 25000
const MCP_TOKEN_COUNT_THRESHOLD_FACTOR = 0.5

async function mcpContentNeedsTruncation(content): Promise<boolean> {
  // Fast path: rough estimate < 50% of limit -> skip API call
  const estimate = getContentSizeEstimate(content)
  if (estimate <= maxTokens * 0.5) return false
  // Slow path: exact count via API
  const tokenCount = await countMessagesTokensWithAPI(messages, [])
  return tokenCount > maxTokens
}
```

Key design decisions:
- Two-tier check: cheap heuristic first, expensive API count second
- Configurable via `MAX_MCP_OUTPUT_TOKENS` env var or GrowthBook flag
- Image blocks get a fixed 1600-token estimate
- Truncation message tells the model to use pagination tools

### Binary Content Persistence (src/utils/mcpOutputStorage.ts)

When MCP returns binary content (PDFs, images, spreadsheets):
1. `isBinaryContentType()` checks the MIME type
2. `persistBinaryContent()` writes raw bytes to `~/.claude/tool-results/<id>.<ext>`
3. `extensionForMimeType()` maps MIME -> extension (supports 20+ types)
4. Model receives a text pointer: "Binary content saved to /path"

## 5. Prompt-to-Skill Conversion

### MCP Skill Builders (src/skills/mcpSkillBuilders.ts)

A write-once registry that breaks circular dependencies:

```typescript
let builders: MCPSkillBuilders | null = null

export function registerMCPSkillBuilders(b: MCPSkillBuilders): void {
  builders = b
}

export function getMCPSkillBuilders(): MCPSkillBuilders {
  if (!builders) throw new Error('Not registered yet')
  return builders
}
```

When client.ts discovers MCP prompts via `listPrompts()`, it:
1. Gets the builders via `getMCPSkillBuilders()`
2. Calls `createSkillCommand()` for each prompt
3. Parses frontmatter fields via `parseSkillFrontmatterFields()`
4. The resulting skill commands appear in `AppState.mcp.commands`

## 6. Plugin MCP Integration (src/utils/plugins/mcpPluginIntegration.ts)

Plugins can bundle MCP servers via their manifests:

```typescript
// Plugin manifest can specify MCP servers as:
// 1. Inline config: { mcpServers: { name: { type: 'stdio', ... } } }
// 2. JSON file path: { mcpServers: 'path/to/.mcp.json' }
// 3. MCPB file: { mcpServers: 'https://example.com/server.mcpb' }
// 4. Array of mixed: { mcpServers: ['path1.json', { ... }] }
```

Environment variable resolution supports:
- `${CLAUDE_PLUGIN_ROOT}` -> plugin installation directory
- `${CLAUDE_PLUGIN_DATA}` -> plugin data directory
- `${user_config.X}` -> user-configured values from plugin settings
- Standard `${ENV_VAR}` expansion

Server names are scoped: `plugin:<pluginName>:<serverName>` to prevent collisions.

## 7. Connection Lifecycle

### MCPConnectionManager (src/services/mcp/MCPConnectionManager.tsx)

A React hook (`useManageMCPConnections`) that:
1. Watches config changes (settings file edits, plugin installs)
2. Connects new servers, disconnects removed ones
3. Handles reconnection with exponential backoff
4. Manages OAuth flows for authenticated servers
5. Updates `AppState.mcp` with tools, commands, and resources

### Cleanup

`registerCleanup()` hooks ensure transports are properly closed on process
exit, preventing zombie subprocess leaks (critical for stdio transports).
