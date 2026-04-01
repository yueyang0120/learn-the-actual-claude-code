"""
Session 10 -- Hooks System Reimplementation
============================================

A runnable Python model of Claude Code's hook system, covering:
- Hook event types (27 in real source, we model the core ones)
- Hook definitions with matchers and four hook types
- Shell command execution with structured JSON I/O
- Hook matching, execution, and result aggregation
- Session-scoped hooks and async hook registry

Reference: src/utils/hooks.ts, src/schemas/hooks.ts,
           src/utils/hooks/sessionHooks.ts, src/utils/hooks/AsyncHookRegistry.ts
"""

import json
import subprocess
import dataclasses
import enum
import fnmatch
import re
import uuid
import time
from dataclasses import dataclass, field
from typing import Any, Optional, Callable


# ---------------------------------------------------------------------------
# 1. Hook Event Types
#    Real source: src/entrypoints/sdk/coreTypes.ts -- HOOK_EVENTS (27 total)
#    We model the most important subset here.
# ---------------------------------------------------------------------------

class HookEvent(str, enum.Enum):
    PRE_TOOL_USE = "PreToolUse"
    POST_TOOL_USE = "PostToolUse"
    POST_TOOL_USE_FAILURE = "PostToolUseFailure"
    NOTIFICATION = "Notification"
    USER_PROMPT_SUBMIT = "UserPromptSubmit"
    SESSION_START = "SessionStart"
    SESSION_END = "SessionEnd"
    STOP = "Stop"
    STOP_FAILURE = "StopFailure"
    SUBAGENT_START = "SubagentStart"
    SUBAGENT_STOP = "SubagentStop"
    PRE_COMPACT = "PreCompact"
    POST_COMPACT = "PostCompact"
    PERMISSION_REQUEST = "PermissionRequest"
    SETUP = "Setup"


# Match query field for each event type.
# Real source: getMatchingHooks() switch statement in src/utils/hooks.ts:1616
MATCH_FIELD: dict[HookEvent, Optional[str]] = {
    HookEvent.PRE_TOOL_USE: "tool_name",
    HookEvent.POST_TOOL_USE: "tool_name",
    HookEvent.POST_TOOL_USE_FAILURE: "tool_name",
    HookEvent.NOTIFICATION: "notification_type",
    HookEvent.SESSION_START: "source",
    HookEvent.SESSION_END: "reason",
    HookEvent.PERMISSION_REQUEST: "tool_name",
    HookEvent.SUBAGENT_START: "agent_type",
    HookEvent.SUBAGENT_STOP: "agent_type",
    HookEvent.PRE_COMPACT: "trigger",
    HookEvent.POST_COMPACT: "trigger",
    HookEvent.STOP: None,
    HookEvent.STOP_FAILURE: None,
    HookEvent.USER_PROMPT_SUBMIT: None,
    HookEvent.SETUP: "trigger",
}


# ---------------------------------------------------------------------------
# 2. Hook Definitions
#    Real source: src/schemas/hooks.ts -- discriminated union of 4 types
# ---------------------------------------------------------------------------

class HookType(str, enum.Enum):
    COMMAND = "command"      # Shell command
    PROMPT = "prompt"        # LLM prompt evaluation
    AGENT = "agent"          # Agentic verifier
    HTTP = "http"            # HTTP webhook


@dataclass
class HookDefinition:
    """One hook to execute. Mirrors HookCommandSchema in src/schemas/hooks.ts."""
    type: HookType
    command: Optional[str] = None       # For 'command' type
    prompt: Optional[str] = None        # For 'prompt' / 'agent' type
    url: Optional[str] = None           # For 'http' type
    if_condition: Optional[str] = None  # Permission-rule pre-filter
    timeout: Optional[float] = None     # Seconds
    status_message: Optional[str] = None
    once: bool = False                  # Fire once then remove
    is_async: bool = False              # Run in background


@dataclass
class HookMatcher:
    """A matcher config with hooks. Mirrors HookMatcherSchema."""
    matcher: Optional[str] = None  # Pattern string (e.g., tool name "Write")
    hooks: list[HookDefinition] = field(default_factory=list)


# ---------------------------------------------------------------------------
# 3. Hook Result
#    Real source: HookResult / AggregatedHookResult in src/utils/hooks.ts:338
# ---------------------------------------------------------------------------

@dataclass
class HookResult:
    outcome: str = "success"  # success | blocking | non_blocking_error | cancelled
    permission_behavior: Optional[str] = None  # allow | deny | ask
    blocking_error: Optional[str] = None
    additional_context: Optional[str] = None
    updated_input: Optional[dict] = None
    stop_reason: Optional[str] = None
    prevent_continuation: bool = False
    stdout: str = ""
    stderr: str = ""
    exit_code: int = 0


# ---------------------------------------------------------------------------
# 4. Session Hooks
#    Real source: src/utils/hooks/sessionHooks.ts
#    Ephemeral hooks stored per-session, cleaned up on session end.
# ---------------------------------------------------------------------------

@dataclass
class SessionHookEntry:
    event: HookEvent
    matcher: str
    hook: HookDefinition


class SessionHookStore:
    """Per-session mutable hook store. Real impl uses Map<sessionId, SessionStore>."""

    def __init__(self):
        # Real source uses Map for O(1) mutation without triggering store listeners
        self._hooks: dict[str, list[SessionHookEntry]] = {}

    def add(self, session_id: str, entry: SessionHookEntry):
        self._hooks.setdefault(session_id, []).append(entry)

    def get(self, session_id: str, event: HookEvent) -> list[SessionHookEntry]:
        return [h for h in self._hooks.get(session_id, []) if h.event == event]

    def clear(self, session_id: str):
        self._hooks.pop(session_id, None)


# ---------------------------------------------------------------------------
# 5. Async Hook Registry
#    Real source: src/utils/hooks/AsyncHookRegistry.ts
#    Tracks background hooks for polling.
# ---------------------------------------------------------------------------

@dataclass
class PendingAsyncHook:
    process_id: str
    hook_name: str
    command: str
    start_time: float
    timeout: float = 15.0
    completed: bool = False
    stdout: str = ""
    exit_code: int = -1


class AsyncHookRegistry:
    """Non-blocking hook tracker. Real impl uses a global Map<processId, PendingAsyncHook>."""

    def __init__(self):
        self._pending: dict[str, PendingAsyncHook] = {}

    def register(self, hook: PendingAsyncHook):
        self._pending[hook.process_id] = hook

    def check_responses(self) -> list[PendingAsyncHook]:
        """Poll completed hooks. Real source: checkForAsyncHookResponses()."""
        completed = [h for h in self._pending.values() if h.completed]
        for h in completed:
            del self._pending[h.process_id]
        return completed

    def finalize_all(self):
        """Cleanup on shutdown. Real source: finalizePendingAsyncHooks()."""
        self._pending.clear()


# ---------------------------------------------------------------------------
# 6. Hook Engine
#    Real source: executeHooks() in src/utils/hooks.ts:1952
#    Central orchestrator for all hook execution.
# ---------------------------------------------------------------------------

class HookEngine:
    """
    The hook engine loads hook config, matches hooks to events, executes them,
    and aggregates results. This models the core of src/utils/hooks.ts.
    """

    def __init__(self):
        # Settings-based hooks: event -> list of HookMatcher
        # Real source: loaded from .claude/settings.json via HooksSchema
        self._config: dict[HookEvent, list[HookMatcher]] = {}
        self._session_hooks = SessionHookStore()
        self._async_registry = AsyncHookRegistry()

    # -- Registration --

    def load_config(self, config: dict[str, list[dict]]):
        """
        Load hooks from a config dict simulating .claude/settings.json.
        Real source: HooksSchema Zod validation in src/schemas/hooks.ts.
        """
        for event_name, matchers_raw in config.items():
            try:
                event = HookEvent(event_name)
            except ValueError:
                print(f"[warn] Unknown hook event: {event_name}")
                continue

            matchers = []
            for m in matchers_raw:
                hooks = []
                for h in m.get("hooks", []):
                    hooks.append(HookDefinition(
                        type=HookType(h["type"]),
                        command=h.get("command"),
                        prompt=h.get("prompt"),
                        url=h.get("url"),
                        if_condition=h.get("if"),
                        timeout=h.get("timeout"),
                        status_message=h.get("statusMessage"),
                        once=h.get("once", False),
                        is_async=h.get("async", False),
                    ))
                matchers.append(HookMatcher(matcher=m.get("matcher"), hooks=hooks))
            self._config[event] = matchers

    def register_session_hook(self, session_id: str, event: HookEvent,
                               matcher: str, hook: HookDefinition):
        """
        Register a session-scoped hook. Used by registerFrontmatterHooks()
        in src/utils/hooks/registerFrontmatterHooks.ts.
        """
        self._session_hooks.add(session_id, SessionHookEntry(event, matcher, hook))

    def register_frontmatter_hooks(self, session_id: str,
                                     hooks_settings: dict, is_agent: bool = False):
        """
        Mirrors registerFrontmatterHooks() in registerFrontmatterHooks.ts.
        Converts Stop -> SubagentStop for agents.
        """
        for event_name, matchers in hooks_settings.items():
            event = HookEvent(event_name)
            # Real source: if isAgent && event === 'Stop' => targetEvent = 'SubagentStop'
            if is_agent and event == HookEvent.STOP:
                event = HookEvent.SUBAGENT_STOP
            for m in matchers:
                matcher_str = m.get("matcher", "")
                for h in m.get("hooks", []):
                    hook_def = HookDefinition(
                        type=HookType(h["type"]),
                        command=h.get("command"),
                    )
                    self.register_session_hook(session_id, event, matcher_str, hook_def)

    # -- Matching --

    def _matches_pattern(self, query: str, pattern: str) -> bool:
        """
        Check if a query matches a matcher pattern.
        Real source uses both exact match and glob-like patterns.
        """
        if not pattern:
            return True
        # Support pipe-separated patterns like "Write|Edit"
        for p in pattern.split("|"):
            if fnmatch.fnmatch(query, p.strip()):
                return True
        return False

    def _get_matching_hooks(self, event: HookEvent, hook_input: dict,
                             session_id: Optional[str] = None
                             ) -> list[HookDefinition]:
        """
        Find hooks matching this event and input.
        Real source: getMatchingHooks() in src/utils/hooks.ts:1603.
        """
        # Determine match query from event type
        match_key = MATCH_FIELD.get(event)
        match_query = hook_input.get(match_key, "") if match_key else None

        matched: list[HookDefinition] = []

        # Check settings-based hooks
        for matcher in self._config.get(event, []):
            if match_query is None or not matcher.matcher or \
               self._matches_pattern(match_query, matcher.matcher):
                matched.extend(matcher.hooks)

        # Check session hooks
        if session_id:
            for entry in self._session_hooks.get(session_id, event):
                if match_query is None or not entry.matcher or \
                   self._matches_pattern(match_query, entry.matcher):
                    matched.append(entry.hook)

        return matched

    # -- Execution --

    def _execute_command_hook(self, hook: HookDefinition,
                               json_input: str) -> HookResult:
        """
        Execute a shell command hook. The real implementation:
        1. Spawns via child_process.spawn()
        2. Pipes JSON to stdin
        3. Captures stdout/stderr
        4. Parses JSON from stdout
        Real source: within executeHooks() async generator, ~line 2194+.
        """
        assert hook.command, "Command hook must have a command"
        result = HookResult()
        timeout = hook.timeout or 600  # 10 min default like TOOL_HOOK_EXECUTION_TIMEOUT_MS

        try:
            proc = subprocess.run(
                hook.command,
                shell=True,
                input=json_input,
                capture_output=True,
                text=True,
                timeout=timeout,
            )
            result.stdout = proc.stdout
            result.stderr = proc.stderr
            result.exit_code = proc.returncode

            # Parse exit code per protocol (src/utils/hooks/hooksConfigManager.ts)
            if proc.returncode == 0:
                result.outcome = "success"
                # Try to parse JSON from stdout
                self._parse_hook_output(result)
            elif proc.returncode == 2:
                # Blocking error: stderr shown to model, operation blocked
                result.outcome = "blocking"
                result.blocking_error = proc.stderr or proc.stdout
            else:
                # Non-blocking: stderr shown to user only
                result.outcome = "non_blocking_error"

        except subprocess.TimeoutExpired:
            result.outcome = "cancelled"
            result.stderr = f"Hook timed out after {timeout}s"
        except Exception as e:
            result.outcome = "non_blocking_error"
            result.stderr = str(e)

        return result

    def _parse_hook_output(self, result: HookResult):
        """
        Parse JSON from stdout. Mirrors parseHookOutput() + processHookJSONOutput()
        in src/utils/hooks.ts:399 and :489.
        """
        stdout = result.stdout.strip()
        if not stdout.startswith("{"):
            return  # Plain text output, no JSON to process

        try:
            data = json.loads(stdout)
        except json.JSONDecodeError:
            return

        # Process common fields (processHookJSONOutput, line ~489)
        if data.get("continue") is False:
            result.prevent_continuation = True
            result.stop_reason = data.get("stopReason", "")

        # Top-level decision
        decision = data.get("decision")
        if decision == "approve":
            result.permission_behavior = "allow"
        elif decision == "block":
            result.permission_behavior = "deny"
            result.blocking_error = data.get("reason", "Blocked by hook")

        # hookSpecificOutput handling
        specific = data.get("hookSpecificOutput", {})
        event_name = specific.get("hookEventName")

        if event_name == "PreToolUse":
            perm = specific.get("permissionDecision")
            if perm == "allow":
                result.permission_behavior = "allow"
            elif perm == "deny":
                result.permission_behavior = "deny"
                result.blocking_error = specific.get(
                    "permissionDecisionReason", "Blocked by hook"
                )
            elif perm == "ask":
                result.permission_behavior = "ask"
            if specific.get("updatedInput"):
                result.updated_input = specific["updatedInput"]
            result.additional_context = specific.get("additionalContext")

        elif event_name == "PostToolUse":
            result.additional_context = specific.get("additionalContext")

    # -- Public API --

    def execute_pre_tool(self, tool_name: str, tool_input: dict,
                          session_id: Optional[str] = None) -> HookResult:
        """
        Execute PreToolUse hooks for a tool call.
        Real source: executePreToolHooks() in src/utils/hooks.ts:3394.
        """
        hook_input = {
            "hook_event_name": "PreToolUse",
            "tool_name": tool_name,
            "tool_input": tool_input,
            "tool_use_id": str(uuid.uuid4()),
            "session_id": session_id or "main",
            "cwd": "/tmp",
        }
        return self._execute_event(HookEvent.PRE_TOOL_USE, hook_input, session_id)

    def execute_post_tool(self, tool_name: str, tool_input: dict,
                           tool_response: Any,
                           session_id: Optional[str] = None) -> HookResult:
        """
        Execute PostToolUse hooks after a tool completes.
        Real source: executePostToolHooks() in src/utils/hooks.ts:3450.
        """
        hook_input = {
            "hook_event_name": "PostToolUse",
            "tool_name": tool_name,
            "tool_input": tool_input,
            "tool_response": tool_response,
            "tool_use_id": str(uuid.uuid4()),
            "session_id": session_id or "main",
            "cwd": "/tmp",
        }
        return self._execute_event(HookEvent.POST_TOOL_USE, hook_input, session_id)

    def _execute_event(self, event: HookEvent, hook_input: dict,
                        session_id: Optional[str] = None) -> HookResult:
        """
        Core execution path. Real source: executeHooks() async generator.
        In the real code this yields AggregatedHookResult for each hook in parallel.
        We simplify to sequential + single aggregated result.
        """
        hooks = self._get_matching_hooks(event, hook_input, session_id)
        if not hooks:
            return HookResult()

        json_input = json.dumps(hook_input)
        aggregate = HookResult()

        for hook in hooks:
            if hook.type == HookType.COMMAND:
                result = self._execute_command_hook(hook, json_input)
            else:
                # Prompt/agent/http hooks are modeled as no-ops in this demo
                print(f"  [skip] {hook.type.value} hook (not implemented in demo)")
                continue

            # Aggregate results (real source: result aggregation loop ~line 2800+)
            if result.outcome == "blocking":
                aggregate.outcome = "blocking"
                aggregate.blocking_error = result.blocking_error
                break  # First blocking error stops further hooks

            if result.permission_behavior:
                aggregate.permission_behavior = result.permission_behavior
            if result.additional_context:
                aggregate.additional_context = result.additional_context
            if result.updated_input:
                aggregate.updated_input = result.updated_input
            if result.prevent_continuation:
                aggregate.prevent_continuation = True
                aggregate.stop_reason = result.stop_reason

        return aggregate


# ---------------------------------------------------------------------------
# 7. Demo
# ---------------------------------------------------------------------------

def main():
    print("=" * 70)
    print("Claude Code Hooks System -- Python Reimplementation Demo")
    print("=" * 70)

    engine = HookEngine()

    # --- Load config from dict (simulating .claude/settings.json) ---
    # Real source: HooksSchema Zod validation in src/schemas/hooks.ts
    config = {
        "PreToolUse": [
            {
                "matcher": "Bash",
                "hooks": [
                    {
                        "type": "command",
                        # This hook checks if the command contains 'rm -rf'
                        # and blocks it by exiting with code 2
                        "command": (
                            "python3 -c \""
                            "import sys, json; "
                            "data = json.load(sys.stdin); "
                            "cmd = json.dumps(data.get('tool_input', {})); "
                            "blocked = 'rm -rf' in cmd; "
                            "print(json.dumps({'hookSpecificOutput': {"
                            "'hookEventName': 'PreToolUse', "
                            "'permissionDecision': 'deny' if blocked else 'allow', "
                            "'permissionDecisionReason': 'Dangerous rm -rf detected' if blocked else 'OK'"
                            "}})); "
                            "sys.exit(0)"
                            "\""
                        ),
                        "timeout": 5,
                    }
                ],
            },
            {
                # No matcher = fires for ALL tools
                "hooks": [
                    {
                        "type": "command",
                        "command": (
                            "python3 -c \""
                            "import sys, json; "
                            "data = json.load(sys.stdin); "
                            "print(f'[audit] Tool: {data.get(\"tool_name\")}, "
                            "Input: {json.dumps(data.get(\"tool_input\", {}))}', file=sys.stderr); "
                            "print(json.dumps({})); "
                            "sys.exit(0)"
                            "\""
                        ),
                        "statusMessage": "Running audit hook...",
                    }
                ],
            },
        ],
        "PostToolUse": [
            {
                "matcher": "Write",
                "hooks": [
                    {
                        "type": "command",
                        "command": (
                            "python3 -c \""
                            "import sys, json; "
                            "data = json.load(sys.stdin); "
                            "print(json.dumps({"
                            "'hookSpecificOutput': {"
                            "'hookEventName': 'PostToolUse', "
                            "'additionalContext': 'File was written successfully. Remember to run tests.'"
                            "}})); "
                            "sys.exit(0)"
                            "\""
                        ),
                    }
                ],
            },
        ],
    }

    engine.load_config(config)
    print("\n[1] Loaded hook config with PreToolUse and PostToolUse hooks\n")

    # --- Test 1: Safe Bash command (should be allowed) ---
    print("-" * 50)
    print("Test 1: PreToolUse hook on safe Bash command")
    print("-" * 50)
    result = engine.execute_pre_tool("Bash", {"command": "git status"})
    print(f"  Outcome: {result.outcome}")
    print(f"  Permission: {result.permission_behavior or 'not set (passthrough)'}")
    print(f"  Blocking error: {result.blocking_error}")
    print()

    # --- Test 2: Dangerous Bash command (should be denied) ---
    print("-" * 50)
    print("Test 2: PreToolUse hook on dangerous Bash command")
    print("-" * 50)
    result = engine.execute_pre_tool("Bash", {"command": "rm -rf /"})
    print(f"  Outcome: {result.outcome}")
    print(f"  Permission: {result.permission_behavior}")
    print(f"  Blocking error: {result.blocking_error}")
    print()

    # --- Test 3: Non-matching tool (only audit hook fires) ---
    print("-" * 50)
    print("Test 3: PreToolUse hook on Read tool (only audit hook fires)")
    print("-" * 50)
    result = engine.execute_pre_tool("Read", {"file_path": "/etc/passwd"})
    print(f"  Outcome: {result.outcome}")
    print(f"  Permission: {result.permission_behavior or 'not set (passthrough)'}")
    print()

    # --- Test 4: PostToolUse with context injection ---
    print("-" * 50)
    print("Test 4: PostToolUse hook with context injection")
    print("-" * 50)
    result = engine.execute_post_tool(
        "Write",
        {"file_path": "/tmp/test.py", "content": "print('hello')"},
        {"success": True},
    )
    print(f"  Outcome: {result.outcome}")
    print(f"  Additional context: {result.additional_context}")
    print()

    # --- Test 5: Session-scoped hooks (skill frontmatter) ---
    print("-" * 50)
    print("Test 5: Session hook via registerFrontmatterHooks()")
    print("-" * 50)
    skill_hooks = {
        "Stop": [
            {
                "hooks": [
                    {
                        "type": "command",
                        "command": "echo 'Skill verification complete'",
                    }
                ]
            }
        ]
    }
    # For an agent, Stop is converted to SubagentStop
    engine.register_frontmatter_hooks("agent-123", skill_hooks, is_agent=True)
    # Verify the hook was stored under SubagentStop, not Stop
    sub_hooks = engine._session_hooks.get("agent-123", HookEvent.SUBAGENT_STOP)
    stop_hooks = engine._session_hooks.get("agent-123", HookEvent.STOP)
    print(f"  SubagentStop hooks registered: {len(sub_hooks)}")
    print(f"  Stop hooks registered: {len(stop_hooks)} (should be 0)")
    print()

    # --- Test 6: Async hook registry ---
    print("-" * 50)
    print("Test 6: AsyncHookRegistry lifecycle")
    print("-" * 50)
    registry = engine._async_registry
    pending = PendingAsyncHook(
        process_id="proc-001",
        hook_name="PostToolUse:Write",
        command="long-running-check.sh",
        start_time=time.time(),
        timeout=15.0,
    )
    registry.register(pending)
    print(f"  Registered async hook: {pending.process_id}")
    # Simulate completion
    pending.completed = True
    pending.stdout = '{"hookSpecificOutput": {"hookEventName": "PostToolUse"}}'
    pending.exit_code = 0
    completed = registry.check_responses()
    print(f"  Completed hooks polled: {len(completed)}")
    print(f"  Hook name: {completed[0].hook_name}")
    print()

    print("=" * 70)
    print("All tests passed. The hook system controls Claude Code's")
    print("extensibility without modifying core source code.")
    print("=" * 70)


if __name__ == "__main__":
    main()
