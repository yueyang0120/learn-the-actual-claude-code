# s05: Permissions

`s01 > s02 > s03 > s04 > [ s05 ]`

> *"Rules from four sources, safety checks that survive bypass"* -- 多步流水线, 受保护路径怎么也绕不过去。

## 问题

一个能跑任意 bash 命令、能写任意文件的 Agent 需要护栏。但简单的 allow/deny 列表不够。规则来自多个来源, 信任级别各不相同。某些路径（`.git/`, `.claude/`）即使在"全部接受"模式下也必须弹窗确认。自动模式下还需要熔断器来捕捉连续拒绝。

## 解决方案

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

Step 1f 和 1g 在 bypass 检查之前跑。这是关键设计：安全检查不可被覆盖。真实代码：`src/utils/permissions/permissions.ts`（~1,500 LOC）。

## 工作原理

**1. 每条规则是一个三元组：source, behavior, value。**

```python
@dataclass
class PermissionRule:
    source: RuleSource    # userSettings, projectSettings, policySettings, ...
    behavior: Behavior    # ALLOW, DENY, ASK
    value: RuleValue      # tool_name="Bash", content="prefix:git "
```

规则以 `Bash(prefix:git )` 这样的字符串存在 settings.json 里。解析器能处理转义括号。真实代码：`permissionRuleParser.ts`。

**2. 内容匹配支持 prefix 和 exact 两种模式。**

```python
# settings.json
{"allow": ["Read", "Bash(prefix:git )", "Bash(npm test)"],
 "deny":  ["Bash(rm -rf /)"],
 "ask":   ["Bash(npm publish)"]}
```

`Bash(prefix:git )` 匹配所有以 `git ` 开头的命令。`Bash(npm test)` 精确匹配。像 `Read` 这样的整工具规则匹配任何输入。

**3. Bash 命令用安全启发式做分类。**

```python
def classify_bash_command(command: str) -> Behavior:
    if matches_dangerous(command):   # rm -rf, sudo, curl|sh, fork bomb
        return DENY
    if matches_safe(command):        # ls, cat, grep, git status
        return ALLOW
    return ASK                       # unknown -> prompt user
```

真实实现（`yoloClassifier.ts`）用两阶段 AI 分类器：先快速 yes/no 判断, 再 chain-of-thought 降低误判。

**4. 熔断器捕捉自动模式下的连续拒绝。**

```python
@dataclass
class DenialTracker:
    consecutive: int = 0
    total: int = 0

    def should_fallback(self) -> bool:
        return self.consecutive >= 3 or self.total >= 20
```

连续 3 次拒绝（或总共 20 次）后, 自动模式回退到弹窗问用户, 而不是继续静默拦截。真实代码：`denialTracking.ts`。

**5. 权限引擎按顺序跑完所有步骤。**

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

受保护路径（`.git/`, `.claude/`, `.bashrc`）始终要确认, 即使在 bypass 模式下。MCP 服务器级 deny 规则匹配该服务器的所有工具（`mcp__server` 匹配 `mcp__server__any_tool`）。

## 变更内容

| 组件 | 之前 (s04) | 之后 (s05) |
|---|---|---|
| 权限模型 | 无（所有工具直接放行） | 规则三元组：source + behavior + value |
| 规则来源 | -- | 4+ 个来源, 按优先级排序 |
| 匹配方式 | -- | 工具级 + 内容级 + prefix 匹配 |
| Bash 安全性 | -- | 分类器：dangerous / safe / unknown |
| bypass 免疫 | -- | 受保护路径和 ask 规则在 bypass 模式下依然生效 |
| 权限模式 | -- | 6 种（default, acceptEdits, bypass, dontAsk, plan, auto） |
| 自动模式安全 | -- | 拒绝追踪 + 熔断器 |

## 试一试

```bash
cd learn-the-actual-claude-code
python agents/s05_permissions.py
```

注意观察：

- `Read` 被用户规则放行, `Agent` 得到 ASK（没有规则匹配）
- `Bash: rm -rf /` 被规则和分类器同时拒绝
- bypass 模式下, 写 `.git/config` 仍然要确认（安全检查不可绕过）
- 自动模式下, 第 4 次连续拒绝触发回退到弹窗
