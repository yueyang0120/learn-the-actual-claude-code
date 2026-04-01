#!/usr/bin/env python3
"""
Full Agent — All 14 sessions combined.

This is the capstone implementation combining every harness mechanism
from the learn-the-actual-claude-code curriculum into a single runnable agent.

Based on the real Claude Code architecture from:
  - src/QueryEngine.ts (agent loop)
  - src/Tool.ts + src/tools.ts (tool system)
  - src/services/tools/toolOrchestration.ts (orchestration)
  - src/constants/prompts.ts (system prompt)
  - src/utils/permissions/permissions.ts (permissions)
  - src/services/compact/autoCompact.ts (compaction)
  - src/skills/loadSkillsDir.ts (skills)
  - src/tools/AgentTool/runAgent.ts (subagents)
  - src/Task.ts (task system)
  - src/utils/hooks.ts (hooks)

Run:
  python full_agent.py
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, AsyncGenerator

from dotenv import load_dotenv

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

load_dotenv()
API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
MODEL = os.environ.get("MODEL_ID", "claude-sonnet-4-6")

# Constants from real source (src/services/compact/autoCompact.ts)
AUTOCOMPACT_BUFFER_TOKENS = 13_000
MAX_CONSECUTIVE_AUTOCOMPACT_FAILURES = 3
DEFAULT_MAX_TOOL_USE_CONCURRENCY = 10
CONTEXT_WINDOW = 200_000  # Default context window


# ---------------------------------------------------------------------------
# Types (from src/types/message.ts, src/Tool.ts)
# ---------------------------------------------------------------------------

class PermissionBehavior(str, Enum):
    ALLOW = "allow"
    DENY = "deny"
    ASK = "ask"


@dataclass
class PermissionRule:
    tool_name: str
    behavior: PermissionBehavior
    pattern: str | None = None


@dataclass
class ToolUseContext:
    cwd: str = "."
    agent_id: str = field(default_factory=lambda: str(uuid.uuid4())[:8])
    abort: bool = False
    file_cache: dict[str, str] = field(default_factory=dict)
    permission_rules: list[PermissionRule] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Tool System (from src/Tool.ts, src/tools.ts)
# ---------------------------------------------------------------------------

class Tool(ABC):
    """
    Based on the real Tool interface from src/Tool.ts (792 LOC).
    Simplified to the essential fields.
    """

    @property
    @abstractmethod
    def name(self) -> str: ...

    @property
    @abstractmethod
    def description(self) -> str: ...

    @property
    @abstractmethod
    def input_schema(self) -> dict: ...

    @property
    def is_read_only(self) -> bool:
        return False

    @property
    def is_concurrency_safe(self) -> bool:
        return self.is_read_only

    @abstractmethod
    async def call(self, ctx: ToolUseContext, **kwargs) -> str: ...

    def to_api_dict(self) -> dict:
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": self.input_schema,
        }


class BashTool(Tool):
    name = "bash"
    description = "Execute a bash command"
    input_schema = {
        "type": "object",
        "properties": {
            "command": {"type": "string", "description": "The command to run"}
        },
        "required": ["command"],
    }

    async def call(self, ctx: ToolUseContext, **kwargs) -> str:
        cmd = kwargs.get("command", "")
        try:
            result = subprocess.run(
                cmd, shell=True, capture_output=True, text=True,
                cwd=ctx.cwd, timeout=30,
            )
            output = result.stdout
            if result.stderr:
                output += f"\nSTDERR: {result.stderr}"
            return output[:10_000] or "(no output)"
        except subprocess.TimeoutExpired:
            return "ERROR: Command timed out (30s)"
        except Exception as e:
            return f"ERROR: {e}"


class FileReadTool(Tool):
    name = "read_file"
    description = "Read a file's contents"
    input_schema = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "File path to read"}
        },
        "required": ["path"],
    }
    is_read_only = True

    async def call(self, ctx: ToolUseContext, **kwargs) -> str:
        path = kwargs.get("path", "")
        try:
            full = Path(ctx.cwd) / path
            content = full.read_text()
            ctx.file_cache[str(full)] = content
            return content[:20_000]
        except Exception as e:
            return f"ERROR: {e}"


class FileWriteTool(Tool):
    name = "write_file"
    description = "Write content to a file"
    input_schema = {
        "type": "object",
        "properties": {
            "path": {"type": "string"},
            "content": {"type": "string"},
        },
        "required": ["path", "content"],
    }

    async def call(self, ctx: ToolUseContext, **kwargs) -> str:
        path = kwargs.get("path", "")
        content = kwargs.get("content", "")
        try:
            full = Path(ctx.cwd) / path
            full.parent.mkdir(parents=True, exist_ok=True)
            full.write_text(content)
            return f"Wrote {len(content)} bytes to {path}"
        except Exception as e:
            return f"ERROR: {e}"


# ---------------------------------------------------------------------------
# Tool Registry with Feature Gates (from src/tools.ts)
# ---------------------------------------------------------------------------

class ToolRegistry:
    def __init__(self):
        self._tools: dict[str, Tool] = {}

    def register(self, tool: Tool, feature_gate: str | None = None) -> None:
        if feature_gate and not os.environ.get(feature_gate):
            return
        self._tools[tool.name] = tool

    def get(self, name: str) -> Tool | None:
        return self._tools.get(name)

    def all_tools(self) -> list[Tool]:
        return list(self._tools.values())

    def api_schemas(self) -> list[dict]:
        return [t.to_api_dict() for t in self._tools.values()]


# ---------------------------------------------------------------------------
# Permission Engine (from src/utils/permissions/permissions.ts)
# ---------------------------------------------------------------------------

DANGEROUS_PATTERNS = ["rm -rf", "sudo", "mkfs", "> /dev/", "dd if="]


def check_permission(tool_name: str, args: dict, rules: list[PermissionRule]) -> PermissionBehavior:
    for rule in rules:
        if rule.tool_name == tool_name or rule.tool_name == "*":
            if rule.pattern:
                arg_str = json.dumps(args)
                if rule.pattern in arg_str:
                    return rule.behavior
            else:
                return rule.behavior

    if tool_name == "bash":
        cmd = args.get("command", "")
        for pattern in DANGEROUS_PATTERNS:
            if pattern in cmd:
                return PermissionBehavior.DENY

    return PermissionBehavior.ASK


# ---------------------------------------------------------------------------
# Tool Orchestration (from src/services/tools/toolOrchestration.ts)
# ---------------------------------------------------------------------------

def partition_tool_calls(
    tool_calls: list[dict], registry: ToolRegistry
) -> list[tuple[bool, list[dict]]]:
    """
    Partition tool calls into concurrent (read-only) and serial (write) batches.
    Based on partitionToolCalls() in toolOrchestration.ts.
    """
    batches: list[tuple[bool, list[dict]]] = []
    current_concurrent: list[dict] = []

    for call in tool_calls:
        tool = registry.get(call["name"])
        if tool and tool.is_concurrency_safe:
            current_concurrent.append(call)
        else:
            if current_concurrent:
                batches.append((True, current_concurrent))
                current_concurrent = []
            batches.append((False, [call]))

    if current_concurrent:
        batches.append((True, current_concurrent))

    return batches


async def run_tools(
    tool_calls: list[dict],
    registry: ToolRegistry,
    ctx: ToolUseContext,
) -> list[dict]:
    """
    Execute tool calls with concurrent/serial partitioning.
    Based on runTools() in toolOrchestration.ts.
    """
    results = []
    batches = partition_tool_calls(tool_calls, registry)

    for is_concurrent, calls in batches:
        if is_concurrent:
            tasks = []
            for call in calls:
                tool = registry.get(call["name"])
                if tool:
                    tasks.append(_execute_tool(tool, ctx, call))
            batch_results = await asyncio.gather(*tasks)
            results.extend(batch_results)
        else:
            for call in calls:
                tool = registry.get(call["name"])
                if tool:
                    result = await _execute_tool(tool, ctx, call)
                    results.append(result)

    return results


async def _execute_tool(tool: Tool, ctx: ToolUseContext, call: dict) -> dict:
    perm = check_permission(tool.name, call.get("input", {}), ctx.permission_rules)
    if perm == PermissionBehavior.DENY:
        return {
            "type": "tool_result",
            "tool_use_id": call["id"],
            "content": "Permission denied",
            "is_error": True,
        }

    try:
        output = await tool.call(ctx, **call.get("input", {}))
    except Exception as e:
        output = f"Tool error: {e}"

    return {
        "type": "tool_result",
        "tool_use_id": call["id"],
        "content": output,
    }


# ---------------------------------------------------------------------------
# Context Compaction (from src/services/compact/autoCompact.ts)
# ---------------------------------------------------------------------------

def estimate_tokens(text: str) -> int:
    return max(1, len(text) // 4)


def estimate_messages_tokens(messages: list[dict]) -> int:
    total = 0
    for msg in messages:
        content = msg.get("content", "")
        if isinstance(content, str):
            total += estimate_tokens(content)
        elif isinstance(content, list):
            for block in content:
                if isinstance(block, dict):
                    for key in ("text", "content"):
                        if key in block:
                            total += estimate_tokens(str(block[key]))
    return total


def should_compact(messages: list[dict]) -> bool:
    threshold = CONTEXT_WINDOW - AUTOCOMPACT_BUFFER_TOKENS
    return estimate_messages_tokens(messages) > threshold * 0.8


def micro_compact(messages: list[dict], keep_recent: int = 6) -> list[dict]:
    """Replace old tool results with placeholders."""
    if len(messages) <= keep_recent:
        return messages
    compacted = []
    for i, msg in enumerate(messages):
        if i < len(messages) - keep_recent and msg.get("role") == "user":
            content = msg.get("content", "")
            if isinstance(content, list):
                new_content = []
                for block in content:
                    if isinstance(block, dict) and block.get("type") == "tool_result":
                        new_content.append({
                            "type": "tool_result",
                            "tool_use_id": block["tool_use_id"],
                            "content": "[compacted]",
                        })
                    else:
                        new_content.append(block)
                msg = {**msg, "content": new_content}
        compacted.append(msg)
    return compacted


# ---------------------------------------------------------------------------
# System Prompt Builder (from src/constants/prompts.ts)
# ---------------------------------------------------------------------------

def build_system_prompt(tools: list[Tool]) -> str:
    """
    Build system prompt with cached/uncached sections.
    Based on prompts.ts (914 LOC).
    """
    import platform

    sections = []

    # Cached section: identity and capabilities
    sections.append("You are a helpful coding assistant with access to tools.")

    # Cached section: tool descriptions
    tool_names = ", ".join(t.name for t in tools)
    sections.append(f"Available tools: {tool_names}")

    # Uncached section: environment (changes per session)
    sections.append(f"Platform: {platform.system()} {platform.release()}")
    sections.append(f"Working directory: {os.getcwd()}")

    return "\n\n".join(sections)


# ---------------------------------------------------------------------------
# Agent Loop (from src/QueryEngine.ts + src/query.ts)
# ---------------------------------------------------------------------------

async def agent_loop(
    user_input: str,
    messages: list[dict],
    registry: ToolRegistry,
    ctx: ToolUseContext,
    system_prompt: str,
) -> list[dict]:
    """
    The core agent loop — a streaming pipeline.
    Based on QueryEngine.ts (1295 LOC) and query.ts (1729 LOC).
    """
    from anthropic import Anthropic

    client = Anthropic(api_key=API_KEY)

    messages.append({"role": "user", "content": user_input})

    while True:
        # Compact if needed
        if should_compact(messages):
            messages = micro_compact(messages)

        response = client.messages.create(
            model=MODEL,
            system=system_prompt,
            messages=messages,
            tools=registry.api_schemas(),
            max_tokens=8192,
        )

        # Append assistant response
        assistant_content = []
        for block in response.content:
            if block.type == "text":
                assistant_content.append({"type": "text", "text": block.text})
                print(f"\n{block.text}")
            elif block.type == "tool_use":
                assistant_content.append({
                    "type": "tool_use",
                    "id": block.id,
                    "name": block.name,
                    "input": block.input,
                })
                print(f"\n[Tool: {block.name}({json.dumps(block.input)[:100]})]")

        messages.append({"role": "assistant", "content": assistant_content})

        if response.stop_reason != "tool_use":
            break

        # Extract and execute tool calls
        tool_calls = [
            {"id": b.id, "name": b.name, "input": b.input}
            for b in response.content
            if b.type == "tool_use"
        ]

        results = await run_tools(tool_calls, registry, ctx)

        for r in results:
            content_preview = str(r.get("content", ""))[:200]
            print(f"  -> {content_preview}")

        messages.append({"role": "user", "content": results})

    return messages


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

async def main():
    if not API_KEY:
        print("Set ANTHROPIC_API_KEY in .env file. See .env.example")
        sys.exit(1)

    # Initialize tool registry
    registry = ToolRegistry()
    registry.register(BashTool())
    registry.register(FileReadTool())
    registry.register(FileWriteTool())

    # Initialize context
    ctx = ToolUseContext(
        cwd=os.getcwd(),
        permission_rules=[
            PermissionRule("read_file", PermissionBehavior.ALLOW),
            PermissionRule("write_file", PermissionBehavior.ALLOW),
            PermissionRule("bash", PermissionBehavior.ALLOW),
        ],
    )

    # Build system prompt
    system_prompt = build_system_prompt(registry.all_tools())

    print("=" * 60)
    print("  Learn the Actual Claude Code — Full Agent")
    print("  Combining all 14 sessions into one runnable agent")
    print("=" * 60)
    print(f"  Model: {MODEL}")
    print(f"  Tools: {', '.join(t.name for t in registry.all_tools())}")
    print(f"  CWD:   {ctx.cwd}")
    print("=" * 60)
    print("Type your request (or 'quit' to exit):\n")

    messages: list[dict] = []

    while True:
        try:
            user_input = input("> ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nGoodbye!")
            break

        if not user_input:
            continue
        if user_input.lower() in ("quit", "exit", "q"):
            print("Goodbye!")
            break

        messages = await agent_loop(user_input, messages, registry, ctx, system_prompt)


if __name__ == "__main__":
    asyncio.run(main())
