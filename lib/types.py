"""
Shared types for the agent system.

Based on the real Claude Code message types from:
  - src/types/message.ts
  - src/Tool.ts (ToolUseContext, ToolResult types)
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


# ---------------------------------------------------------------------------
# Message types  (mirrors src/types/message.ts)
# ---------------------------------------------------------------------------

class MessageRole(str, Enum):
    USER = "user"
    ASSISTANT = "assistant"


@dataclass
class ContentBlock:
    """Base for all content blocks in a message."""
    type: str


@dataclass
class TextBlock(ContentBlock):
    text: str
    type: str = "text"


@dataclass
class ToolUseBlock(ContentBlock):
    id: str
    name: str
    input: dict[str, Any]
    type: str = "tool_use"


@dataclass
class ToolResultBlock(ContentBlock):
    tool_use_id: str
    content: str
    is_error: bool = False
    type: str = "tool_result"


@dataclass
class Message:
    role: MessageRole
    content: list[ContentBlock] | str

    def to_api_dict(self) -> dict:
        """Convert to Anthropic API message format."""
        if isinstance(self.content, str):
            return {"role": self.role.value, "content": self.content}
        return {
            "role": self.role.value,
            "content": [_block_to_dict(b) for b in self.content],
        }


def _block_to_dict(block: ContentBlock) -> dict:
    if isinstance(block, TextBlock):
        return {"type": "text", "text": block.text}
    elif isinstance(block, ToolUseBlock):
        return {
            "type": "tool_use",
            "id": block.id,
            "name": block.name,
            "input": block.input,
        }
    elif isinstance(block, ToolResultBlock):
        d: dict[str, Any] = {
            "type": "tool_result",
            "tool_use_id": block.tool_use_id,
            "content": block.content,
        }
        if block.is_error:
            d["is_error"] = True
        return d
    return {"type": block.type}


# ---------------------------------------------------------------------------
# Tool-related types  (mirrors src/Tool.ts)
# ---------------------------------------------------------------------------

@dataclass
class ToolUseContext:
    """
    Context threaded through every tool call.

    In the real source (src/Tool.ts), ToolUseContext carries:
    - options (abort signal, cwd, stdin, etc.)
    - readFileState (shared file cache)
    - getAppState / setAppState
    - canUseTool callback
    - mcpClients
    - agentId
    - and more (~20 fields)

    We keep a simplified version.
    """
    cwd: str = "."
    agent_id: str = field(default_factory=lambda: str(uuid.uuid4())[:8])
    abort: bool = False
    file_cache: dict[str, str] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Task types  (mirrors src/Task.ts)
# ---------------------------------------------------------------------------

class TaskStatus(str, Enum):
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    FAILED = "failed"
    KILLED = "killed"


class TaskType(str, Enum):
    """
    Real source defines: local_bash, local_agent, remote_agent,
    in_process_teammate, local_workflow, monitor_mcp, dream
    """
    LOCAL_BASH = "local_bash"
    LOCAL_AGENT = "local_agent"
    IN_PROCESS_TEAMMATE = "in_process_teammate"


@dataclass
class Task:
    id: str
    subject: str
    description: str = ""
    status: TaskStatus = TaskStatus.PENDING
    task_type: TaskType = TaskType.LOCAL_BASH
    owner: str | None = None
    blocks: list[str] = field(default_factory=list)
    blocked_by: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Permission types  (mirrors src/utils/permissions/)
# ---------------------------------------------------------------------------

class PermissionBehavior(str, Enum):
    ALLOW = "allow"
    DENY = "deny"
    ASK = "ask"


class PermissionSource(str, Enum):
    MANAGED = "managed"
    USER = "user"
    PROJECT = "project"
    SESSION = "session"


@dataclass
class PermissionRule:
    tool_name: str
    behavior: PermissionBehavior
    source: PermissionSource = PermissionSource.SESSION
    pattern: str | None = None  # Optional argument pattern to match


@dataclass
class PermissionResult:
    behavior: PermissionBehavior
    rule: PermissionRule | None = None
    reason: str = ""
