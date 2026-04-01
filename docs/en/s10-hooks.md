# Session 10 -- Hooks: Lifecycle Extensibility

s01 > s02 > s03 > s04 > s05 | s06 > s07 > s08 > s09 > **s10** | s11 > s12 > s13 > s14

---

> *"The best extension system is one you never have to patch the source for."*
>
> **Harness layer**: This session covers the hooks system -- the extensibility
> mechanism that lets users inject custom behavior at 27 lifecycle points
> without modifying Claude Code's core source. Hooks are the seam between
> "what Claude Code does" and "what you need it to do."

---

## Problem

Every team has different rules. One team blocks `rm -rf` in all Bash commands.
Another wants to audit every file write. A third needs to auto-approve certain
safe tool calls. You cannot bake all of these into the core product -- the
combinations are infinite.

You need a system that:

- Fires at well-defined lifecycle points (before tool use, after tool use, etc.)
- Matches selectively (only for specific tools, notification types, etc.)
- Communicates through structured JSON (not fragile string parsing)
- Uses exit codes to control flow (0 = success, 2 = block, other = warning)
- Supports async execution for non-blocking checks
- Can be scoped to a session (for skill-specific hooks)

## Solution

Claude Code implements a hook system with **27 event types**, **4 hook types**
(command, prompt, agent, HTTP), and **matcher-based selective firing**. Hooks
are configured in `.claude/settings.json` and executed as shell commands that
receive JSON on stdin and produce JSON on stdout.

```
  .claude/settings.json
  +------------------------------------------+
  | "hooks": {                               |
  |   "PreToolUse": [                        |
  |     { "matcher": "Bash",                 |
  |       "hooks": [                         |
  |         { "type": "command",             |
  |           "command": "check-safety.sh" } |
  |       ]                                  |
  |     }                                    |
  |   ]                                      |
  | }                                        |
  +------------------------------------------+
           |
           v
  +------------------------------------------+
  | HookEngine                                |
  |                                           |
  |  1. Match event + tool name to hooks      |
  |  2. Pipe JSON to stdin of shell command   |
  |  3. Parse JSON from stdout                |
  |  4. Exit code 0 = pass, 2 = block         |
  |  5. Aggregate results across all hooks    |
  +------------------------------------------+
           |
           v
  Tool execution proceeds or is blocked
```

## How It Works

### Hook Event Types

The system defines 27 event types. Each event has a match field that determines
what the matcher pattern is compared against.

```python
# agents/s10_hooks.py -- mirrors HOOK_EVENTS in coreTypes.ts

class HookEvent(str, enum.Enum):
    PRE_TOOL_USE = "PreToolUse"
    POST_TOOL_USE = "PostToolUse"
    POST_TOOL_USE_FAILURE = "PostToolUseFailure"
    NOTIFICATION = "Notification"
    USER_PROMPT_SUBMIT = "UserPromptSubmit"
    SESSION_START = "SessionStart"
    SESSION_END = "SessionEnd"
    STOP = "Stop"
    SUBAGENT_START = "SubagentStart"
    SUBAGENT_STOP = "SubagentStop"
    PRE_COMPACT = "PreCompact"
    POST_COMPACT = "PostCompact"
    PERMISSION_REQUEST = "PermissionRequest"
    SETUP = "Setup"
    # ... 27 total in real source

# Each event type has a specific match field
MATCH_FIELD = {
    HookEvent.PRE_TOOL_USE: "tool_name",        # Match on tool name
    HookEvent.POST_TOOL_USE: "tool_name",
    HookEvent.NOTIFICATION: "notification_type",
    HookEvent.SESSION_START: "source",
    HookEvent.SUBAGENT_START: "agent_type",
    HookEvent.PRE_COMPACT: "trigger",
    # ...
}
```

### Four Hook Types

Hooks come in four flavors. The command type is the most common and the only
one that runs shell commands.

```python
# agents/s10_hooks.py -- mirrors HookCommandSchema in schemas/hooks.ts

class HookType(str, enum.Enum):
    COMMAND = "command"      # Shell command -- stdin JSON, stdout JSON
    PROMPT = "prompt"        # LLM prompt evaluation
    AGENT = "agent"          # Agentic verifier
    HTTP = "http"            # HTTP webhook

@dataclass
class HookDefinition:
    type: HookType
    command: Optional[str] = None       # For 'command' type
    prompt: Optional[str] = None        # For 'prompt' / 'agent' type
    url: Optional[str] = None           # For 'http' type
    timeout: Optional[float] = None     # Seconds (default 600)
    once: bool = False                  # Fire once then remove
    is_async: bool = False              # Run in background
```

### Matcher-Based Selective Firing

Hooks fire only when the event's match field matches the pattern. Patterns
support pipe-separated alternatives (`"Write|Edit"`) and glob matching.

```python
# agents/s10_hooks.py -- mirrors getMatchingHooks() in hooks.ts

@dataclass
class HookMatcher:
    matcher: Optional[str] = None  # Pattern (e.g., "Bash", "Write|Edit")
    hooks: list[HookDefinition] = field(default_factory=list)

def _get_matching_hooks(self, event, hook_input, session_id=None):
    match_key = MATCH_FIELD.get(event)
    match_query = hook_input.get(match_key, "") if match_key else None

    matched = []
    for matcher in self._config.get(event, []):
        # No matcher = fires for ALL tools
        if match_query is None or not matcher.matcher or \
           self._matches_pattern(match_query, matcher.matcher):
            matched.extend(matcher.hooks)
    return matched
```

### Shell Command Execution with JSON I/O

Command hooks receive JSON on stdin and produce JSON on stdout. The exit code
controls flow.

```python
# agents/s10_hooks.py -- mirrors executeHooks() in hooks.ts

def _execute_command_hook(self, hook, json_input) -> HookResult:
    timeout = hook.timeout or 600  # 10 min default

    proc = subprocess.run(
        hook.command,
        shell=True,
        input=json_input,      # JSON piped to stdin
        capture_output=True,
        text=True,
        timeout=timeout,
    )

    # Exit code protocol:
    # 0 = success (parse JSON from stdout)
    # 2 = blocking error (stderr shown to model, operation blocked)
    # other = non-blocking error (stderr shown to user only)
    if proc.returncode == 0:
        result.outcome = "success"
        self._parse_hook_output(result)  # Parse JSON from stdout
    elif proc.returncode == 2:
        result.outcome = "blocking"
        result.blocking_error = proc.stderr or proc.stdout
    else:
        result.outcome = "non_blocking_error"
```

### Structured JSON Output

Hooks produce structured JSON with permission decisions, context injection,
and input modification.

```python
# agents/s10_hooks.py -- mirrors parseHookOutput() in hooks.ts

def _parse_hook_output(self, result):
    data = json.loads(result.stdout)

    # Stop continuation
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

    # Hook-specific output
    specific = data.get("hookSpecificOutput", {})
    if specific.get("hookEventName") == "PreToolUse":
        perm = specific.get("permissionDecision")
        if perm == "allow":
            result.permission_behavior = "allow"
        elif perm == "deny":
            result.permission_behavior = "deny"
        if specific.get("updatedInput"):
            result.updated_input = specific["updatedInput"]  # Modify tool input!
        result.additional_context = specific.get("additionalContext")
```

### Session-Scoped Hooks

Skills can register hooks that live only for the duration of an agent session.
When a skill's frontmatter includes hook configuration, those hooks are
registered under the session ID and cleaned up when the session ends.

```python
# agents/s10_hooks.py -- mirrors sessionHooks.ts

class SessionHookStore:
    def __init__(self):
        self._hooks: dict[str, list[SessionHookEntry]] = {}

    def add(self, session_id: str, entry: SessionHookEntry):
        self._hooks.setdefault(session_id, []).append(entry)

    def get(self, session_id: str, event: HookEvent) -> list[SessionHookEntry]:
        return [h for h in self._hooks.get(session_id, []) if h.event == event]

    def clear(self, session_id: str):
        self._hooks.pop(session_id, None)
```

### Agent-Aware Event Mapping

When hooks are registered for a subagent, `Stop` events are automatically
remapped to `SubagentStop`. This prevents agent hooks from interfering with
the main session's stop behavior.

```python
# agents/s10_hooks.py -- mirrors registerFrontmatterHooks.ts

def register_frontmatter_hooks(self, session_id, hooks_settings, is_agent=False):
    for event_name, matchers in hooks_settings.items():
        event = HookEvent(event_name)
        # For agents: Stop -> SubagentStop
        if is_agent and event == HookEvent.STOP:
            event = HookEvent.SUBAGENT_STOP
        # ... register hooks under the remapped event
```

### Async Hook Registry

Non-blocking hooks run in the background and are polled for completion.

```python
# agents/s10_hooks.py -- mirrors AsyncHookRegistry.ts

class AsyncHookRegistry:
    def __init__(self):
        self._pending: dict[str, PendingAsyncHook] = {}

    def register(self, hook: PendingAsyncHook):
        self._pending[hook.process_id] = hook

    def check_responses(self) -> list[PendingAsyncHook]:
        """Poll completed hooks."""
        completed = [h for h in self._pending.values() if h.completed]
        for h in completed:
            del self._pending[h.process_id]
        return completed
```

## What Changed

| Component | Before | After |
|-----------|--------|-------|
| Extensibility | Fork the source | 27 hook event types, no source modification needed |
| Hook types | None | 4 types: command, prompt, agent, HTTP |
| Matching | All-or-nothing | Glob and pipe-separated patterns per event |
| Communication | String parsing | Structured JSON on stdin/stdout |
| Flow control | Cannot block operations | Exit code 2 blocks, code 0 passes |
| Input modification | Impossible | Hooks can modify tool input via `updatedInput` |
| Context injection | Impossible | Hooks inject `additionalContext` into model's view |
| Scope | Global only | Session-scoped hooks with automatic cleanup |
| Async support | None | Background hooks with polling registry |

## Try It

```bash
# Run the hooks demo
python agents/s10_hooks.py
```

The demo walks through:

1. **Safe Bash command** -- hook allows `git status` to proceed
2. **Dangerous Bash command** -- hook denies `rm -rf /` with a blocking error
3. **Audit hook** -- a matcherless hook fires for every tool call
4. **Post-tool context injection** -- adding "remember to run tests" after a Write
5. **Session-scoped hooks** -- registering a skill's hooks with Stop-to-SubagentStop mapping
6. **Async hook lifecycle** -- registering, completing, and polling a background hook

Try writing your own hook:

```bash
# Create a hook that logs every tool call to a file
cat > /tmp/audit-hook.sh << 'HOOK'
#!/bin/bash
# Read JSON from stdin, log it
cat /dev/stdin >> /tmp/claude-audit.log
echo '{}' # Success, no modifications
HOOK
chmod +x /tmp/audit-hook.sh
```

Then add it to your settings:

```json
{
  "hooks": {
    "PreToolUse": [
      {
        "hooks": [
          { "type": "command", "command": "/tmp/audit-hook.sh" }
        ]
      }
    ]
  }
}
```
