# Session 02 -- Tool Interface & Registration

`s01 > [ s02 ] s03 > s04 > s05 | s06 > s07 > s08 > s09 > s10 | s11 > s12 > s13 > s14`

> "Every tool follows a rich interface with typed input/output, feature gates for conditional registration, and ToolUseContext carrying 40+ fields."
>
> *Harness layer: `agents/s02_tool_system.py` reimplements the Tool ABC, ToolRegistry, and 4 concrete tools in ~700 lines of Python. Run it to see registration, gating, validation, and execution in action.*

---

## Problem

A naive agent just passes tool names and JSON schemas to the API and calls a function when the model asks. That falls apart fast:

- **No validation before execution** -- a malformed `file_path` hits the filesystem and you get a cryptic Python traceback instead of a structured error.
- **No behavioral metadata** -- the orchestrator has no idea which tools can run concurrently and which must run alone.
- **No conditional registration** -- experimental tools ship in the binary but should only activate behind feature flags.
- **No context** -- the tool function receives raw JSON but has no idea about the current working directory, file caches, abort signals, or permission state.

Claude Code solves all of these with a **30+ field Tool interface** in `src/Tool.ts` (792 LOC) and a **three-stage assembly pipeline** in `src/tools.ts` (389 LOC).

---

## Solution

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

## How It Works

### 1. The Tool abstract base class

Every tool inherits from this. `buildTool()` in the real source provides **fail-closed defaults** so tool authors only override what they need:

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

The key insight: **behavioral flags are input-dependent**. `BashTool.is_read_only()` inspects the actual command to decide:

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

### 2. ToolRegistry with feature gates

The real source uses Bun's `feature()` for dead-code elimination at build time. Our reimplementation captures the same pattern:

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

### 3. The execution pipeline

Before any tool runs, it passes through a three-step pipeline:

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

### 4. Concrete tool example: FileReadTool

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

## What Changed

| Component | Before (tutorial style) | After (Claude Code) |
|---|---|---|
| Tool definition | Dict with name + schema | 30+ field interface with behavioral flags |
| Concurrency info | None | `isReadOnly(input)`, `isConcurrencySafe(input)` per-call |
| Registration | Hardcoded list | Feature-gated registry with `assembleToolPool()` |
| Validation | Try/except in call body | Structured `validateInput()` before permissions |
| Context | Bare `cwd` string | `ToolUseContext` with 40+ fields (cwd, abort, cache, options) |
| MCP tools | N/A | Merged and deduplicated, built-ins take precedence |
| Output limits | None | `maxResultSizeChars` with disk persistence fallback |

---

## Try It

```bash
cd agents
python s02_tool_system.py
```

The demo will:
1. Build a registry with 4 tools and feature gates
2. Show input-dependent behavioral flags (same `BashTool`, different commands)
3. Run the validation pipeline (blocked device paths, same-string edits)
4. Demonstrate MCP tool merging with deduplication
5. Execute a live bash command through the full pipeline

**Source files to explore next:**
- `src/Tool.ts` -- the full 30+ field Tool interface (792 LOC)
- `src/tools.ts` -- `getAllBaseTools()`, `getTools()`, `assembleToolPool()` (389 LOC)
- `src/tools/BashTool/BashTool.tsx` -- real bash tool (~900 LOC)
- `src/tools/FileReadTool/FileReadTool.ts` -- real read tool (1,184 LOC)
