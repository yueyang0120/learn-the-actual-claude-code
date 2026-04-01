# s10: Hooks

`s01 > s02 > s03 > s04 > s05 | s06 > s07 > s08 > s09 > [ s10 ] | s11 > s12 > s13 > s14`

> "最好的扩展系统，就是永远不需要你去改源代码。"

## 问题

每个团队有不同的规矩。一个团队要拦截所有 Bash 命令里的 `rm -rf`。另一个要审计每次文件写入。还有一个要自动批准某些安全的 tool 调用。你没法把所有这些都塞进核心产品里。

## 解决方案

Claude Code 实现了一个 hook 系统 -- 有事件类型、基于 matcher 的选择性触发、通过结构化 JSON 通信的 shell 命令。Hooks 配置在 `.claude/settings.json` 里。

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

## 工作原理

### 第 1 步：事件类型

真实源码里有 27 种事件类型。每种都有一个 match field，决定 matcher pattern 跟什么比较。源码参考：`coreTypes.ts`。

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

### 第 2 步：四种 hook 类型

Hooks 有四种形式。command 类型最常用。源码参考：`schemas/hooks.ts`。

```python
@dataclass
class HookDefinition:
    type: str       # "command" | "prompt" | "agent" | "http"
    command: str     # shell command (for command type)
    timeout: float   # seconds, default 600
    once: bool       # fire once then remove
    is_async: bool   # run in background
```

### 第 3 步：基于 matcher 的选择性触发

Hooks 只在事件的 match field 匹配 pattern 时才触发。pattern 支持管道符分隔（`"Write|Edit"`）和 glob 匹配。源码参考：`hooks.ts`。

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

### 第 4 步：Shell 命令执行

Command hooks 通过 stdin 接收 JSON，通过 stdout 输出 JSON。退出码控制流程。源码参考：`hooks.ts`。

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

### 第 5 步：结构化 JSON 输出

Hooks 不止能 pass/block。它们可以批准或拒绝权限，注入上下文给模型看，甚至修改 tool 输入。源码参考：`hooks.ts:399`。

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

### 第 6 步：Session 作用域的 hooks

Skills 可以注册只在 agent session 存活期间有效的 hooks。对子 agent 来说，`Stop` 事件会自动映射成 `SubagentStop`。源码参考：`sessionHooks.ts`。

## 变更内容

| 组件 | 之前 (s09) | 之后 (s10) |
|------|-----------|-----------|
| 可扩展性 | 不存在 | 27 种 hook 事件类型，不用改源码 |
| Hook 类型 | 不存在 | 4 种：command、prompt、agent、HTTP |
| 匹配机制 | 不存在 | glob 和管道符分隔的 pattern |
| 通信方式 | 不存在 | stdin/stdout 上的结构化 JSON |
| 流程控制 | 不存在 | 退出码 2 阻止，0 放行 |
| 输入修改 | 不存在 | hooks 可以通过 `updatedInput` 改 tool 输入 |
| 作用域 | 不存在 | session 作用域 hooks，自动清理 |

## 试一试

```bash
cd learn-the-actual-claude-code
python agents/s10_hooks.py
```

演示测试安全和危险的 Bash 命令、审计 hook、tool 后上下文注入、session 作用域 hooks、异步 hook 注册表。

试着写一个你自己的 hook：

```bash
cat > /tmp/audit-hook.sh << 'HOOK'
#!/bin/bash
cat /dev/stdin >> /tmp/claude-audit.log
echo '{}'
HOOK
chmod +x /tmp/audit-hook.sh
```

然后加到 `.claude/settings.json` 里：

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
