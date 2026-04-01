# s11: MCP Integration

s01 > s02 > s03 > s04 > s05 | s06 > s07 > s08 > s09 > s10 | **[ s11 ]** s12 > s13 > s14

> "Any sufficiently advanced tool system eventually becomes a protocol."

## 问题

一个只有内置工具的 agent，能力面是固定的。每次新增一个集成 -- GitHub、数据库、文件系统服务 -- 都得在 agent 内部写新代码再发布。MCP (Model Context Protocol) 让外部进程通过标准协议暴露工具、资源和提示词，agent 在运行时动态发现就行了。

## 解决方案

Claude Code 把三种传输方式统一收敛到一个连接管理器背后，给每个工具加命名空间防冲突，还对输出做截断来保护上下文窗口。

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

命名约定 `mcp__<server>__<tool>` 让每个 MCP 工具在全局唯一。

## 工作原理

### 1. 传输配置

每个服务端声明自己的传输类型，连接管理器自动选对应的客户端。

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

### 2. 防冲突命名

两个函数构建带命名空间的工具名。`github` 服务端上的 `read` 工具变成 `mcp__github__read`，`filesystem` 服务端上的同名工具变成 `mcp__filesystem__read`。

```python
def normalize_name_for_mcp(name: str) -> str:
    return re.sub(r"[^a-zA-Z0-9_]", "_", name)

def build_mcp_tool_name(server_name: str, tool_name: str) -> str:
    return f"mcp__{normalize_name_for_mcp(server_name)}__{normalize_name_for_mcp(tool_name)}"
```

### 3. 模板克隆

客户端为每个发现的工具克隆一个 wrapper，负责路由调用并截断输出。

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

### 4. 输出截断

MCP 服务端可能返回几兆的数据。两级检查把输出上限卡在 25,000 tokens，并提示模型下次用分页。

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

### 5. Prompts 变成斜杠命令

MCP 服务端还能暴露 prompts -- 可复用的指令模板。Claude Code 把每个 prompt 转成斜杠命令，比如 `/github:review_pr`。

```python
@dataclass
class McpSkillCommand:
    server_name: str
    prompt_name: str

    def format_command(self) -> str:
        return f"/{self.server_name}:{self.prompt_name}"
```

## 变更内容

| 组件 | s10 | s11 |
|------|-----|-----|
| 工具面 | 固定的内置工具集 | 动态: 内置工具 + 任何 MCP 服务端的工具 |
| 传输方式 | 无 | stdio, SSE, HTTP 统一在一个管理器后面 |
| 工具命名 | 简单名称 (`Read`, `Bash`) | 命名空间: `mcp__server__tool` |
| 输出安全 | 没有外部输出预算 | 25k token 上限 + 截断 |
| 权限 | 名称直接匹配 | 完全限定名防止误匹配 |
| 斜杠命令 | 仅内置 skills | MCP prompts 自动注册为 `/server:prompt` |

## 试一试

```bash
cd learn-the-actual-claude-code
python agents/s11_mcp.py
```

留意这些输出：

- 两个服务端连接成功（`github` 用 stdio，`filesystem` 用 stdio）
- 工具名带 `mcp__` 前缀（如 `mcp__github__list_issues`）
- 一个 200k 字符的字符串被截断到 token 上限
- `mcp__github__read` 和 `mcp__filesystem__read` 互不冲突

试着加一个第三服务端配置，看 `get_all_tools()` 如何聚合所有连接的工具。
