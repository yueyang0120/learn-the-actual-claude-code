# Session 05 -- 权限系统

`s01 > s02 > s03 > s04 > [ s05 ] | s06 > s07 > s08 > s09 > s10 | s11 > s12 > s13 > s14`

> "PermissionRule triple (source, behavior, value), 6 permission modes, bash XML classifier, denial tracking with circuit breaker."
> "PermissionRule 三元组（source、behavior、value），6 种权限模式，bash XML 分类器，带熔断器的拒绝追踪。"
>
> *实践层：`agents/s05_permissions.py` 用约 570 行 Python 重新实现了完整的权限流水线 —— 规则解析、模式处理、bash 分类、拒绝追踪和多步权限检查流程。*

---

## 问题

一个能运行任意 bash 命令和写入任意文件的 Agent 需要安全护栏。但简单的允许/拒绝列表远远不够：

- **规则来自 4+ 个来源**，信任级别各不相同（用户设置、项目设置、管理策略、会话覆盖）。
- **某些规则不可绕过** —— 即使在"全部接受"模式下，写入 `.git/config` 仍然应该需要确认。
- **Bash 命令需要语义分类** —— `ls -la` 是安全的，`rm -rf /` 不是，而 `curl ... | sh` 的危险性是简单允许列表无法捕捉的。
- **自动模式需要熔断器** —— 如果 AI 分类器持续拒绝命令，系统应该回退到提示用户，而不是静默地阻止一切。
- **6 种不同的权限模式**改变了流水线对模糊情况的处理方式。

---

## 解决方案

权限系统是一个**多步流水线**，按优先级顺序评估规则，并包含不可绕过的安全检查：

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

## 工作原理

### 1. PermissionRule 三元组

每条规则有三个组成部分：它来自哪里、它做什么、它匹配什么：

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

### 2. 规则字符串解析器

规则以 `Bash(prefix:git )` 这样的字符串形式存储在 settings.json 中。解析器能处理转义的括号：

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

settings.json 中类似这样的条目：

```json
{
  "permissions": {
    "allow": ["Read", "Bash(prefix:git )", "Bash(npm test)"],
    "deny": ["Bash(rm -rf /)"],
    "ask": ["Bash(npm publish)"]
  }
}
```

...会生成带有解析后的 `tool_name` 和 `content` 字段的 PermissionRule 对象。

### 3. Bash 命令分类器

真实代码使用两阶段 XML AI 分类器（`yoloClassifier.ts`）。重新实现使用正则表达式启发式方法，捕捉相同的类别：

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

### 4. 带熔断器的拒绝追踪

在自动模式下，AI 分类器可能会拒绝命令。如果它持续拒绝，系统会回退到询问用户：

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

### 5. 权限引擎流水线

主 `check_permission()` 方法遵循真实代码中 `hasPermissionsToUseToolInner()` 的步骤编号：

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

关键设计要点：
- **步骤 1f 和 1g 不可绕过** —— 它们在步骤 2a 的绕过模式检查之前执行。
- **MCP 服务器级匹配**：规则 `mcp__server1` 匹配工具 `mcp__server1__any_tool`。
- **受保护路径**（`.git/`、`.claude/`、`.bashrc`）始终需要确认。

### 6. 配置加载

规则从与 `settings.json` 相同的 JSON 结构中加载：

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

## 变化对比

| 组件 | 之前（教程风格） | 之后（Claude Code） |
|---|---|---|
| 权限模型 | 单一允许/拒绝列表 | PermissionRule 三元组（source、behavior、value） |
| 规则来源 | 单个配置文件 | 4+ 个来源，按优先级排序 |
| 匹配方式 | 精确工具名 | 工具级 + 内容级 + 前缀匹配 |
| Bash 安全性 | 无 | 两阶段分类器（dangerous/safe/unknown） |
| 绕过免疫 | 无 | 受保护路径和 ask 规则在绕过模式下依然生效 |
| 权限模式 | 允许或拒绝 | 6 种模式（default、acceptEdits、bypass、dontAsk、plan、auto） |
| 自动模式安全性 | 不适用 | 带熔断器的拒绝追踪（连续 3 次 / 总计 20 次） |
| MCP 工具 | 不适用 | 服务器级拒绝规则（`mcp__server` 匹配所有工具） |

---

## 试一试

```bash
cd agents
python s05_permissions.py
```

演示在所有三种模式下运行测试用例：

**默认模式** —— 规则严格评估：
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

**绕过模式** —— 大部分操作被允许，但安全检查依然生效：
```
[+] ALLOW  Bash in bypass mode
[?] ASK    Write .git/ in bypass (still protected!)
```

**拒绝追踪** —— 自动模式下的熔断器：
```
Attempt 1: [X] DENY   (consecutive=1)
Attempt 2: [X] DENY   (consecutive=2)
Attempt 3: [X] DENY   (consecutive=3)
Attempt 4: [?] ASK    Denial limit exceeded -- falling back to prompting
```

**接下来可以探索的源文件：**
- `src/utils/permissions/permissions.ts` -- 完整的权限流水线 (1,486 LOC)
- `src/types/permissions.ts` -- 核心类型（PermissionRule、PermissionMode）
- `src/utils/permissions/permissionRuleParser.ts` -- 规则字符串解析
- `src/utils/permissions/denialTracking.ts` -- 熔断器逻辑
- `src/utils/permissions/yoloClassifier.ts` -- 两阶段 AI bash 分类器
