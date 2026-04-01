#!/usr/bin/env python3
"""
Session 12 -- State Management reimplementation.

Mirrors the real Claude Code state system:
  - src/state/store.ts           (Store<T> with getState/setState/subscribe)
  - src/state/AppStateStore.ts   (AppState type + getDefaultAppState)
  - src/state/onChangeAppState.ts (side-effect reactor)
  - src/state/selectors.ts       (derived-state selectors)
  - src/types/message.ts         (Message type hierarchy: 8+ types)
  - src/utils/messages.ts        (normalizeMessagesForAPI, normalizeMessages)

Run:  python sessions/s12-state-management/reimplementation.py
"""
from __future__ import annotations

import copy
import dataclasses
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Generic, Optional, TypeVar

T = TypeVar("T")


# ---------------------------------------------------------------------------
# Store<T>  (mirrors src/state/store.ts -- 35 LOC in the real source)
# ---------------------------------------------------------------------------

class Store(Generic[T]):
    """
    Minimal pub/sub state container with functional updates.

    Real source design decisions:
    - setState takes an updater function, never a raw value
    - Object.is identity check skips notification on no-op
    - onChange callback fires on every real transition (used by onChangeAppState)
    - Listeners stored in a Set for O(1) add/remove
    """

    def __init__(
        self,
        initial_state: T,
        on_change: Callable[[T, T], None] | None = None,
    ):
        self._state: T = initial_state
        self._listeners: list[Callable[[], None]] = []
        self._on_change = on_change

    def get_state(self) -> T:
        return self._state

    def set_state(self, updater: Callable[[T], T]) -> None:
        prev = self._state
        next_state = updater(prev)
        if next_state is prev:  # identity check (like Object.is)
            return
        self._state = next_state
        if self._on_change:
            self._on_change(next_state, prev)
        for listener in list(self._listeners):  # snapshot to allow unsub during iteration
            listener()

    def subscribe(self, listener: Callable[[], None]) -> Callable[[], None]:
        """Returns an unsubscribe function."""
        self._listeners.append(listener)
        return lambda: self._listeners.remove(listener)


# ---------------------------------------------------------------------------
# Message type hierarchy  (mirrors src/types/message.ts)
# ---------------------------------------------------------------------------

class MessageType(str, Enum):
    USER = "user"
    ASSISTANT = "assistant"
    SYSTEM = "system"
    ATTACHMENT = "attachment"
    PROGRESS = "progress"
    TOMBSTONE = "tombstone"
    TOOL_USE_SUMMARY = "tool_use_summary"


class SystemSubtype(str, Enum):
    INFORMATIONAL = "informational"
    API_ERROR = "api_error"
    TURN_DURATION = "turn_duration"
    COMPACT_BOUNDARY = "compact_boundary"
    MICROCOMPACT_BOUNDARY = "microcompact_boundary"
    MEMORY_SAVED = "memory_saved"
    STOP_HOOK_SUMMARY = "stop_hook_summary"
    LOCAL_COMMAND = "local_command"
    PERMISSION_RETRY = "permission_retry"
    SCHEDULED_TASK_FIRE = "scheduled_task_fire"
    AWAY_SUMMARY = "away_summary"
    BRIDGE_STATUS = "bridge_status"
    AGENTS_KILLED = "agents_killed"
    API_METRICS = "api_metrics"


class SystemLevel(str, Enum):
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"


@dataclass
class Message:
    """
    Base message.  Real source uses a discriminated union on `type`.
    We use a single class with optional fields for the Python reimplementation.
    """
    type: MessageType
    content: Any = ""
    uuid: str = field(default_factory=lambda: str(uuid.uuid4())[:8])
    timestamp: str = ""
    # --- UserMessage fields ---
    tool_use_result: str | None = None   # tool_use_id when this is a tool result
    is_meta: bool = False                # system-injected content
    is_virtual: bool = False             # display-only (never sent to API)
    is_compact_summary: bool = False     # result of compaction
    origin: str | None = None            # provenance: None = human keyboard
    permission_mode: str | None = None   # for rewind restoration
    # --- AssistantMessage fields ---
    request_id: str | None = None
    stop_reason: str | None = None
    model: str | None = None
    is_api_error_message: bool = False
    # --- SystemMessage fields ---
    level: SystemLevel = SystemLevel.INFO
    subtype: SystemSubtype | None = None
    # --- ProgressMessage fields ---
    tool_use_id: str | None = None
    parent_tool_use_id: str | None = None
    # --- ToolUseSummaryMessage fields ---
    summary: str | None = None
    preceding_tool_use_ids: list[str] = field(default_factory=list)


# --- Message constructors  (mirrors src/utils/messages.ts) ---

def create_user_message(text: str, is_meta: bool = False, origin: str | None = None) -> Message:
    return Message(type=MessageType.USER, content=text, is_meta=is_meta, origin=origin)


def create_tool_result_message(tool_use_id: str, result: str, is_error: bool = False) -> Message:
    block = {"type": "tool_result", "tool_use_id": tool_use_id, "content": result}
    if is_error:
        block["is_error"] = True
    return Message(type=MessageType.USER, content=[block], tool_use_result=tool_use_id)


def create_assistant_message(
    text: str = "", tool_uses: list[dict] | None = None, is_api_error: bool = False,
) -> Message:
    content: list[dict] = []
    if text:
        content.append({"type": "text", "text": text})
    if tool_uses:
        content.extend(tool_uses)
    return Message(type=MessageType.ASSISTANT, content=content, is_api_error_message=is_api_error)


def create_system_message(
    text: str, level: SystemLevel = SystemLevel.INFO,
    subtype: SystemSubtype = SystemSubtype.INFORMATIONAL,
) -> Message:
    return Message(type=MessageType.SYSTEM, content=text, level=level, subtype=subtype)


def create_compact_boundary() -> Message:
    return Message(type=MessageType.SYSTEM, content="[compact boundary]",
                   subtype=SystemSubtype.COMPACT_BOUNDARY)


def create_tombstone(original_uuid: str) -> Message:
    return Message(type=MessageType.TOMBSTONE, content=f"[tombstone for {original_uuid}]")


def create_tool_use_summary(summary: str, tool_use_ids: list[str]) -> Message:
    return Message(type=MessageType.TOOL_USE_SUMMARY, summary=summary,
                   preceding_tool_use_ids=tool_use_ids)


def create_progress_message(tool_use_id: str, data: Any = None) -> Message:
    return Message(type=MessageType.PROGRESS, content=data or {},
                   tool_use_id=tool_use_id)


def create_attachment_message(content: Any) -> Message:
    return Message(type=MessageType.ATTACHMENT, content=content)


def create_local_command_message(output: str) -> Message:
    """Real source: SystemLocalCommandMessage -- converted to user message for API."""
    return Message(type=MessageType.SYSTEM, content=output,
                   subtype=SystemSubtype.LOCAL_COMMAND)


# --- Message predicates  (mirrors src/utils/messagePredicates.ts) ---

def is_human_turn(msg: Message) -> bool:
    """type === 'user' && !isMeta && toolUseResult === undefined"""
    return msg.type == MessageType.USER and not msg.is_meta and msg.tool_use_result is None


def is_api_sendable(msg: Message) -> bool:
    """Would this message be included in an API call?"""
    return msg.type in (MessageType.USER, MessageType.ASSISTANT)


# ---------------------------------------------------------------------------
# normalizeMessagesForAPI  (mirrors src/utils/messages.ts)
# ---------------------------------------------------------------------------

NO_CONTENT_SENTINEL = "[no content - the assistant responded with only tool calls]"


def normalize_messages_for_api(messages: list[Message]) -> list[dict]:
    """
    Transform internal messages into clean API MessageParam[] format.

    Real source phases:
    1. Filter: remove system/progress/tombstone/attachment/compact-boundary/virtual
    2. Reorder: bubble attachments up to correct position
    3. Transform: strip thinking blocks, handle empty content, normalize tool inputs
    4. Merge: adjacent same-role messages combined
    5. Pair: ensure tool_use/tool_result matching (ensureToolResultPairing)

    This reimplementation covers phases 1, 3, 4 and part of 5.
    """
    # Phase 1: Filter to API-relevant messages
    api_messages: list[Message] = []
    for msg in messages:
        # Skip all system messages except local_command (converted to user)
        if msg.type == MessageType.SYSTEM:
            if msg.subtype == SystemSubtype.LOCAL_COMMAND:
                # Real source converts local_command to user message
                api_messages.append(Message(
                    type=MessageType.USER, content=msg.content, is_meta=True,
                    uuid=msg.uuid, timestamp=msg.timestamp,
                ))
            continue
        if msg.type in (MessageType.PROGRESS, MessageType.TOMBSTONE,
                        MessageType.TOOL_USE_SUMMARY, MessageType.ATTACHMENT):
            continue
        if msg.is_virtual:
            continue
        api_messages.append(msg)

    if not api_messages:
        return []

    # Phase 3: Convert to API dicts with transformations
    result: list[dict] = []
    for msg in api_messages:
        role = "user" if msg.type == MessageType.USER else "assistant"
        content = msg.content

        if role == "assistant":
            if isinstance(content, list):
                # Strip thinking blocks (real source checks model capability)
                filtered = [b for b in content if b.get("type") in ("text", "tool_use")]
                content = filtered if filtered else NO_CONTENT_SENTINEL
            elif not content:
                content = NO_CONTENT_SENTINEL

        result.append({"role": role, "content": content})

    # Phase 4: Merge adjacent same-role messages
    merged: list[dict] = []
    for msg_dict in result:
        if merged and merged[-1]["role"] == msg_dict["role"]:
            prev = merged[-1]
            pc, nc = prev["content"], msg_dict["content"]
            if isinstance(pc, str) and isinstance(nc, str):
                prev["content"] = pc + "\n" + nc
            elif isinstance(pc, list) and isinstance(nc, list):
                prev["content"] = pc + nc
            elif isinstance(pc, str) and isinstance(nc, list):
                prev["content"] = [{"type": "text", "text": pc}] + nc
            elif isinstance(pc, list) and isinstance(nc, str):
                prev["content"] = pc + [{"type": "text", "text": nc}]
        else:
            merged.append(msg_dict)

    # Ensure first message is user role
    if merged and merged[0]["role"] != "user":
        merged.insert(0, {"role": "user", "content": "[system initialized]"})

    return merged


def ensure_tool_result_pairing(normalized: list[dict]) -> list[dict]:
    """
    Real source: ensureToolResultPairing() -- safety net for tool_use/tool_result matching.
    Forward: insert synthetic error tool_result for unpaired tool_use.
    Reverse: strip orphaned tool_result referencing non-existent tool_use.
    """
    seen_tool_use_ids: set[str] = set()
    result: list[dict] = []

    for msg in normalized:
        content = msg.get("content")
        if msg["role"] == "assistant" and isinstance(content, list):
            for block in content:
                if block.get("type") == "tool_use" and "id" in block:
                    seen_tool_use_ids.add(block["id"])
        result.append(msg)

    # Strip orphaned tool_results
    cleaned: list[dict] = []
    for msg in result:
        content = msg.get("content")
        if msg["role"] == "user" and isinstance(content, list):
            filtered = [
                b for b in content
                if b.get("type") != "tool_result" or b.get("tool_use_id") in seen_tool_use_ids
            ]
            if filtered:
                cleaned.append({**msg, "content": filtered})
            # Drop empty user messages entirely
        else:
            cleaned.append(msg)

    return cleaned


# ---------------------------------------------------------------------------
# AppState  (mirrors src/state/AppStateStore.ts)
# ---------------------------------------------------------------------------

@dataclass
class AppState:
    """
    Simplified AppState. Real source has ~100 fields split into:
    - DeepImmutable partition (settings, permissions, UI, bridge)
    - Mutable partition (tasks, MCP, plugins, teams, speculation)
    """
    # --- Immutable partition ---
    settings: dict = field(default_factory=dict)
    verbose: bool = False
    main_loop_model: str | None = None
    main_loop_model_for_session: str | None = None
    tool_permission_mode: str = "default"
    agent: str | None = None
    kairos_enabled: bool = False
    expanded_view: str = "none"       # "none" | "tasks" | "teammates"
    footer_selection: str | None = None
    thinking_enabled: bool = True
    fast_mode: bool = False
    # Bridge state (8 fields in real source)
    repl_bridge_enabled: bool = False
    repl_bridge_connected: bool = False
    # --- Mutable partition ---
    tasks: dict = field(default_factory=dict)
    agent_name_registry: dict = field(default_factory=dict)
    mcp: dict = field(default_factory=lambda: {
        "clients": [], "tools": [], "commands": [], "resources": {},
        "plugin_reconnect_key": 0,
    })
    plugins: dict = field(default_factory=lambda: {
        "enabled": [], "disabled": [], "commands": [], "errors": [],
        "installation_status": {"marketplaces": [], "plugins": []},
        "needs_refresh": False,
    })
    team_context: dict | None = None
    inbox: dict = field(default_factory=lambda: {"messages": []})
    session_hooks: dict = field(default_factory=dict)
    todos: dict = field(default_factory=dict)
    speculation_status: str = "idle"


def get_default_app_state() -> AppState:
    """Real source: getDefaultAppState() initializes ~100 fields."""
    return AppState()


# ---------------------------------------------------------------------------
# onChangeAppState  (mirrors src/state/onChangeAppState.ts)
# ---------------------------------------------------------------------------

class SideEffectLog:
    """Collects side effects for demo visibility."""

    def __init__(self):
        self.entries: list[str] = []


def create_on_change_reactor(log: SideEffectLog) -> Callable[[AppState, AppState], None]:
    """
    Real source hooks:
    - Permission mode -> CCR metadata sync
    - mainLoopModel -> user settings persistence
    - expandedView -> global config persistence
    - verbose -> global config persistence
    - settings change -> clear auth caches, reapply env vars
    """
    def on_change(new_state: AppState, old_state: AppState) -> None:
        if new_state.tool_permission_mode != old_state.tool_permission_mode:
            log.entries.append(
                f"[reactor] permission mode: {old_state.tool_permission_mode} -> "
                f"{new_state.tool_permission_mode}"
            )
        if new_state.main_loop_model != old_state.main_loop_model:
            log.entries.append(
                f"[reactor] model: {old_state.main_loop_model} -> {new_state.main_loop_model}"
            )
        if new_state.expanded_view != old_state.expanded_view:
            log.entries.append(
                f"[reactor] view: {old_state.expanded_view} -> {new_state.expanded_view}"
            )
        if new_state.verbose != old_state.verbose:
            log.entries.append(
                f"[reactor] verbose: {old_state.verbose} -> {new_state.verbose}"
            )
    return on_change


# ---------------------------------------------------------------------------
# Selectors  (mirrors src/state/selectors.ts)
# ---------------------------------------------------------------------------

def get_viewed_teammate_task(state: AppState) -> dict | None:
    """Real source: getViewedTeammateTask -- returns task if viewing a teammate."""
    viewing_id = state.tasks.get("_viewing_agent_task_id")
    if not viewing_id:
        return None
    task = state.tasks.get(viewing_id)
    if not task or task.get("type") != "in_process_teammate":
        return None
    return task


def get_active_agent_for_input(state: AppState) -> dict:
    """
    Real source returns discriminated union:
    - { type: 'leader' }
    - { type: 'viewed', task }
    - { type: 'named_agent', task }
    """
    viewed = get_viewed_teammate_task(state)
    if viewed:
        return {"type": "viewed", "task": viewed}
    return {"type": "leader"}


# ---------------------------------------------------------------------------
# Demo
# ---------------------------------------------------------------------------

def main() -> None:
    print("=" * 70)
    print("Session 12: State Management")
    print("=" * 70)

    # --- Store lifecycle with onChange reactor ---
    print("\n--- Store<AppState> Lifecycle ---")
    effect_log = SideEffectLog()
    reactor = create_on_change_reactor(effect_log)
    store: Store[AppState] = Store(get_default_app_state(), on_change=reactor)

    # Subscribe for reactivity
    render_count = [0]
    unsub = store.subscribe(lambda: render_count.__setitem__(0, render_count[0] + 1))

    print(f"  Initial mode:  {store.get_state().tool_permission_mode}")
    print(f"  Initial model: {store.get_state().main_loop_model}")

    # Functional update (same pattern as React useState updater)
    store.set_state(lambda prev: dataclasses.replace(prev, tool_permission_mode="plan"))
    print(f"  After set:     mode={store.get_state().tool_permission_mode}")

    store.set_state(lambda prev: dataclasses.replace(prev, main_loop_model="claude-sonnet-4-20250514"))
    print(f"  After model:   model={store.get_state().main_loop_model}")

    store.set_state(lambda prev: dataclasses.replace(prev, expanded_view="tasks"))
    store.set_state(lambda prev: dataclasses.replace(prev, verbose=True))

    print(f"  Render count:  {render_count[0]}")
    print(f"  Side effects:")
    for entry in effect_log.entries:
        print(f"    {entry}")

    # Identity bailout test
    prev_state = store.get_state()
    store.set_state(lambda prev: prev)  # no-op, should not fire
    print(f"  After no-op:   renders={render_count[0]} (unchanged)")

    unsub()
    store.set_state(lambda prev: dataclasses.replace(prev, tool_permission_mode="default"))
    print(f"  After unsub:   renders={render_count[0]} (unchanged after unsub)")

    # --- Message type hierarchy ---
    print("\n--- Message Type Hierarchy (8+ types) ---")
    messages: list[Message] = [
        create_user_message("Hello, analyze this codebase"),
        create_assistant_message("I'll look at the files.", [
            {"type": "tool_use", "id": "tu_1", "name": "Read", "input": {"path": "/src/main.ts"}},
        ]),
        create_tool_result_message("tu_1", "export function main() { ... }"),
        create_system_message("Turn completed in 2.3s", subtype=SystemSubtype.TURN_DURATION),
        create_progress_message("tu_1", {"stdout": "Reading file..."}),
        create_compact_boundary(),
        create_assistant_message("The main function initializes the CLI."),
        create_system_message("Memory saved to MEMORY.md", subtype=SystemSubtype.MEMORY_SAVED),
        create_tombstone("old_msg_123"),
        create_tool_use_summary("Ran 3 bash commands", ["tu_2", "tu_3", "tu_4"]),
        create_attachment_message({"type": "memory", "content": "Project uses TypeScript"}),
        create_local_command_message("<local_command_stdout>help output</local_command_stdout>"),
        create_user_message("What does it do?"),
        create_assistant_message("It bootstraps the agent loop."),
    ]

    type_counts: dict[str, int] = {}
    for m in messages:
        type_counts[m.type.value] = type_counts.get(m.type.value, 0) + 1

    print(f"  Total messages: {len(messages)}")
    for t, c in sorted(type_counts.items()):
        print(f"    {t:20s}: {c}")
    print(f"  Human turns:    {sum(1 for m in messages if is_human_turn(m))}")

    # --- Normalization ---
    print("\n--- normalizeMessagesForAPI ---")
    normalized = normalize_messages_for_api(messages)
    paired = ensure_tool_result_pairing(normalized)
    print(f"  Input messages:    {len(messages)}")
    print(f"  After normalize:   {len(normalized)}")
    print(f"  After pairing:     {len(paired)}")

    for i, msg in enumerate(paired):
        content_preview = msg["content"]
        if isinstance(content_preview, str):
            content_preview = content_preview[:55] + ("..." if len(content_preview) > 55 else "")
        elif isinstance(content_preview, list):
            types = [b.get("type", "?") for b in content_preview]
            content_preview = f"[{', '.join(types)}]"
        print(f"  [{i}] role={msg['role']:10s} content={content_preview}")

    # --- Role alternation check ---
    print("\n--- Role Alternation Check ---")
    roles = [m["role"] for m in paired]
    alternates = all(roles[i] != roles[i + 1] for i in range(len(roles) - 1))
    print(f"  Roles:      {roles}")
    print(f"  Alternates: {alternates}")
    assert alternates, "Role alternation violated!"
    assert roles[0] == "user", "First message must be user!"
    print("  PASSED")

    # --- AppState partitions demo ---
    print("\n--- AppState Partitions ---")
    store2: Store[AppState] = Store(get_default_app_state())

    # MCP partition (mutable)
    store2.set_state(lambda prev: dataclasses.replace(prev, mcp={
        **prev.mcp,
        "tools": [
            {"name": "mcp__github__list_issues"},
            {"name": "mcp__github__create_issue"},
        ],
        "resources": {"github": [{"uri": "github://repos/test/readme"}]},
    }))
    s = store2.get_state()
    print(f"  MCP tools:     {len(s.mcp['tools'])}")
    print(f"  MCP resources: {sum(len(v) for v in s.mcp['resources'].values())}")

    # Team context (mutable)
    store2.set_state(lambda prev: dataclasses.replace(prev, team_context={
        "team_name": "alpha-team",
        "lead_agent_id": "lead@alpha",
        "teammates": {
            "lead@alpha": {"name": "team-lead", "cwd": "/repo"},
            "researcher@alpha": {"name": "researcher", "cwd": "/repo/docs"},
        },
    }))
    ctx = store2.get_state().team_context
    print(f"  Team: {ctx['team_name']}")
    print(f"  Members: {list(ctx['teammates'].keys())}")

    # --- Selector demo ---
    print("\n--- Selectors ---")
    routing = get_active_agent_for_input(store2.get_state())
    print(f"  Input routing: {routing}")

    # Simulate viewing a teammate
    store2.set_state(lambda prev: dataclasses.replace(prev, tasks={
        "_viewing_agent_task_id": "researcher@alpha",
        "researcher@alpha": {"type": "in_process_teammate", "name": "researcher"},
    }))
    routing = get_active_agent_for_input(store2.get_state())
    print(f"  After view:    {routing}")

    print("\n" + "=" * 70)
    print("Session 12 complete.")


if __name__ == "__main__":
    main()
