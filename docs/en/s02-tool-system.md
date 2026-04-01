# s02: Tool System

`s01 > [ s02 ] s03 > s04 > s05`

> *"One ABC, thirty fields, fail-closed defaults"* -- every tool is a rich interface, not a name + JSON schema.

## Problem

In s01 the agent had one hardcoded bash tool. A real agent needs dozens of tools, each with validation, behavioral metadata (can it run in parallel?), and conditional registration behind feature flags. A naive dict of `{name: function}` cannot express any of this.

## Solution

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

Every tool inherits from one ABC. `buildTool()` provides fail-closed defaults so authors only override what they need. Real code: `src/Tool.ts` (792 LOC), `src/tools.ts` (389 LOC).

## How It Works

**1. Define the Tool ABC with fail-closed defaults.**

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

Defaults are conservative: a tool is assumed to write and to be unsafe for parallel execution unless it says otherwise.

**2. Behavioral flags are input-dependent.**

The same tool can be read-only or read-write depending on the command:

```python
class BashTool(Tool):
    def is_read_only(self, input_data: dict) -> bool:
        cmd = input_data.get("command", "").split()[0]
        return cmd in {"ls", "cat", "grep", "find", "pwd"}

    def is_concurrency_safe(self, input_data: dict) -> bool:
        return self.is_read_only(input_data)
```

`ls` is safe to run in parallel. `rm` is not. Same tool, different input.

**3. Register tools with feature gates.**

```python
registry = ToolRegistry()
registry.register(BashTool())
registry.register(FileReadTool())
registry.register(SleepTool(), requires_feature="KAIROS")  # gated

enabled = registry.get_tools()          # filters by is_enabled()
pool = registry.assemble_tool_pool(mcp) # merge MCP tools, dedup by name
```

Real code uses Bun's `feature()` for dead-code elimination at build time. Feature-gated tools never ship to users who do not have the flag.

**4. Execute through a three-step pipeline.**

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

Validation catches malformed input before it ever touches the filesystem. Permissions run after validation so they see clean data. Real pipeline lives in `src/services/tools/toolOrchestration.ts`.

## What Changed

| Component | Before (s01) | After (s02) |
|---|---|---|
| Tool definition | Bare dict with name + schema | 30+ field ABC with behavioral flags |
| Concurrency info | None | `is_read_only(input)`, `is_concurrency_safe(input)` per-call |
| Registration | Hardcoded list | Feature-gated registry with `assemble_tool_pool()` |
| Validation | Try/except inside call | Structured `validate_input()` before permissions |
| Execution | Direct function call | Three-step pipeline: validate, permissions, call |

## Try It

```bash
cd learn-the-actual-claude-code
python agents/s02_tool_system.py
```

Example things to watch for:

- `BashTool.is_read_only("ls -la")` returns `True`, but `is_read_only("git push")` returns `False`
- `FileReadTool` blocks `/dev/zero` at the validation step, before execution
- MCP tool `Bash` is deduplicated because the built-in takes precedence
