# s10: Hooks

`s01 > s02 > s03 > s04 > s05 | s06 > s07 > s08 > s09 > [ s10 ] | s11 > s12 > s13 > s14`

> "The best extension system is one you never have to patch the source for."

## Problem

Every team has different rules. One blocks `rm -rf` in all Bash commands. Another audits every file write. A third auto-approves certain safe tool calls. You cannot bake all of these into the core product.

## Solution

Claude Code implements a hook system with event types, matcher-based selective firing, and shell commands that communicate via structured JSON. Hooks are configured in `.claude/settings.json`.

```
  .claude/settings.json               HookEngine
  +----------------------------+      +---------------------------+
  | "hooks": {                 |      | 1. Match event + tool     |
  |   "PreToolUse": [{         | ---> | 2. Pipe JSON to stdin     |
  |     "matcher": "Bash",     |      | 3. Parse JSON from stdout |
  |     "hooks": [{            |      | 4. Exit 0=pass, 2=block   |
  |       "type": "command",   |      | 5. Aggregate results      |
  |       "command": "check.sh"|      +---------------------------+
  |     }]                     |                |
  |   }]                       |                v
  | }                          |      Tool proceeds or is blocked
  +----------------------------+
```

## How It Works

### Step 1: Event types

27 event types in the real source. Each has a match field that determines what the matcher pattern is compared against. Source: `coreTypes.ts`.

```python
# agents/s10_hooks.py (simplified)

EVENTS = {
    "PreToolUse":   match_on="tool_name",
    "PostToolUse":  match_on="tool_name",
    "Notification": match_on="notification_type",
    "SessionStart": match_on="source",
    "SubagentStart":match_on="agent_type",
    "Stop":         match_on=None,   # fires for all
    # ... 27 total
}
```

### Step 2: Four hook types

Hooks come in four flavors. Command type is the most common. Source: `schemas/hooks.ts`.

```python
@dataclass
class HookDefinition:
    type: str       # "command" | "prompt" | "agent" | "http"
    command: str     # shell command (for command type)
    timeout: float   # seconds, default 600
    once: bool       # fire once then remove
    is_async: bool   # run in background
```

### Step 3: Matcher-based selective firing

Hooks fire only when the event's match field matches the pattern. Patterns support pipe-separated alternatives (`"Write|Edit"`) and glob matching. Source: `hooks.ts`.

```python
def get_matching_hooks(self, event, hook_input):
    match_key = MATCH_FIELD[event]
    match_query = hook_input.get(match_key)
    matched = []
    for matcher in self.config.get(event, []):
        if not matcher.pattern or matches(match_query, matcher.pattern):
            matched.extend(matcher.hooks)
    return matched
```

### Step 4: Shell command execution

Command hooks receive JSON on stdin and produce JSON on stdout. The exit code controls flow. Source: `hooks.ts`.

```python
def execute_command_hook(self, hook, json_input):
    proc = subprocess.run(hook.command, shell=True,
                          input=json_input, capture_output=True)
    if proc.returncode == 0:
        return parse_json_output(proc.stdout)   # success
    elif proc.returncode == 2:
        return blocking_error(proc.stderr)       # operation blocked
    else:
        return non_blocking_warning(proc.stderr)  # warning only
```

### Step 5: Structured JSON output

Hooks can do more than pass/block. They can approve or deny permissions, inject context into the model's view, and even modify tool input. Source: `hooks.ts:399`.

```python
# Hook stdout JSON can contain:
{
    "decision": "approve" | "block",
    "hookSpecificOutput": {
        "hookEventName": "PreToolUse",
        "permissionDecision": "allow" | "deny",
        "updatedInput": { ... },        # modify tool input!
        "additionalContext": "Remember to run tests."
    }
}
```

### Step 6: Session-scoped hooks

Skills can register hooks that live only for the duration of an agent session. For subagents, `Stop` events are automatically remapped to `SubagentStop`. Source: `sessionHooks.ts`.

## What Changed

| Component | Before (s09) | After (s10) |
|-----------|-------------|-------------|
| Extensibility | N/A | 27 hook event types, no source modification |
| Hook types | N/A | 4 types: command, prompt, agent, HTTP |
| Matching | N/A | Glob and pipe-separated patterns |
| Communication | N/A | Structured JSON on stdin/stdout |
| Flow control | N/A | Exit code 2 blocks, 0 passes |
| Input modification | N/A | Hooks can modify tool input via `updatedInput` |
| Scope | N/A | Session-scoped hooks with auto cleanup |

## Try It

```bash
cd learn-the-actual-claude-code
python agents/s10_hooks.py
```

The demo tests safe and dangerous Bash commands, audit hooks, post-tool context injection, session-scoped hooks, and the async hook registry.

Try writing your own hook:

```bash
cat > /tmp/audit-hook.sh << 'HOOK'
#!/bin/bash
cat /dev/stdin >> /tmp/claude-audit.log
echo '{}'
HOOK
chmod +x /tmp/audit-hook.sh
```

Then add it to `.claude/settings.json`:

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
