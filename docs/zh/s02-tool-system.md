# Session 02 -- 工具接口与注册

`s01 > [ s02 ] s03 > s04 > s05 | s06 > s07 > s08 > s09 > s10 | s11 > s12 > s13 > s14`

> "Every tool follows a rich interface with typed input/output, feature gates for conditional registration, and ToolUseContext carrying 40+ fields."
> "每个工具都遵循一套丰富的接口，包含类型化的输入/输出、用于条件注册的特性门控，以及携带 40+ 字段的 ToolUseContext。"
>
> *实践层：`agents/s02_tool_system.py` 用约 700 行 Python 重新实现了 Tool 抽象基类、ToolRegistry 和 4 个具体工具。运行它可以看到注册、门控、校验和执行的全过程。*

---

## 问题

一个简单的 Agent 只是把工具名称和 JSON schema 传给 API，然后在模型请求时调用函数。这很快就会出问题：

- **执行前没有校验** —— 格式错误的 `file_path` 直接访问文件系统，得到的是晦涩的 Python 错误堆栈而非结构化的错误信息。
- **没有行为元数据** —— 编排器无法知道哪些工具可以并发执行、哪些必须串行运行。
- **没有条件注册** —— 实验性工具包含在二进制文件中，但应该只在特性标志启用时才激活。
- **没有上下文** —— 工具函数收到原始 JSON，但对当前工作目录、文件缓存、中止信号或权限状态一无所知。

Claude Code 通过 `src/Tool.ts`（792 LOC）中的 **30+ 字段 Tool 接口** 和 `src/tools.ts`（389 LOC）中的 **三阶段装配流水线** 解决了所有这些问题。

---

## 解决方案

```
  Tool interface (30+ fields)
  +-----------------------------------------+
  |  name, aliases, searchHint              |  identity
  |  description, inputSchema              |  API schema
  |  isReadOnly(input), isConcurrencySafe  |  behavioral flags
  |  isEnabled(), isDestructive            |  lifecycle
  |  validateInput(input, ctx)              |  pre-check
  |  checkPermissions(input, ctx)           |  authorization
  |  call(input, ctx)                       |  execution
  |  prompt(), userFacingName()            |  display
  |  maxResultSizeChars                     |  output limits
  +-----------------------------------------+

  ToolRegistry
  +-----------------------------------------+
  |  getAllBaseTools()    -- master list     |
  |  getTools()          -- filter enabled  |
  |  assembleToolPool()  -- merge MCP tools |
  |  feature gates       -- dead-code elim  |
  +-----------------------------------------+

  ToolUseContext (40+ fields)
  +-----------------------------------------+
  |  cwd, abortSignal, readFileTimestamps   |
  |  options (permission mode, model, etc.) |
  |  setToolJSX, onUpdate callbacks         |
  +-----------------------------------------+
```

---

## 工作原理

### 1. Tool 抽象基类

每个工具都继承自此基类。真实源码中的 `buildTool()` 提供了**失败即关闭的默认值**，工具作者只需覆盖他们需要的部分：

```python
class Tool(ABC):
    @property
    @abstractmethod
    def name(self) -> str: ...

    @property
    @abstractmethod
    def description(self) -> str: ...

    @property
    @abstractmethod
    def input_schema(self) -> dict: ...

    # -- Behavioral flags (fail-closed defaults) --

    def is_read_only(self, input_data: dict) -> bool:
        return False  # assume writes

    def is_concurrency_safe(self, input_data: dict) -> bool:
        return False  # assume NOT safe

    def is_destructive(self, input_data: dict) -> bool:
        return False

    # -- Validation & Permissions --

    def validate_input(self, input_data, context) -> ValidationResult:
        return ValidationResult(ok=True)

    def check_permissions(self, input_data, context) -> PermissionResult:
        return PermissionResult(behavior=PermissionBehavior.ALLOW)

    # -- Execution --

    @abstractmethod
    def call(self, input_data: dict, context: ToolUseContext) -> ToolResult: ...
```

关键洞察：**行为标志是依赖输入的**。`BashTool.is_read_only()` 会检查实际命令来决定：

```python
class BashTool(Tool):
    def is_read_only(self, input_data: dict) -> bool:
        """Real source checks command against read-only constraints."""
        cmd = input_data.get("command", "").strip().split()[0]
        read_commands = {"ls", "cat", "head", "tail", "grep",
                         "find", "wc", "echo", "pwd", "which"}
        return cmd in read_commands

    def is_concurrency_safe(self, input_data: dict) -> bool:
        """Real source: isConcurrencySafe returns isReadOnly result."""
        return self.is_read_only(input_data)
```

### 2. 带特性门控的 ToolRegistry

真实源码使用 Bun 的 `feature()` 在构建时进行死代码消除。我们的重新实现捕获了相同的模式：

```python
class ToolRegistry:
    def __init__(self):
        self._tools: list[Tool] = []
        self._feature_flags: dict[str, bool] = {}

    def register(self, tool: Tool, *, requires_feature: str | None = None):
        """
        Real source patterns:
          - Direct: [BashTool, FileReadTool, ...]
          - Feature-gated: ...(feature('KAIROS') ? [SleepTool] : [])
          - Env-gated: ...(process.env.USER_TYPE === 'ant' ? [ConfigTool] : [])
        """
        if requires_feature and not self.feature(requires_feature):
            return
        self._tools.append(tool)

    def get_tools(self) -> list[Tool]:
        """Filter by isEnabled() -- mirrors getTools() in tools.ts."""
        return [t for t in self._tools if t.is_enabled()]

    def assemble_tool_pool(self, mcp_tools=None) -> list[Tool]:
        """Merge built-in + MCP tools, deduplicate by name.
        Real source sorts for prompt-cache stability."""
        enabled = self.get_tools()
        if not mcp_tools:
            return enabled
        seen = {t.name for t in enabled}
        merged = list(enabled)
        for mcp_tool in mcp_tools:
            if mcp_tool.name not in seen:
                merged.append(mcp_tool)
                seen.add(mcp_tool.name)
        return merged
```

### 3. 执行流水线

在任何工具运行之前，都需要经过三步流水线：

```python
def execute_tool(tool, input_data, context) -> ToolResult:
    """
    Real source pipeline (in toolOrchestration.ts):
      1. validateInput()  -- structured pre-check
      2. checkPermissions() -- tool-specific + general permission system
      3. call()           -- actual execution
    """
    # Step 1: Validate input
    validation = tool.validate_input(input_data, context)
    if not validation.ok:
        return ToolResult(
            data=f"Validation error (code {validation.error_code}): {validation.message}",
            is_error=True,
        )

    # Step 2: Check permissions
    perm = tool.check_permissions(input_data, context)
    if perm.behavior == PermissionBehavior.DENY:
        return ToolResult(data=f"Permission denied: {perm.reason}", is_error=True)

    # Step 3: Execute
    return tool.call(input_data, context)
```

### 4. 具体工具示例：FileReadTool

```python
class FileReadTool(Tool):
    @property
    def name(self) -> str:
        return "Read"

    @property
    def max_result_size_chars(self) -> int:
        return float("inf")  # avoid circular reads

    def is_read_only(self, input_data) -> bool:
        return True

    def is_concurrency_safe(self, input_data) -> bool:
        return True

    def validate_input(self, input_data, context) -> ValidationResult:
        file_path = input_data.get("file_path", "")
        if not file_path:
            return ValidationResult(ok=False, message="file_path is required", error_code=1)
        blocked = {"/dev/zero", "/dev/random", "/dev/urandom"}
        if file_path in blocked:
            return ValidationResult(
                ok=False,
                message=f"Cannot read '{file_path}': device file would block",
                error_code=9,
            )
        return ValidationResult(ok=True)
```

---

## 变化对比

| 组件 | 之前（教程风格） | 之后（Claude Code） |
|---|---|---|
| 工具定义 | 包含 name + schema 的字典 | 带行为标志的 30+ 字段接口 |
| 并发信息 | 无 | 每次调用的 `isReadOnly(input)`、`isConcurrencySafe(input)` |
| 注册方式 | 硬编码列表 | 带 `assembleToolPool()` 的特性门控注册表 |
| 校验 | 在 call 函数体内 try/except | 在权限检查之前的结构化 `validateInput()` |
| 上下文 | 裸露的 `cwd` 字符串 | 带 40+ 字段的 `ToolUseContext`（cwd、abort、cache、options） |
| MCP 工具 | 不适用 | 合并并去重，内置工具优先 |
| 输出限制 | 无 | `maxResultSizeChars` 配合磁盘持久化回退 |

---

## 试一试

```bash
cd agents
python s02_tool_system.py
```

演示将会：
1. 构建一个包含 4 个工具和特性门控的注册表
2. 展示依赖输入的行为标志（同一个 `BashTool`，不同的命令）
3. 运行校验流水线（阻止设备路径、相同字符串编辑）
4. 演示 MCP 工具合并与去重
5. 通过完整流水线执行一个实际的 bash 命令

**接下来可以探索的源文件：**
- `src/Tool.ts` -- 完整的 30+ 字段 Tool 接口 (792 LOC)
- `src/tools.ts` -- `getAllBaseTools()`、`getTools()`、`assembleToolPool()` (389 LOC)
- `src/tools/BashTool/BashTool.tsx` -- 真实的 bash 工具 (~900 LOC)
- `src/tools/FileReadTool/FileReadTool.ts` -- 真实的 read 工具 (1,184 LOC)
