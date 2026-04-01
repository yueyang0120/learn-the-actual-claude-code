# s05: Permissions

`s01 > s02 > s03 > s04 > [ s05 ]`

> *"Rules from four sources, safety checks that survive bypass"* -- a multi-step pipeline where protected paths cannot be overridden.

## Problem

An agent that can run arbitrary bash commands and write to any file needs guardrails. But a simple allow/deny list is not enough. Rules come from multiple sources with different trust levels. Some paths (`.git/`, `.claude/`) must always prompt the user, even in "accept all" mode. And in auto-mode, a circuit breaker must catch runaway denials.

## Solution

```
  Tool call: Bash(command="git push --force")
                    |
  Step 1a:  whole-tool DENY rules?         -> DENY
  Step 1b:  whole-tool ASK rules?          -> ASK
  Step 1c:  content-specific DENY?         -> DENY
  Step 1f:  content-specific ASK?          -> ASK    (bypass-immune)
  Step 1g:  protected path safety?         -> ASK    (bypass-immune)
                    |
  Step 2a:  bypass mode?                   -> ALLOW
  Step 2b:  whole-tool ALLOW?              -> ALLOW
  Step 2c:  content-specific ALLOW?        -> ALLOW
                    |
  Step 3:   default -> ASK
            dontAsk mode?  -> ASK becomes DENY
            auto mode?     -> run classifier, track denials
```

Steps 1f and 1g run before the bypass check. That is the key design: safety checks cannot be overridden. Real code: `src/utils/permissions/permissions.ts` (~1,500 LOC).

## How It Works

**1. Every rule is a triple: source, behavior, value.**

```python
@dataclass
class PermissionRule:
    source: RuleSource    # userSettings, projectSettings, policySettings, ...
    behavior: Behavior    # ALLOW, DENY, ASK
    value: RuleValue      # tool_name="Bash", content="prefix:git "
```

Rules are parsed from strings like `Bash(prefix:git )` in settings.json. The parser handles escaped parentheses. Real code: `permissionRuleParser.ts`.

**2. Content matching supports prefix and exact patterns.**

```python
# settings.json
{"allow": ["Read", "Bash(prefix:git )", "Bash(npm test)"],
 "deny":  ["Bash(rm -rf /)"],
 "ask":   ["Bash(npm publish)"]}
```

`Bash(prefix:git )` matches any command starting with `git `. `Bash(npm test)` matches that exact command. Whole-tool rules like `Read` match regardless of input.

**3. Bash commands get classified by a safety heuristic.**

```python
def classify_bash_command(command: str) -> Behavior:
    if matches_dangerous(command):   # rm -rf, sudo, curl|sh, fork bomb
        return DENY
    if matches_safe(command):        # ls, cat, grep, git status
        return ALLOW
    return ASK                       # unknown -> prompt user
```

The real implementation (`yoloClassifier.ts`) uses a 2-stage AI classifier: a fast yes/no pass, then a chain-of-thought pass to reduce false positives.

**4. A circuit breaker catches runaway denials in auto mode.**

```python
@dataclass
class DenialTracker:
    consecutive: int = 0
    total: int = 0

    def should_fallback(self) -> bool:
        return self.consecutive >= 3 or self.total >= 20
```

After 3 consecutive denials (or 20 total), auto mode falls back to prompting the user instead of silently blocking. Real code: `denialTracking.ts`.

**5. The permission engine runs all steps in order.**

```python
def check_permission(self, tool_name, args):
    content = args.get("command") or args.get("file_path", "")

    # Step 1: deny/ask rules (bypass-immune ask rules checked here)
    for rule in self.deny_rules:
        if matches(tool_name, content, rule): return DENY
    for rule in self.ask_rules:
        if matches(tool_name, content, rule): return ASK

    # Protected paths -- bypass-immune
    if is_protected_path(args.get("file_path", "")):
        return ASK

    # Step 2: bypass mode allows everything remaining
    if self.mode == BYPASS: return ALLOW

    # Step 2b: allow rules
    for rule in self.allow_rules:
        if matches(tool_name, content, rule): return ALLOW

    # Step 3: default is ASK, transformed by mode
    if self.mode == DONT_ASK: return DENY
    if self.mode == AUTO:     return self.run_classifier(...)
    return ASK
```

Protected paths (`.git/`, `.claude/`, `.bashrc`) always require confirmation, even in bypass mode. MCP server-level deny rules match all tools from that server (`mcp__server` matches `mcp__server__any_tool`).

## What Changed

| Component | Before (s04) | After (s05) |
|---|---|---|
| Permission model | None (all tools allowed) | Rule triple: source + behavior + value |
| Rule sources | -- | 4+ sources with priority ordering |
| Matching | -- | Tool-level + content-level + prefix matching |
| Bash safety | -- | Classifier: dangerous / safe / unknown |
| Bypass immunity | -- | Protected paths and ask-rules survive bypass mode |
| Permission modes | -- | 6 modes (default, acceptEdits, bypass, dontAsk, plan, auto) |
| Auto-mode safety | -- | Denial tracker with circuit breaker |

## Try It

```bash
cd learn-the-actual-claude-code
python agents/s05_permissions.py
```

Example things to watch for:

- `Read` is allowed by user rule, `Agent` gets ASK (no rule matches)
- `Bash: rm -rf /` is denied by both rule and classifier
- In bypass mode, `.git/config` writes still require approval (safety check survives)
- In auto mode, the 4th consecutive denial triggers fallback to prompting
