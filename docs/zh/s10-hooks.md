# Session 10 -- Hooks：生命周期可扩展性

s01 > s02 > s03 > s04 > s05 | s06 > s07 > s08 > s09 > **s10** | s11 > s12 > s13 > s14

---

> *"The best extension system is one you never have to patch the source for."*
> *"最好的扩展系统是让你永远不需要修改源代码的系统。"*
>
> **Harness 层**: 本节涵盖 hooks 系统——让用户在 27 个生命周期点注入自定义行为
> 而无需修改 Claude Code 核心源代码的可扩展性机制。Hooks 是"Claude Code 做什么"
> 和"你需要它做什么"之间的接缝。

---

## 问题

每个团队都有不同的规则。一个团队要在所有 Bash 命令中阻止 `rm -rf`。另一个团队
想要审计每次文件写入。第三个团队需要自动批准某些安全的 tool 调用。你无法把所有
这些都内置到核心产品中——组合是无穷无尽的。

你需要一个系统来：

- 在明确定义的生命周期点触发（tool 使用前、tool 使用后等）
- 选择性匹配（仅针对特定 tools、通知类型等）
- 通过结构化 JSON 通信（而非脆弱的字符串解析）
- 使用退出码控制流程（0 = 成功，2 = 阻止，其他 = 警告）
- 支持异步执行以进行非阻塞检查
- 可以限定到某个 session（用于 skill 专属 hooks）

## 解决方案

Claude Code 实现了一个 hook 系统，包含 **27 种事件类型**、**4 种 hook 类型**
（command、prompt、agent、HTTP）和 **基于匹配器的选择性触发**。Hooks 在
`.claude/settings.json` 中配置，作为 shell 命令执行，通过 stdin 接收 JSON
并在 stdout 上产生 JSON。

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

## 工作原理

### Hook 事件类型

系统定义了 27 种事件类型。每个事件有一个匹配字段，决定匹配器模式与什么进行比较。

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

### 四种 Hook 类型

Hooks 有四种形式。command 类型最常见，也是唯一运行 shell 命令的类型。

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

### 基于匹配器的选择性触发

Hooks 仅在事件的匹配字段与模式匹配时触发。模式支持管道符分隔的多选
（`"Write|Edit"`）和 glob 匹配。

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

### Shell 命令执行与 JSON I/O

Command hooks 通过 stdin 接收 JSON，在 stdout 上产生 JSON。退出码控制流程。

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

### 结构化 JSON 输出

Hooks 产生结构化 JSON，包含权限决策、上下文注入和输入修改。

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

### Session 作用域的 Hooks

Skills 可以注册仅在 agent session 持续期间存活的 hooks。当 skill 的 frontmatter
包含 hook 配置时，这些 hooks 在该 session ID 下注册，并在 session 结束时清理。

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

### Agent 感知的事件映射

当 hooks 为子 agent 注册时，`Stop` 事件会自动重映射为 `SubagentStop`。这可以
防止 agent hooks 干扰主 session 的停止行为。

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

### 异步 Hook 注册表

非阻塞 hooks 在后台运行，通过轮询检查完成状态。

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

## 变化对比

| 组件 | 之前 | 之后 |
|------|------|------|
| 可扩展性 | 需要 fork 源代码 | 27 种 hook 事件类型，无需修改源代码 |
| Hook 类型 | 无 | 4 种类型：command、prompt、agent、HTTP |
| 匹配机制 | 全有或全无 | 每个事件支持 glob 和管道符分隔的模式 |
| 通信方式 | 字符串解析 | stdin/stdout 上的结构化 JSON |
| 流程控制 | 无法阻止操作 | 退出码 2 阻止，退出码 0 放行 |
| 输入修改 | 不可能 | Hooks 可以通过 `updatedInput` 修改 tool 输入 |
| 上下文注入 | 不可能 | Hooks 向模型视图注入 `additionalContext` |
| 作用域 | 仅全局 | Session 作用域的 hooks，带自动清理 |
| 异步支持 | 无 | 后台 hooks，带轮询注册表 |

## 试一试

```bash
# Run the hooks demo
python agents/s10_hooks.py
```

演示逐步展示：

1. **安全的 Bash 命令** -- hook 允许 `git status` 继续执行
2. **危险的 Bash 命令** -- hook 用阻塞错误拒绝 `rm -rf /`
3. **审计 hook** -- 一个无匹配器的 hook 对每次 tool 调用都触发
4. **tool 后上下文注入** -- 在 Write 之后添加"记得运行测试"
5. **Session 作用域的 hooks** -- 注册 skill 的 hooks，带 Stop 到 SubagentStop 的映射
6. **异步 hook 生命周期** -- 注册、完成和轮询一个后台 hook

试着编写你自己的 hook：

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

然后将其添加到你的设置中：

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
