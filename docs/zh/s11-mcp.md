# Session 11 -- MCP 集成

s01 > s02 > s03 > s04 > s05 | s06 > s07 > s08 > s09 > s10 | **s11** > s12 > s13 > s14

> "Any sufficiently advanced tool system eventually becomes a protocol."
> -- "任何足够先进的工具系统最终都会演变为一种协议。"
>
> *Harness 层: MCP 是 agent 与外部世界之间的桥梁。Claude Code 复用三种传输方式，为每个远程能力克隆一份模板工具，并截断输出以保护上下文窗口。*

---

## 问题

一个只能调用自身内置工具的 agent，其能力面是固定的。每一次新的集成 -- GitHub、文件系统、数据库 -- 都需要在 agent 内部编写并发布新代码。

如果外部进程能够通过标准协议**暴露**工具、资源和提示词，而 agent 可以在运行时动态发现并调用它们，会怎样？

这正是 MCP（Model Context Protocol）所做的事情。但将其接入会带来几个问题：

1. **传输方式多样化** -- 有些服务端以子进程形式运行（stdio），有些是远程 HTTP 端点（SSE、StreamableHTTP）。客户端如何统一处理所有传输方式？
2. **命名冲突** -- 如果两个 MCP 服务端都暴露了一个 `read` 工具，如何区分它们？
3. **上下文预算** -- MCP 工具可能返回数 MB 的数据。如何防止响应内容撑爆上下文窗口？
4. **权限控制** -- 一个名为 `Write` 的远程 MCP 工具不能意外匹配到内置 `Write` 工具的权限规则。

---

## 解决方案

Claude Code 的 MCP 子系统通过三层设计解决了这些问题：传输复用器、模板克隆工具包装器，以及输出截断。

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

命名约定 `mcp__<server>__<tool>` 是关键洞见。它使每个 MCP 工具在全局唯一，并防止权限规则与内置工具发生冲突。

---

## 工作原理

### 1. 传输配置

每个 MCP 服务端起初都是一个描述使用何种传输方式的配置对象。真实源码为每种传输类型定义了独立的 Zod schema；我们的重新实现将它们统一为单个 dataclass：

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

来源: `src/services/mcp/types.ts` -- `McpStdioServerConfigSchema`、`McpSSEServerConfigSchema` 等。

### 2. 工具命名约定

两个函数处理防冲突命名：

```python
def normalize_name_for_mcp(name: str) -> str:
    """Real source: src/services/mcp/normalization.ts normalizeNameForMCP"""
    return re.sub(r"[^a-zA-Z0-9_]", "_", name)

def build_mcp_tool_name(server_name: str, tool_name: str) -> str:
    """mcp__<server>__<tool> format for internal identification."""
    return f"mcp__{normalize_name_for_mcp(server_name)}__{normalize_name_for_mcp(tool_name)}"
```

这意味着 `github` 服务端上的 `read` 工具会变成 `mcp__github__read`，而 `filesystem` 服务端上的同名工具则变成 `mcp__filesystem__read`。不会冲突。

来源: `src/services/mcp/mcpStringUtils.ts`

### 3. 模板克隆 -- WrappedMcpTool

真实源码有一个 `MCPTool` 类，它会为每个发现的工具**克隆**一份。每个克隆体覆盖了名称、描述、提示词和调用方法。我们的重新实现用 `WrappedMcpTool` 来体现这一点：

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

注意每次调用都会经过 `truncate_mcp_content` -- 上下文预算的守卫。

来源: `src/tools/MCPTool/MCPTool.ts`、`client.ts`

### 4. 输出截断

MCP 服务端可以返回任意大小的响应。截断函数使用两级检查：先做快速的字符长度估算，然后（在真实源码中）进行精确的 token 计数：

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

截断消息不仅仅是提示信息 -- 它告诉模型下次应该使用分页。

来源: `src/utils/mcpValidation.ts`

### 5. 连接管理器

所有 MCP 客户端通过 `McpConnectionManager` 进行管理，它维护生命周期并聚合跨服务端的能力：

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

真实源码是一个 React hook（`useManageMCPConnections`），它监视配置变化并在 `AppState.mcp` 中维护客户端生命周期。

来源: `src/services/mcp/MCPConnectionManager.tsx`

### 6. Prompts 变成 Skills

MCP 服务端还可以暴露 **prompts** -- 可复用的指令模板。Claude Code 将它们转换为斜杠命令：

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

因此，`github` 服务端上名为 `review_pr` 的 prompt 会变成斜杠命令 `/github:review_pr`。

来源: `src/skills/mcpSkillBuilders.ts`

---

## 变化对比

| 组件 | 之前 | 之后 |
|------|------|------|
| 工具面 | 仅固定的内置工具集 | 动态: 内置工具 + 任何 MCP 服务端的工具 |
| 传输方式 | 无 | 3+ 种传输方式 (stdio, SSE, HTTP) 统一在一个客户端之后 |
| 工具命名 | 简单函数名 (`Read`, `Bash`) | 命名空间限定: `mcp__server__tool` |
| 输出安全 | 无外部输出预算 | 25,000 token 上限，附带截断消息 |
| 权限 | 工具名直接匹配 | 完全限定的 MCP 名称防止误匹配 |
| 资源访问 | 仅文件 (通过 Read/Glob) | MCP 资源，使用 URI 方案 (`github://...`) |
| 斜杠命令 | 仅内置 skills | MCP prompts 自动注册为 `/server:prompt` |

---

## 试一试

```bash
# Run the MCP integration demo
python agents/s11_mcp.py
```

输出中需要关注的要点：

1. **两个服务端连接** -- `github`（包含 tools + resources + prompts）和 `filesystem`（包含一个 read 工具）
2. **工具名使用 `mcp__` 前缀** -- 例如 `mcp__github__list_issues`
3. **工具调用结果** -- `list_issues` 调用返回数据并经过截断处理
4. **截断生效** -- 一个 200,000 字符的字符串被裁剪到 token 上限
5. **无命名冲突** -- `mcp__github__read` 和 `mcp__filesystem__read` 是不同的工具

尝试修改代码，添加第三个服务端，然后观察 `get_all_tools()` 如何无缝聚合所有三个连接的工具。
