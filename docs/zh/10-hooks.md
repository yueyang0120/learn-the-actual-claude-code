# 第 10 章: Hooks

前面的章节描述了 Claude Code 内部控制的系统 -- tool, 权限, 上下文管理。但每个组织都有核心产品无法预见的策略: 某团队阻止破坏性 shell 命令, 另一个审计每次文件写入, 第三个在特定 tool call 前注入领域上下文。Hook 提供扩展面。它允许外部代码在 27 个定义好的事件点上观察, 修改或阻止 agent 行为, 全部通过设置文件声明式配置, 无需修改源码。

## 问题

通用 agent 无法编码每个组织的规则。硬编码 "阻止 `rm -rf /`" 到权限系统处理了一个场景, 但忽略了团队需要执行的成千上万其他策略。扩展机制必须同时满足三个要求。

第一, 选择性。一个对每个 tool call 都触发的 hook 产生噪音。系统需要基于模式匹配, 使只针对 `Bash` tool call 的 hook 不会在 agent 读文件时触发。

第二, 可组合。多个 hook 可能适用于同一事件。审计 hook 应记录调用, 安全 hook 应检查命令, 上下文 hook 应注入额外指令 -- 各自独立, 按顺序执行。

第三, 结构化数据通信。简单的通过/失败退出码不够, hook 需要修改 tool 输入, 向对话注入上下文, 或做权限决策。协议必须支持丰富响应, 同时简单到可以用 shell 脚本实现。

## Claude Code 的解法

### 事件类型与匹配字段

Claude Code 定义了 27 种 hook 事件类型。每种事件指定一个匹配字段 -- matcher 模式比较的属性。`PreToolUse` 匹配 tool name, `Notification` 匹配 notification type, `SessionStart` 匹配 source, `Stop` 无条件触发 (无匹配字段):

```typescript
// src/hooks/coreTypes.ts
type HookEventName =
  | "PreToolUse"        // match: tool_name
  | "PostToolUse"       // match: tool_name
  | "PostToolUseFailure"// match: tool_name
  | "SessionStart"      // match: source
  | "SessionEnd"        // match: source
  | "Stop"              // match: none (全部触发)
  | "SubagentStart"     // match: agent_type
  | "PreCompact"        // match: none
  | "PostCompact"       // match: none
  | "Notification"      // match: notification_type
  | "FileChanged"       // match: file_path
  // ... 共 27 种
```

匹配字段设计意味着 hook 作者不需要在脚本内部写过滤逻辑。引擎在调用前完成匹配。

### 四种 Hook 类型

Hook 分四种, 适用于不同场景。`command` 类型最常见, 生成 shell 子进程。`prompt` 类型向模型上下文注入文本。`http` 类型发送 webhook。`agent` 类型委托给子 agent:

```typescript
// src/schemas/hooks.ts
interface HookDefinition {
  type: "command" | "prompt" | "http" | "agent";
  command?: string;     // shell 命令 (command 类型)
  prompt?: string;      // 注入文本 (prompt 类型)
  url?: string;         // 端点 (http 类型)
  timeout?: number;     // 秒, 默认 600
  once?: boolean;       // 触发一次后自动移除
}
```

`once` 标志支持一次性 hook, 用于初始化工作 -- 创建临时文件, 注册资源 -- 不应重复执行。

### 基于 Matcher 的选择性触发

`settings.json` 中的 hook 配置使用两层结构: matcher 包裹一个或多个 hook 定义。Matcher 的 `pattern` 字段支持管道分隔的备选项和 glob 语法。可选的 `if` 条件在匹配字段之外提供额外过滤:

```typescript
// .claude/settings.json
{
  "hooks": {
    "PreToolUse": [
      {
        "matcher": "Bash|Write|Edit",  // 管道分隔备选项
        "if": "command contains 'rm'", // 额外过滤
        "hooks": [
          { "type": "command", "command": "/scripts/safety-check.sh" }
        ]
      }
    ]
  }
}
```

引擎的 `getMatchingHooks()` 函数根据事件的匹配字段评估 matcher。只有模式匹配 (或模式缺失, 意味着 "全部匹配") 的 hook 进入执行:

```typescript
// src/hooks/hooks.ts
function getMatchingHooks(
  config: HookConfig,
  event: HookEventName,
  input: HookInput
): HookDefinition[] {
  const matchField = MATCH_FIELDS[event];
  const matchValue = matchField ? input[matchField] : undefined;
  const matchers = config[event] ?? [];
  return matchers
    .filter(m => !m.pattern || matchesPattern(matchValue, m.pattern))
    .flatMap(m => m.hooks);
}
```

### 执行引擎与退出码协议

核心 `executeHooks()` 函数编排完整生命周期: 收集匹配 hook, 逐个执行, 聚合结果。对 `command` hook, 执行意味着生成子进程, stdin 写入 JSON, 从 stdout 解析 JSON。退出码承载语义:

```typescript
// src/hooks/hooks.ts
async function executeCommandHook(
  hook: HookDefinition,
  jsonInput: string
): Promise<HookResult> {
  const proc = spawn(hook.command, { shell: true });
  proc.stdin.write(jsonInput);
  proc.stdin.end();

  const stdout = await collectStream(proc.stdout);
  const exitCode = await waitForExit(proc);

  if (exitCode === 0) return parseSuccess(stdout);
  if (exitCode === 2) return blockingError(stderr);
  return nonBlockingWarning(stderr); // 其他退出码
}
```

退出码 0: hook 成功, 操作继续。退出码 2: hook 主动阻止操作 -- tool call 不会执行。其他退出码: 产生非阻塞警告, 操作继续但警告出现在 agent 上下文中。

### 结构化 JSON 输入/输出

写入 stdin 的 JSON 包含所有事件通用的基础字段 (session ID, 事件名, 时间戳) 加事件特定字段 (tool name, tool input, file path)。stdout 上的 JSON 可携带远超通过/失败的丰富响应:

```typescript
// Hook stdout 响应结构
interface HookResponse {
  decision?: "approve" | "block";
  hookSpecificOutput?: {
    hookEventName: string;
    permissionDecision?: "allow" | "deny";
    updatedInput?: Record<string, unknown>;  // 修改 tool 输入
    additionalContext?: string;               // 注入对话
  };
}
```

`updatedInput` 字段尤其有用: `PreToolUse` hook 可以在执行前改写 tool 的输入。例如, 监视 `Bash` 调用的 hook 可以在每条命令前追加 `set -euo pipefail`。

### Session 范围与异步 Hook

除静态配置外, hook 可在 session 期间动态注册。`addSessionHook` 注册 command 或 prompt hook; `addFunctionHook` 直接注册 TypeScript 函数。Skill 和 agent 通过 `registerFrontmatterHooks()` 在 frontmatter 中声明 hook:

```typescript
// src/hooks/sessionHooks.ts
function addSessionHook(
  event: HookEventName,
  hook: HookDefinition
): void {
  sessionHooks[event] = sessionHooks[event] ?? [];
  sessionHooks[event].push(hook);
}
```

对子 agent 上下文, 系统自动将 `Stop` 事件转换为 `SubagentStop`。防止子 agent 的 stop hook 干扰父 agent 的生命周期。

`AsyncHookRegistry` 支持非阻塞 hook, 在后台运行不延迟所观察的操作。适用于审计日志和遥测等对延迟敏感的场景。

## 关键设计决策

**阻止操作用退出码 2 而非 1。** 退出码 1 是多数程序故障的默认值 (未捕获异常, 断言错误, 文件缺失)。用它表示 "阻止此操作" 会在 hook 脚本有 bug 时产生误判。退出码 2 是显式的, 需要有意使用的信号。

**JSON 通过 stdin/stdout 而非命令行参数。** 命令行参数有长度限制, 需要转义, 无法表示嵌套结构。stdin 上的 JSON 无实际大小限制, 直接映射到 hook 需要消费和产生的结构化数据。

**Matcher 模式在配置层而非 hook 脚本内部。** 把匹配逻辑推入每个 hook 脚本会跨 hook 重复逻辑并产生不一致。引擎中的集中匹配确保统一行为, 让 hook 脚本专注于领域逻辑。

**Stop 到 SubagentStop 的转换。** 子 agent 不应能注册在父 session 结束时触发的 `Stop` hook。自动转换将 hook 的作用域限定在子 agent 自身的生命周期, 防止跨 agent 的意外干扰。

## 实际体验

一个团队配置 `PreToolUse` hook 匹配 `Bash`, 运行脚本检查危险模式 (`rm -rf`, `chmod 777`, 数据库 drop 命令)。Agent 尝试匹配命令时, hook 通过 stdin 接收完整命令文本, 检查后以退出码 2 退出表示危险。Agent 看到阻止原因并重新调整方案。

另一个 `PostToolUse` hook 匹配 `Write|Edit`, 在文件修改时向 Slack 频道发送 webhook, 提供审计追踪。此 hook 使用 `AsyncHookRegistry`, 不拖慢 agent 执行。

通过 frontmatter 注册的 skill 添加 session 范围的 `PreToolUse` hook, 在特定 tool call 前注入领域上下文 ("Always use the v2 API endpoint")。Skill session 结束时, hook 自动清理。

## 总结

- 27 种 hook 事件类型覆盖完整 agent 生命周期, 从 session 启动到 tool 使用, 压缩, 通知和关闭。
- 四种 hook 类型 (command, prompt, http, agent) 支持不同执行模型, command hook 最常见。
- 退出码协议 (0 = 通过, 2 = 阻止, 其他 = 警告) 提供清晰的流控语义, 避免与通用程序故障混淆。
- stdin/stdout 上的结构化 JSON 使 hook 能修改 tool 输入, 注入上下文, 做权限决策 -- 远超简单的通过/失败。
- Session 范围 hook 和 AsyncHookRegistry 支持动态注册和非阻塞执行, 保持系统可组合且高性能。
