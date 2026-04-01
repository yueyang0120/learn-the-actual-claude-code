# Session 05 -- Permission System

`s01 > s02 > s03 > s04 > [ s05 ] | s06 > s07 > s08 > s09 > s10 | s11 > s12 > s13 > s14`

> "PermissionRule triple (source, behavior, value), 6 permission modes, bash XML classifier, denial tracking with circuit breaker."
>
> *Harness layer: `agents/s05_permissions.py` reimplements the full permission pipeline in ~570 lines of Python -- rule parsing, mode handling, bash classification, denial tracking, and the multi-step permission check flow.*

---

## Problem

An agent that can run arbitrary bash commands and write to any file needs guardrails. But a simple allow/deny list is not enough:

- **Rules come from 4+ sources** with different trust levels (user settings, project settings, admin policy, session overrides).
- **Some rules are bypass-immune** -- even in "accept all" mode, writing to `.git/config` should still require confirmation.
- **Bash commands need semantic classification** -- `ls -la` is safe, `rm -rf /` is not, and `curl ... | sh` is dangerous in ways a simple allowlist cannot capture.
- **Auto-mode needs a circuit breaker** -- if the AI classifier keeps denying commands, the system should fall back to prompting the user rather than silently blocking everything.
- **6 different permission modes** change how the pipeline resolves ambiguous cases.

---

## Solution

The permission system is a **multi-step pipeline** that evaluates rules in priority order, with safety checks that cannot be bypassed:

```
  Tool call arrives: Bash(command="git push --force")
                          |
       +------------------+------------------+
       |                                     |
  Step 1a: Whole-tool DENY rules?       NO   |
  Step 1b: Whole-tool ASK rules?        NO   |
  Step 1c: Content-specific DENY?       NO   |
  Step 1f: Content-specific ASK?        NO   |
  Step 1g: Protected path safety?       NO   |
       |                                     |
  Step 2a: bypassPermissions mode?  ----YES---> ALLOW
       |  NO                                 |
  Step 2b: Whole-tool ALLOW rule?       NO   |
       |                                     |
  Step 3: Default -> ASK                     |
       |                                     |
  Mode transform:                            |
    dontAsk -> ASK becomes DENY              |
    auto    -> run AI classifier             |
              deny? -> track denial          |
              3 consecutive? -> fallback ASK |
```

---

## How It Works

### 1. The PermissionRule triple

Every rule has three components: where it came from, what it does, and what it matches:

```python
class Behavior(Enum):
    ALLOW = "allow"
    DENY = "deny"
    ASK = "ask"


class RuleSource(Enum):
    USER = "userSettings"        # ~/.claude/settings.json
    PROJECT = "projectSettings"  # .claude/settings.json
    LOCAL = "localSettings"      # .claude/settings.local.json
    POLICY = "policySettings"    # /etc/claude-code/settings.json
    CLI_ARG = "cliArg"           # --allowedTools flag
    SESSION = "session"          # runtime user approvals


@dataclass
class PermissionRuleValue:
    """Parsed from 'Bash(npm install)' -> tool_name='Bash', content='npm install'."""
    tool_name: str
    content: Optional[str] = None


@dataclass
class PermissionRule:
    source: RuleSource
    behavior: Behavior
    value: PermissionRuleValue
```

### 2. Rule string parser

Rules are stored as strings like `Bash(prefix:git )` in settings.json. The parser handles escaped parentheses:

```python
def parse_rule_string(rule_str: str) -> PermissionRuleValue:
    """Parse 'Tool(content)' format. Handles escaped parentheses.
    Ref: permissionRuleValueFromString in permissionRuleParser.ts
    """
    match = re.search(r'(?<!\\)\(', rule_str)
    if not match:
        return PermissionRuleValue(tool_name=rule_str)

    open_idx = match.start()
    # Find last unescaped ')'
    close_idx = -1
    for i in range(len(rule_str) - 1, open_idx, -1):
        if rule_str[i] == ')':
            num_bs = 0
            j = i - 1
            while j >= 0 and rule_str[j] == '\\':
                num_bs += 1
                j -= 1
            if num_bs % 2 == 0:
                close_idx = i
                break

    tool_name = rule_str[:open_idx]
    raw_content = rule_str[open_idx + 1:close_idx]
    content = raw_content.replace('\\(', '(').replace('\\)', ')')
    return PermissionRuleValue(tool_name=tool_name, content=content)
```

A settings.json entry like this:

```json
{
  "permissions": {
    "allow": ["Read", "Bash(prefix:git )", "Bash(npm test)"],
    "deny": ["Bash(rm -rf /)"],
    "ask": ["Bash(npm publish)"]
  }
}
```

...produces PermissionRule objects with parsed `tool_name` and `content` fields.

### 3. Bash command classifier

The real code uses a 2-stage XML AI classifier (`yoloClassifier.ts`). The reimplementation uses regex heuristics that capture the same categories:

```python
DANGEROUS_PATTERNS = [
    r'\brm\s+(-[a-zA-Z]*f[a-zA-Z]*\s+)?-[a-zA-Z]*r',  # rm -rf
    r'\bsudo\b',
    r'\bchmod\s+777\b',
    r'\bmkfs\b',
    r'\bcurl\b.*\|\s*(ba)?sh',          # curl | bash
    r':\(\)\s*\{\s*:\|:&\s*\}\s*;',     # fork bomb
    r'\bshutdown\b',
    r'\breboot\b',
]

SAFE_PATTERNS = [
    r'^ls(\s|$)',
    r'^cat\s',
    r'^git\s+(status|log|diff|branch)',
    r'^npm\s+(install|test|run|list)',
    r'^grep\s',
    r'^find\s',
]


def classify_bash_command(command: str) -> Behavior:
    """Simplified classifier.
    Real implementation uses 2-stage XML classifier:
      Stage 1 (fast): <block>yes/no</block>
      Stage 2 (thinking): chain-of-thought to reduce false positives
    """
    for pattern in DANGEROUS_PATTERNS:
        if re.search(pattern, command.strip()):
            return Behavior.DENY

    for pattern in SAFE_PATTERNS:
        if re.search(pattern, command.strip()):
            return Behavior.ALLOW

    return Behavior.ASK
```

### 4. Denial tracking with circuit breaker

In auto mode, the AI classifier can deny commands. If it keeps denying, the system falls back to asking the user:

```python
MAX_CONSECUTIVE_DENIALS = 3
MAX_TOTAL_DENIALS = 20


@dataclass
class DenialTracker:
    """Circuit breaker for auto-mode denials."""
    consecutive: int = 0
    total: int = 0

    def record_denial(self):
        self.consecutive += 1
        self.total += 1

    def record_success(self):
        self.consecutive = 0

    def should_fallback(self) -> bool:
        return (self.consecutive >= MAX_CONSECUTIVE_DENIALS or
                self.total >= MAX_TOTAL_DENIALS)
```

### 5. The permission engine pipeline

The main `check_permission()` method follows the real code's step numbering from `hasPermissionsToUseToolInner()`:

```python
@dataclass
class PermissionEngine:
    rules: list[PermissionRule] = field(default_factory=list)
    mode: PermissionMode = PermissionMode.DEFAULT
    denial_tracker: DenialTracker = field(default_factory=DenialTracker)

    def check_permission(self, tool_name, args=None) -> PermissionDecision:
        args = args or {}
        content = args.get('command', '') or args.get('file_path', '')

        # Step 1a: Entire tool denied by rule
        for rule in self._rules_for(Behavior.DENY):
            if self._tool_matches_rule(tool_name, rule):
                return PermissionDecision(Behavior.DENY, ...)

        # Step 1b: Entire tool has ask rule
        for rule in self._rules_for(Behavior.ASK):
            if self._tool_matches_rule(tool_name, rule):
                return PermissionDecision(Behavior.ASK, ...)

        # Step 1c: Content-specific deny rules
        for rule in self._rules_for(Behavior.DENY):
            if self._content_matches(tool_name, content, rule):
                return PermissionDecision(Behavior.DENY, ...)

        # Step 1f: Content-specific ask rules (BYPASS-IMMUNE)
        for rule in self._rules_for(Behavior.ASK):
            if self._content_matches(tool_name, content, rule):
                return PermissionDecision(Behavior.ASK, ...)

        # Bash-specific classification
        if tool_name == 'Bash' and content:
            if classify_bash_command(content) == Behavior.DENY:
                return PermissionDecision(Behavior.DENY, ...)

        # Step 1g: Safety checks (BYPASS-IMMUNE)
        if args.get('file_path') and self._is_protected_path(args['file_path']):
            return PermissionDecision(Behavior.ASK, ...)

        # Step 2a: bypassPermissions mode
        if self.mode == PermissionMode.BYPASS:
            return PermissionDecision(Behavior.ALLOW, ...)

        # Step 2b: Whole-tool allow rules
        for rule in self._rules_for(Behavior.ALLOW):
            if self._tool_matches_rule(tool_name, rule):
                return PermissionDecision(Behavior.ALLOW, ...)

        # Content-specific allow rules
        for rule in self._rules_for(Behavior.ALLOW):
            if self._content_matches(tool_name, content, rule):
                return PermissionDecision(Behavior.ALLOW, ...)

        # Step 3: Mode transforms on default ASK
        if self.mode == PermissionMode.DONT_ASK:
            return PermissionDecision(Behavior.DENY, ...)

        if self.mode == PermissionMode.AUTO:
            # Run classifier, track denials, circuit-break
            ...

        return PermissionDecision(Behavior.ASK, ...)
```

Key design points:
- **Steps 1f and 1g are bypass-immune** -- they run before the bypass mode check at step 2a.
- **MCP server-level matching**: rule `mcp__server1` matches tool `mcp__server1__any_tool`.
- **Protected paths** (`.git/`, `.claude/`, `.bashrc`) always require confirmation.

### 6. Config loading

Rules load from the same JSON structure used by `settings.json`:

```python
SAMPLE_CONFIG = {
    "mode": "default",
    "rules": {
        "userSettings": {
            "allow": ["Read", "Bash(prefix:git )", "Bash(npm test)"],
            "deny":  ["Bash(rm -rf /)"],
            "ask":   ["Bash(npm publish)"],
        },
        "projectSettings": {
            "allow": ["Write", "Bash(prefix:python3 )"],
            "deny":  ["mcp__untrusted_server"],
        },
    },
}
```

---

## What Changed

| Component | Before (tutorial style) | After (Claude Code) |
|---|---|---|
| Permission model | Single allow/deny list | PermissionRule triple (source, behavior, value) |
| Rule sources | One config file | 4+ sources with priority ordering |
| Matching | Exact tool name | Tool-level + content-level + prefix matching |
| Bash safety | None | 2-stage classifier (dangerous/safe/unknown) |
| Bypass immunity | None | Protected paths and ask-rules survive bypass mode |
| Permission modes | Allow or deny | 6 modes (default, acceptEdits, bypass, dontAsk, plan, auto) |
| Auto-mode safety | N/A | Denial tracker with circuit breaker (3 consecutive / 20 total) |
| MCP tools | N/A | Server-level deny rules (`mcp__server` matches all tools) |

---

## Try It

```bash
cd agents
python s05_permissions.py
```

The demo runs test cases across all three modes:

**Default mode** -- rules evaluated strictly:
```
[+] ALLOW  Read tool (allowed by user rule)
[+] ALLOW  Write to normal file (allowed by project rule)
[?] ASK    Write to .git/ (safety check -- bypass-immune)
[+] ALLOW  Bash: git status (prefix allow rule)
[X] DENY   Bash: rm -rf / (deny rule + classifier)
[X] DENY   Bash: curl pipe to sh (dangerous)
[?] ASK    Agent tool (no rule -> ASK)
[X] DENY   MCP tool from denied server
```

**Bypass mode** -- most things allowed, but safety checks survive:
```
[+] ALLOW  Bash in bypass mode
[?] ASK    Write .git/ in bypass (still protected!)
```

**Denial tracking** -- auto mode with circuit breaker:
```
Attempt 1: [X] DENY   (consecutive=1)
Attempt 2: [X] DENY   (consecutive=2)
Attempt 3: [X] DENY   (consecutive=3)
Attempt 4: [?] ASK    Denial limit exceeded -- falling back to prompting
```

**Source files to explore next:**
- `src/utils/permissions/permissions.ts` -- the full pipeline (1,486 LOC)
- `src/types/permissions.ts` -- core types (PermissionRule, PermissionMode)
- `src/utils/permissions/permissionRuleParser.ts` -- rule string parsing
- `src/utils/permissions/denialTracking.ts` -- circuit breaker logic
- `src/utils/permissions/yoloClassifier.ts` -- the 2-stage AI bash classifier
