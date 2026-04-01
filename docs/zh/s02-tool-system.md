# s02: Tool System

`s01 > [ s02 ] s03 > s04 > s05`

> *"One ABC, thirty fields, fail-closed defaults"* -- 每个工具是一个丰富的接口, 不是 name + JSON schema 的字典。

## 问题

s01 里 Agent 只有一个硬编码的 bash 工具。真实的 Agent 需要几十个工具, 每个都要有校验、行为元数据（能不能并发？）、还有 feature flag 控制的条件注册。一个简单的 `{name: function}` 字典搞不定这些。

## 解决方案

```
  Tool ABC (30+ fields)
  +-----------------------------------------------+
  |  name, description, input_schema              |  identity
  |  is_read_only(input), is_concurrency_safe     |  behavioral flags
  |  validate_input(input, ctx)                   |  pre-check
  |  check_permissions(input, ctx)                |  authorization
  |  call(input, ctx)                             |  execution
  +-----------------------------------------------+
                     |
                     v
  ToolRegistry
  +-----------------------------------------------+
  |  register(tool, requires_feature=...)         |  feature-gated
  |  get_tools()  -> enabled only                 |  runtime filter
  |  assemble_tool_pool(mcp_tools)                |  merge + dedup
  +-----------------------------------------------+
```

所有工具继承自同一个 ABC。`buildTool()` 提供 fail-closed 默认值, 工具作者只覆盖需要的部分。真实代码：`src/Tool.ts`（792 LOC）, `src/tools.ts`（389 LOC）。

## 工作原理

**1. 定义 Tool ABC, 默认值全部 fail-closed。**

```python
class Tool(ABC):
    @property
    @abstractmethod
    def name(self) -> str: ...

    @property
    @abstractmethod
    def input_schema(self) -> dict: ...

    def is_read_only(self, input_data: dict) -> bool:
        return False   # assume writes

    def is_concurrency_safe(self, input_data: dict) -> bool:
        return False   # assume NOT safe

    def validate_input(self, input_data, ctx) -> ValidationResult:
        return ValidationResult(ok=True)

    @abstractmethod
    def call(self, input_data: dict, ctx: ToolUseContext) -> ToolResult: ...
```

默认很保守：没有显式声明的工具一律被认为会写入、不可并发。

**2. 行为标志取决于输入。**

同一个工具, 不同的命令, 可以是只读也可以是读写：

```python
class BashTool(Tool):
    def is_read_only(self, input_data: dict) -> bool:
        cmd = input_data.get("command", "").split()[0]
        return cmd in {"ls", "cat", "grep", "find", "pwd"}

    def is_concurrency_safe(self, input_data: dict) -> bool:
        return self.is_read_only(input_data)
```

`ls` 可以安全并发跑, `rm` 不行。同一个工具, 不同输入。

**3. 用 feature gate 注册工具。**

```python
registry = ToolRegistry()
registry.register(BashTool())
registry.register(FileReadTool())
registry.register(SleepTool(), requires_feature="KAIROS")  # gated

enabled = registry.get_tools()          # filters by is_enabled()
pool = registry.assemble_tool_pool(mcp) # merge MCP tools, dedup by name
```

真实代码用 Bun 的 `feature()` 做构建时死代码消除。被 gate 住的工具不会出现在没有对应 flag 的用户面前。

**4. 三步流水线执行。**

```python
def execute_tool(tool, input_data, ctx) -> ToolResult:
    # Step 1: validate input (structured pre-check)
    v = tool.validate_input(input_data, ctx)
    if not v.ok:
        return ToolResult(data=v.message, is_error=True)

    # Step 2: check permissions (tool-specific + general)
    p = tool.check_permissions(input_data, ctx)
    if p.behavior == "deny":
        return ToolResult(data=p.reason, is_error=True)

    # Step 3: execute
    return tool.call(input_data, ctx)
```

validate 先拦住格式错误的输入, 不让它碰到文件系统。permissions 在 validate 之后运行, 拿到的是干净数据。真实流水线在 `src/services/tools/toolOrchestration.ts`。

## 变更内容

| 组件 | 之前 (s01) | 之后 (s02) |
|---|---|---|
| 工具定义 | name + schema 的字典 | 30+ 字段 ABC, 带行为标志 |
| 并发信息 | 无 | 每次调用的 `is_read_only(input)`, `is_concurrency_safe(input)` |
| 注册方式 | 硬编码列表 | feature-gated registry + `assemble_tool_pool()` |
| 校验 | call 里面 try/except | 结构化 `validate_input()`, 在 permissions 之前 |
| 执行 | 直接调函数 | 三步流水线：validate, permissions, call |

## 试一试

```bash
cd learn-the-actual-claude-code
python agents/s02_tool_system.py
```

注意观察：

- `BashTool.is_read_only("ls -la")` 返回 `True`, 但 `is_read_only("git push")` 返回 `False`
- `FileReadTool` 在 validate 阶段就拦住了 `/dev/zero`, 根本不会执行
- MCP 工具 `Bash` 被去重了, 因为内置工具优先
