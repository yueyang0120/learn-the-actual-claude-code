"""
Session 02: Tool Interface & Registration -- Reimplementation
=============================================================

A simplified but faithful Python reimplementation of Claude Code's tool system:

  - Tool ABC with the key fields from the real Tool interface
  - ToolRegistry with feature gate support
  - 4 example tools: BashTool, FileReadTool, FileWriteTool, FileEditTool
  - ToolUseContext dataclass (simplified)
  - Demonstration of registration, gating, and execution

Real source references:
  - src/Tool.ts (792 LOC) -- Tool interface, ToolUseContext, buildTool
  - src/tools.ts (389 LOC) -- getAllBaseTools(), getTools(), assembleToolPool()

Run:
    python sessions/s02-tool-interface-and-registration/reimplementation.py
"""

from __future__ import annotations

import os
import subprocess
import sys
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Imports from shared lib (built in Session 01)
# ---------------------------------------------------------------------------
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from lib.types import (
    PermissionBehavior,
    PermissionResult,
    ToolUseContext,
)


# ---------------------------------------------------------------------------
# ValidationResult -- mirrors src/Tool.ts lines 95-101
# ---------------------------------------------------------------------------

@dataclass
class ValidationResult:
    """
    Real source:
      type ValidationResult =
        | { result: true }
        | { result: false; message: string; errorCode: number }
    """
    ok: bool
    message: str = ""
    error_code: int = 0


# ---------------------------------------------------------------------------
# ToolResult -- mirrors src/Tool.ts lines 321-336
# ---------------------------------------------------------------------------

@dataclass
class ToolResult:
    """
    Real source wraps output with optional side effects:
      - newMessages: inject synthetic messages
      - contextModifier: mutate context for next tools
      - mcpMeta: MCP protocol metadata
    """
    data: Any
    is_error: bool = False
    new_messages: list[dict] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Tool ABC -- mirrors src/Tool.ts lines 362-695
#
# The real interface has 30+ fields. We capture the most important ones:
#   name, description, input_schema, is_read_only, is_concurrency_safe,
#   is_enabled, validate_input, check_permissions, call, max_result_size_chars
# ---------------------------------------------------------------------------

class Tool(ABC):
    """
    Abstract base class for all tools.

    In the real source, Tool is a TypeScript generic type with ~30 fields.
    buildTool() provides fail-closed defaults so tool authors only override
    what they need. We replicate that pattern with default method implementations.
    """

    @property
    @abstractmethod
    def name(self) -> str:
        """Primary tool name (e.g., 'Bash', 'Read')."""
        ...

    @property
    def aliases(self) -> list[str]:
        """Optional backwards-compat aliases. Real source: Tool.aliases."""
        return []

    @property
    def search_hint(self) -> str:
        """Keyword phrase for ToolSearch matching. Real source: Tool.searchHint."""
        return ""

    @property
    @abstractmethod
    def description(self) -> str:
        """One-line description sent to the model."""
        ...

    @property
    @abstractmethod
    def input_schema(self) -> dict:
        """JSON Schema for the tool input. Real source uses Zod schemas."""
        ...

    @property
    def max_result_size_chars(self) -> int:
        """
        Threshold before result is persisted to disk.
        Real source: Tool.maxResultSizeChars.
        FileReadTool sets Infinity to avoid circular reads.
        """
        return 30_000

    # -- Behavioral flags (fail-closed defaults, matching buildTool) ----------

    def is_enabled(self) -> bool:
        """Default: True. Override to gate on runtime conditions."""
        return True

    def is_read_only(self, input_data: dict) -> bool:
        """
        Default: False (assume writes). Real source default from TOOL_DEFAULTS.
        Drives concurrent/serial partitioning in tool orchestration.
        """
        return False

    def is_concurrency_safe(self, input_data: dict) -> bool:
        """
        Default: False (assume NOT safe). Real source default from TOOL_DEFAULTS.
        BashTool overrides: safe only when is_read_only returns True.
        """
        return False

    def is_destructive(self, input_data: dict) -> bool:
        """Default: False. Override for delete/overwrite/send operations."""
        return False

    # -- Validation & Permissions --------------------------------------------

    def validate_input(self, input_data: dict, context: ToolUseContext) -> ValidationResult:
        """
        Runs BEFORE permission check. Returns structured error with code.
        Real source: Tool.validateInput? (optional in interface, but
        FileReadTool uses it extensively for path/format validation).
        """
        return ValidationResult(ok=True)

    def check_permissions(self, input_data: dict, context: ToolUseContext) -> PermissionResult:
        """
        Default: allow. Real source default from TOOL_DEFAULTS:
          checkPermissions: (input, _ctx?) => Promise.resolve({ behavior: 'allow', updatedInput: input })
        Tools override for tool-specific permission logic.
        """
        return PermissionResult(behavior=PermissionBehavior.ALLOW)

    # -- Execution -----------------------------------------------------------

    @abstractmethod
    def call(self, input_data: dict, context: ToolUseContext) -> ToolResult:
        """
        Execute the tool. Real source signature:
          call(args, context, canUseTool, parentMessage, onProgress?)
        We simplify to (input_data, context).
        """
        ...

    # -- Display (simplified; real source uses React/Ink) --------------------

    def user_facing_name(self, input_data: dict | None = None) -> str:
        """Human-readable name for UI display."""
        return self.name

    def matches_name(self, name: str) -> bool:
        """Check primary name or aliases. Real source: toolMatchesName()."""
        return self.name == name or name in self.aliases


# ---------------------------------------------------------------------------
# ToolRegistry -- mirrors getAllBaseTools() + getTools() + feature gates
# ---------------------------------------------------------------------------

class ToolRegistry:
    """
    Tool registration with feature gate support.

    In the real source, tools.ts has:
      - getAllBaseTools(): master list with conditional spreading
      - getTools(): filters by deny rules, REPL mode, isEnabled()
      - assembleToolPool(): merges built-in + MCP tools, deduplicates

    We combine these into a single registry with feature gates.
    """

    def __init__(self) -> None:
        self._tools: list[Tool] = []
        self._feature_flags: dict[str, bool] = {}

    def set_feature(self, flag: str, enabled: bool) -> None:
        """
        Set a feature flag. Real source uses Bun's feature() from 'bun:bundle'
        for dead-code elimination at build time. Flags include:
          KAIROS, PROACTIVE, AGENT_TRIGGERS, WEB_BROWSER_TOOL, etc.
        """
        self._feature_flags[flag] = enabled

    def feature(self, flag: str) -> bool:
        """Check if a feature flag is enabled."""
        return self._feature_flags.get(flag, False)

    def register(self, tool: Tool, *, requires_feature: str | None = None) -> None:
        """
        Register a tool, optionally gated on a feature flag.

        Real source patterns:
          - Direct: [BashTool, FileReadTool, ...]
          - Feature-gated: ...(feature('KAIROS') ? [SleepTool] : [])
          - Env-gated: ...(process.env.USER_TYPE === 'ant' ? [ConfigTool] : [])
        """
        if requires_feature and not self.feature(requires_feature):
            return
        self._tools.append(tool)

    def get_all_base_tools(self) -> list[Tool]:
        """
        Return all registered tools (before filtering).
        Mirrors getAllBaseTools() in tools.ts.
        """
        return list(self._tools)

    def get_tools(self) -> list[Tool]:
        """
        Return enabled tools (after filtering).
        Mirrors getTools() which filters by isEnabled() and deny rules.
        """
        return [t for t in self._tools if t.is_enabled()]

    def find_tool(self, name: str) -> Tool | None:
        """
        Find a tool by name or alias.
        Real source: findToolByName() in Tool.ts.
        """
        for tool in self._tools:
            if tool.matches_name(name):
                return tool
        return None

    def assemble_tool_pool(self, mcp_tools: list[Tool] | None = None) -> list[Tool]:
        """
        Merge built-in tools with MCP tools, deduplicate by name.
        Real source: assembleToolPool() sorts for prompt-cache stability
        and uses uniqBy('name') so built-ins take precedence.
        """
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


# ===========================================================================
# Example Tools -- mirrors real tool implementations
# ===========================================================================

class BashTool(Tool):
    """
    Mirrors src/tools/BashTool/BashTool.tsx.
    Real tool has ~900 LOC handling timeouts, sandboxing, sed parsing,
    background tasks, progress streaming, and security analysis.
    """

    @property
    def name(self) -> str:
        return "Bash"

    @property
    def search_hint(self) -> str:
        return "execute shell commands"

    @property
    def description(self) -> str:
        return "Run a shell command"

    @property
    def input_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "command": {"type": "string", "description": "The command to execute"},
                "timeout": {"type": "number", "description": "Optional timeout in ms"},
                "description": {"type": "string", "description": "What this command does"},
            },
            "required": ["command"],
        }

    def is_read_only(self, input_data: dict) -> bool:
        """
        Real source (line 437-441): checks command against read-only
        constraints and whether it contains cd.
        We simplify: read-only if command starts with a known read command.
        """
        cmd = input_data.get("command", "").strip().split()[0] if input_data.get("command") else ""
        read_commands = {"ls", "cat", "head", "tail", "grep", "find", "wc", "echo", "pwd", "which"}
        return cmd in read_commands

    def is_concurrency_safe(self, input_data: dict) -> bool:
        """Real source: isConcurrencySafe returns isReadOnly result."""
        return self.is_read_only(input_data)

    def validate_input(self, input_data: dict, context: ToolUseContext) -> ValidationResult:
        if not input_data.get("command", "").strip():
            return ValidationResult(ok=False, message="Command cannot be empty", error_code=1)
        return ValidationResult(ok=True)

    def call(self, input_data: dict, context: ToolUseContext) -> ToolResult:
        command = input_data["command"]
        timeout = input_data.get("timeout", 120_000) / 1000  # ms -> seconds
        try:
            result = subprocess.run(
                command,
                shell=True,
                capture_output=True,
                text=True,
                timeout=timeout,
                cwd=context.cwd,
            )
            output = result.stdout
            if result.stderr:
                output += f"\n(stderr): {result.stderr}"
            return ToolResult(data=output.strip())
        except subprocess.TimeoutExpired:
            return ToolResult(data=f"Command timed out after {timeout}s", is_error=True)
        except Exception as e:
            return ToolResult(data=str(e), is_error=True)


class FileReadTool(Tool):
    """
    Mirrors src/tools/FileReadTool/FileReadTool.ts (1,184 LOC).
    Real tool handles text, images, PDFs, notebooks, file-unchanged dedup,
    token validation, and macOS screenshot path normalization.
    """

    @property
    def name(self) -> str:
        return "Read"

    @property
    def search_hint(self) -> str:
        return "read files, images, PDFs, notebooks"

    @property
    def description(self) -> str:
        return "Read a file from the filesystem"

    @property
    def input_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "file_path": {"type": "string", "description": "Absolute path to read"},
                "offset": {"type": "integer", "description": "Start line (1-indexed)"},
                "limit": {"type": "integer", "description": "Number of lines to read"},
            },
            "required": ["file_path"],
        }

    @property
    def max_result_size_chars(self) -> int:
        return float("inf")  # type: ignore[return-value]

    def is_read_only(self, input_data: dict) -> bool:
        return True  # Always read-only

    def is_concurrency_safe(self, input_data: dict) -> bool:
        return True  # Always safe to run in parallel

    def validate_input(self, input_data: dict, context: ToolUseContext) -> ValidationResult:
        file_path = input_data.get("file_path", "")
        if not file_path:
            return ValidationResult(ok=False, message="file_path is required", error_code=1)
        # Real source checks: deny rules, UNC paths, binary extensions,
        # blocked device paths (/dev/zero, /dev/random, etc.)
        blocked = {"/dev/zero", "/dev/random", "/dev/urandom", "/dev/stdin", "/dev/tty"}
        if file_path in blocked:
            return ValidationResult(
                ok=False,
                message=f"Cannot read '{file_path}': device file would block or produce infinite output",
                error_code=9,
            )
        return ValidationResult(ok=True)

    def call(self, input_data: dict, context: ToolUseContext) -> ToolResult:
        file_path = input_data["file_path"]
        offset = input_data.get("offset", 1)
        limit = input_data.get("limit")
        try:
            path = Path(file_path).expanduser()
            text = path.read_text()
            lines = text.splitlines()
            start = max(0, offset - 1)
            end = start + limit if limit else len(lines)
            selected = lines[start:end]
            # Add line numbers (matches real source's addLineNumbers format)
            numbered = "\n".join(
                f"{i + start + 1:>6}\t{line}" for i, line in enumerate(selected)
            )
            # Cache in context (real source uses FileStateCache for dedup)
            context.file_cache[file_path] = text
            return ToolResult(data=numbered)
        except FileNotFoundError:
            return ToolResult(data=f"File does not exist: {file_path}", is_error=True)
        except Exception as e:
            return ToolResult(data=str(e), is_error=True)


class FileWriteTool(Tool):
    """Mirrors src/tools/FileWriteTool/FileWriteTool.ts."""

    @property
    def name(self) -> str:
        return "Write"

    @property
    def description(self) -> str:
        return "Write content to a file (creates or overwrites)"

    @property
    def input_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "file_path": {"type": "string", "description": "Absolute path to write"},
                "content": {"type": "string", "description": "Content to write"},
            },
            "required": ["file_path", "content"],
        }

    def is_destructive(self, input_data: dict) -> bool:
        return True  # Overwrites existing files

    def call(self, input_data: dict, context: ToolUseContext) -> ToolResult:
        file_path = input_data["file_path"]
        content = input_data["content"]
        try:
            path = Path(file_path).expanduser()
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content)
            lines = content.count("\n") + 1
            return ToolResult(data=f"Wrote {lines} lines to {file_path}")
        except Exception as e:
            return ToolResult(data=str(e), is_error=True)


class FileEditTool(Tool):
    """Mirrors src/tools/FileEditTool/FileEditTool.ts."""

    @property
    def name(self) -> str:
        return "Edit"

    @property
    def description(self) -> str:
        return "Make exact string replacements in a file"

    @property
    def input_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "file_path": {"type": "string", "description": "Absolute path to edit"},
                "old_string": {"type": "string", "description": "Text to find"},
                "new_string": {"type": "string", "description": "Replacement text"},
            },
            "required": ["file_path", "old_string", "new_string"],
        }

    def validate_input(self, input_data: dict, context: ToolUseContext) -> ValidationResult:
        if input_data.get("old_string") == input_data.get("new_string"):
            return ValidationResult(
                ok=False,
                message="old_string and new_string must be different",
                error_code=1,
            )
        return ValidationResult(ok=True)

    def call(self, input_data: dict, context: ToolUseContext) -> ToolResult:
        file_path = input_data["file_path"]
        old_string = input_data["old_string"]
        new_string = input_data["new_string"]
        try:
            path = Path(file_path).expanduser()
            content = path.read_text()
            count = content.count(old_string)
            if count == 0:
                return ToolResult(
                    data=f"old_string not found in {file_path}",
                    is_error=True,
                )
            if count > 1:
                return ToolResult(
                    data=f"old_string appears {count} times -- must be unique. Provide more context.",
                    is_error=True,
                )
            new_content = content.replace(old_string, new_string, 1)
            path.write_text(new_content)
            return ToolResult(data=f"Edited {file_path}: replaced 1 occurrence")
        except FileNotFoundError:
            return ToolResult(data=f"File does not exist: {file_path}", is_error=True)
        except Exception as e:
            return ToolResult(data=str(e), is_error=True)


# ===========================================================================
# Demonstration
# ===========================================================================

def build_default_registry() -> ToolRegistry:
    """
    Build a registry mirroring getAllBaseTools() with feature gates.

    Real source pattern:
      - Always-on tools: BashTool, FileReadTool, FileEditTool, FileWriteTool, ...
      - Feature-gated: ...(feature('KAIROS') ? [SleepTool] : [])
      - Env-gated: ...(process.env.USER_TYPE === 'ant' ? [ConfigTool] : [])
    """
    registry = ToolRegistry()

    # Simulate feature flags (real source uses Bun's feature() from 'bun:bundle')
    registry.set_feature("KAIROS", False)
    registry.set_feature("PROACTIVE", False)
    registry.set_feature("AGENT_TRIGGERS", False)
    registry.set_feature("WEB_BROWSER_TOOL", False)

    # Always-on tools (unconditionally registered)
    registry.register(BashTool())
    registry.register(FileReadTool())
    registry.register(FileWriteTool())
    registry.register(FileEditTool())

    # Feature-gated tools would go here:
    # registry.register(SleepTool(), requires_feature="KAIROS")
    # registry.register(CronCreateTool(), requires_feature="AGENT_TRIGGERS")

    return registry


def execute_tool(
    tool: Tool,
    input_data: dict,
    context: ToolUseContext,
) -> ToolResult:
    """
    Execute a tool with the full validation pipeline.

    Real source pipeline (in toolOrchestration.ts):
      1. validateInput() -- structured pre-check
      2. checkPermissions() -- tool-specific + general permission system
      3. call() -- actual execution
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


def main() -> None:
    print("=" * 70)
    print("Session 02: Tool Interface & Registration")
    print("Reimplementation of src/Tool.ts + src/tools.ts")
    print("=" * 70)

    # Build registry
    registry = build_default_registry()
    context = ToolUseContext(cwd=os.getcwd())

    # Show registered tools
    all_tools = registry.get_all_base_tools()
    enabled_tools = registry.get_tools()
    print(f"\nRegistered tools: {len(all_tools)}")
    print(f"Enabled tools:    {len(enabled_tools)}")
    for tool in enabled_tools:
        ro = "read-only" if tool.is_read_only({}) else "read-write"
        cs = "concurrent-safe" if tool.is_concurrency_safe({}) else "serial"
        print(f"  [{tool.name:>6}]  {tool.description:<45}  ({ro}, {cs})")

    # Demonstrate tool lookup by name
    print("\n--- Tool Lookup ---")
    found = registry.find_tool("Bash")
    print(f"find_tool('Bash'): {found.name if found else 'None'}")
    found = registry.find_tool("NonExistent")
    print(f"find_tool('NonExistent'): {found.name if found else 'None'}")

    # Demonstrate behavioral flags (input-dependent)
    print("\n--- Input-Dependent Behavior (BashTool) ---")
    bash = registry.find_tool("Bash")
    assert bash is not None
    for cmd in ["ls -la", "rm -rf /tmp/test", "grep pattern file.txt", "git push"]:
        inp = {"command": cmd}
        ro = bash.is_read_only(inp)
        cs = bash.is_concurrency_safe(inp)
        print(f"  '{cmd}' -> read_only={ro}, concurrency_safe={cs}")

    # Demonstrate validation pipeline
    print("\n--- Validation Pipeline ---")
    read_tool = registry.find_tool("Read")
    assert read_tool is not None

    # Valid input
    result = execute_tool(read_tool, {"file_path": "/dev/zero"}, context)
    print(f"  Read /dev/zero: error={result.is_error}, data='{result.data[:60]}...'")

    # Invalid input (empty path)
    result = execute_tool(read_tool, {"file_path": ""}, context)
    print(f"  Read '': error={result.is_error}, data='{result.data}'")

    # Demonstrate Edit validation
    edit_tool = registry.find_tool("Edit")
    assert edit_tool is not None
    result = execute_tool(
        edit_tool,
        {"file_path": "/tmp/x", "old_string": "same", "new_string": "same"},
        context,
    )
    print(f"  Edit same->same: error={result.is_error}, data='{result.data}'")

    # Demonstrate feature gating
    print("\n--- Feature Gates ---")
    print(f"  KAIROS enabled: {registry.feature('KAIROS')}")
    print(f"  Tools before enabling KAIROS: {len(registry.get_tools())}")

    # Simulate enabling a feature (real source: changes at bundle time)
    registry.set_feature("KAIROS", True)
    # In real code, this would cause SleepTool, SendUserFileTool, etc. to register
    print(f"  KAIROS now enabled: {registry.feature('KAIROS')}")

    # Demonstrate MCP tool merging
    print("\n--- MCP Tool Merging (assembleToolPool) ---")

    class MockMCPTool(Tool):
        """Simulates an MCP-provided tool."""
        def __init__(self, tool_name: str):
            self._name = tool_name
        @property
        def name(self) -> str:
            return self._name
        @property
        def description(self) -> str:
            return f"MCP tool: {self._name}"
        @property
        def input_schema(self) -> dict:
            return {"type": "object", "properties": {}}
        def call(self, input_data: dict, context: ToolUseContext) -> ToolResult:
            return ToolResult(data=f"MCP {self._name} executed")

    mcp_tools = [MockMCPTool("mcp__github__create_issue"), MockMCPTool("Bash")]
    merged = registry.assemble_tool_pool(mcp_tools)
    print(f"  Built-in tools: {len(registry.get_tools())}")
    print(f"  MCP tools: {len(mcp_tools)}")
    print(f"  Merged (deduped): {len(merged)}")
    print(f"  Names: {[t.name for t in merged]}")
    print(f"  Note: MCP 'Bash' was deduplicated (built-in takes precedence)")

    # Execute a real tool
    print("\n--- Live Execution ---")
    result = execute_tool(bash, {"command": "echo 'Hello from reimplemented BashTool'"}, context)
    print(f"  Bash 'echo': {result.data}")

    print("\n" + "=" * 70)
    print("Done. See SOURCE_ANALYSIS.md for the full annotated walkthrough.")
    print("=" * 70)


if __name__ == "__main__":
    main()
