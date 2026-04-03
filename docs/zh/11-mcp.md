# 第 11 章: MCP 集成

内置工具的能力面是固定的。每新增一个集成 -- GitHub、数据库、监控仪表盘 -- 都要在 agent 内部写代码再发布。Model Context Protocol (MCP) 打破了这个限制: 外部进程通过标准接口暴露工具、资源和 prompt, agent 在运行时动态发现即可。上一章 hooks 系统在 agent 生命周期的关键节点注入自定义逻辑; 本章的 MCP 则将整个工具面从编译期扩展到运行时。

## 问题

工具集成的增长速度远超任何单一产品团队的交付能力。GitHub 出现一个月, Jira 接着来, 然后是数据库, 然后是文件系统服务。每个集成有自己的认证模型、输出格式和错误语义。全部内置意味着维护成本随集成数线性增长。

即便将集成外部化, 仍有两个问题。第一, 命名冲突: 如果 GitHub server 和 filesystem server 都暴露名为 `read` 的工具, agent 无法区分。第二, 输出体积: 单次 MCP 调用可能返回几兆数据, 直接灌入 context window 会挤占推理空间。

集成层还必须处理 transport 多样性 (有的 server 走 stdio, 有的走 HTTP), 动态发现 (server 在连接后才宣布能力), 以及连接生命周期 (断连、重启、OAuth token 过期)。这些对 agent 必须透明 -- MCP 工具和内置工具在调用层面不应有任何区别。

## Claude Code 的解法

### Transport 抽象

Claude Code 支持六种 transport 类型, 由统一的连接管理器维护从 server 名到活跃 client 的映射。

```typescript
// src/mcp/transport.ts
type McpTransportType =
  | "stdio"    // 子进程, 通过 stdin/stdout 通信
  | "sse"      // HTTP Server-Sent Events
  | "http"     // Streamable HTTP
  | "ws"       // WebSocket
  | "sse-ide"  // IDE 桥接的 SSE
  | "sdk";     // 进程内 SDK client

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

最常见的 transport 是 `stdio`: 管理器将 server 作为子进程启动, 通过标准流通信。`sse` 和 `http` 连接远程 server。`sdk` 将 server 跑在进程内, 适合测试和紧耦合场景。

### 防冲突命名

每个 MCP 工具获得完全限定名, 模式为 `mcp__<server>__<tool>`, 用双下划线分隔。`github` server 上的 `read` 变成 `mcp__github__read`; `filesystem` server 上的同名工具变成 `mcp__filesystem__read`。冲突不可能发生。

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

normalize 函数将工具标识符中的非法字符替换为下划线。包含连字符或点号的 server 名 (域名中很常见) 由此产生合法的工具名。

### 工具模板包装

连接管理器在初始化时为 server 上发现的每个工具创建 `MCPTool` wrapper。该 wrapper 实现与内置工具相同的接口, 使 MCP 工具在工具注册表和权限系统中与原生工具无法区分。

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

模型生成对 `mcp__github__list_issues` 的调用时, 路由层剥离前缀, 识别出 `github` server, 用原始工具名 `list_issues` 向正确的 client 派发请求。包装对模型完全透明。

### 输出截断

MCP server 可以返回任意大的响应。一次数据库查询可能产出几兆行数据。截断系统应用两级检查, 上限为 25,000 tokens。

```typescript
// src/mcp/mcpUtils.ts
const MAX_MCP_OUTPUT_TOKENS = 25_000;

function truncateMcpContent(
  content: string,
  maxTokens: number = MAX_MCP_OUTPUT_TOKENS
): string {
  // 第一级: 廉价的字符长度启发式 (4 字符 ≈ 1 token)
  if (content.length <= maxTokens * 2) return content;

  // 第二级: 硬限制
  const maxChars = maxTokens * 4;
  if (content.length <= maxChars) return content;

  return content.slice(0, maxChars) +
    "\n\n[OUTPUT TRUNCATED — request smaller pages or " +
    "narrower queries to see complete results]";
}
```

第一级是低成本的字符长度启发, 为小响应跳过 token 计数的开销。第二级施加硬限制。截断消息指示模型下次使用分页, 形成自然的反馈回路来约束后续调用的输出大小。

二进制内容 (图片、PDF) 走另一条路径: 持久化到磁盘临时文件, 工具结果中包含 MIME 类型引用而非内联数据。

### Resource 与 prompt 发现

MCP server 除工具外还可以暴露 resource (文件、数据库表、API endpoint) 和 prompt (可复用的指令模板)。Claude Code 通过 `listResources()` 和 `listPrompts()` 协议方法发现它们。

```typescript
// src/mcp/McpConnectionManager.ts
async discoverCapabilities(serverName: string): Promise<void> {
  const client = this.clients.get(serverName);

  // Resources → ListMcpResourcesTool, ReadMcpResourceTool
  const resources = await client.listResources();
  this.registerResources(serverName, resources);

  // Prompts → 通过 skill builder 注册表变成斜杠命令
  const prompts = await client.listPrompts();
  for (const prompt of prompts) {
    const command = `/${serverName}:${prompt.name}`;
    skillBuilderRegistry.register(command, prompt);
  }
}
```

Prompt 得到特殊处理: 每个 prompt 被转换为 CLI 中可访问的斜杠命令。`github` server 上的 `review_pr` prompt 变成命令 `/github:review_pr`。这将 MCP prompt 协议与 Claude Code 的 skill 系统 (第 7 章) 桥接起来。

### 连接生命周期

MCP 连接不是静态的。Server 崩溃, 网络分区, OAuth token 过期。连接管理器监视配置变更, 以指数退避重连, 处理 OAuth 授权流, 并注册在会话退出时运行的清理函数。

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

Plugin manifest 可以打包 MCP server 配置, 包括用于 secret 的环境变量解析。组织可以将预配置的 MCP server 分发为 plugin, 在连接时从本地环境解析凭证。

## 关键设计决策

**双下划线分隔而非单下划线或点号。** 单下划线在工具名中频繁出现 (`list_issues`, `read_file`)。点号在某些上下文合法, 在另一些中不合法。双下划线提供了一个在自然工具名和 server 名中极其罕见的明确分隔符, 使前缀拆分完全可靠。

**25,000 token 截断上限而非全量透传。** Context window 是共享资源。单次 MCP 调用返回 100,000 tokens 会消耗可用 context 的大量份额, 降低后续所有轮次的推理质量。25,000 token 限制在信息密度和 context 保持之间取得平衡。

**Prompt 转换为斜杠命令而非保留为协议层抽象。** Claude Code 的交互界面是 CLI。斜杠命令是调用预定义行为的既定交互模式。将 MCP prompt 转为斜杠命令使其无需学习额外协议即可直接使用。

**六种 transport 类型而非强制统一。** 不同部署环境偏好不同 transport。本地开发受益于 `stdio` (简单、无网络); 远程 server 需要 `http` 或 `sse`; IDE 扩展使用 `sse-ide`。支持全部六种最大化兼容性, 额外的 transport 代码被良好隔离在 client 接口之后。

## 实际体验

开发者在设置中配置 GitHub MCP server。会话启动时, 连接管理器启动 server 进程, 发现其工具 (如 `list_issues`, `create_pr`, `read_file`), 注册为 `mcp__github__list_issues`, `mcp__github__create_pr`, `mcp__github__read_file`。模型看到这些工具与内置的 `Bash`、`Read` 并列, 调用方式无任何区别。

模型带筛选参数调用 `mcp__github__list_issues` 时, 连接管理器将调用路由到 GitHub server, 接收响应, 对照 25,000 token 限制检查大小, 返回 (可能已截断的) 结果。如果 GitHub server 还暴露了 `review_pr` prompt, 用户可以在 CLI 中直接以 `/github:review_pr` 调用。

Server 在会话中崩溃时, 连接管理器检测到断连, 退避后重连。来自崩溃 server 的工具暂时不可用, 连接恢复后重新出现。

## 总结

- 六种 transport 类型 (stdio, sse, http, ws, sse-ide, sdk) 被抽象在统一的 McpConnectionManager 之后, 处理连接、重连和清理。
- `mcp__<server>__<tool>` 双下划线命名约定确保不同 server 上的同名工具保持独立, 不可能冲突。
- 25,000 token 上限配合两级检查保护 context window 免受超大 MCP 响应侵蚀, 截断消息引导模型自行分页。
- MCP prompt 自动转换为斜杠命令, 与 Claude Code 的 skill 和命令基础设施无缝桥接。
- 连接生命周期管理涵盖配置监视、指数退避重连、OAuth 流和基于 plugin 的 server 分发 (支持环境变量解析)。
