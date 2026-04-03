# 第 5 章: 权限系统

Claude Code 中的每次 tool call 在执行前都经过权限决策引擎。`permissions.ts` (1,486 行) 中的系统评估来自多个来源的规则, 强制执行即使在最宽松模式下也存活的安全不变量, 并将模糊情况委托给用户或 AI 分类器。本章直接衔接第 3-4 章: system prompt 组装完毕, tool call 准备执行, 权限流水线是最后的闸门。

## 问题

一个能执行任意 shell 命令并写入磁盘任何文件的 agent 需要护栏。朴素的允许/拒绝列表因三个原因而不够。

第一, 规则来自多个信任层级不同的来源。企业策略必须覆盖用户偏好, 用户偏好必须覆盖项目默认值。单一的扁平列表无法表达这种层级关系。

第二, 某些路径和操作无论配置如何都必须提示用户。如果用户启用 "绕过所有权限" 模式来加快编码会话, 系统仍须阻止对 `.git/`, `.claude/` 或 shell 配置文件的静默写入。这些是绕过免疫的安全检查。

第三, 在非交互式上下文 (如 CI 流水线或后台 agent) 中, 没有用户可以提示。系统需要自动化回退, 但该回退本身必须有断路器以防止失控的拒绝循环。

## Claude Code 的解法

### PermissionRule 三元组

权限系统中的每条规则是来源, 行为和值的三元组:

```typescript
// src/types/permissions.ts
type PermissionRule = {
  source: PermissionRuleSource     // 规则来自哪里
  ruleBehavior: PermissionBehavior // 做什么: 'allow' | 'deny' | 'ask'
  ruleValue: PermissionRuleValue   // 匹配哪个工具 (及可选内容)
}

type PermissionRuleValue = {
  toolName: string       // 如 "Bash", "Write", "mcp__server1__tool1"
  ruleContent?: string   // 如 "npm install", "prefix:git *"
}
```

规则在 settings JSON 中以 `Tool(content)` 格式的字符串持久化。`permissionRuleParser.ts` 中的解析器处理此语法:

```typescript
// src/utils/permissions/permissionRuleParser.ts
permissionRuleValueFromString('Bash')
// => { toolName: 'Bash' }

permissionRuleValueFromString('Bash(npm install)')
// => { toolName: 'Bash', ruleContent: 'npm install' }
```

解析器找到第一个未转义的 `(` 和最后一个未转义的 `)`。特殊情况包括 `Bash()` 和 `Bash(*)`, 均被视为无内容过滤的工具级规则。

### 规则来源与加载

规则来自八种可能的来源, 按信任度升序:

| 来源 | 示例路径 | 作用域 |
|------|---------|--------|
| `session` | (内存中) | 临时, 仅当前会话 |
| `command` | 会话中的 `/allow` 命令 | 当前会话 |
| `cliArg` | `--allow`, `--deny` 标志 | 当前调用 |
| `localSettings` | `.claude/settings.local.json` | 每项目, 在 gitignore 中 |
| `projectSettings` | `.claude/settings.json` | 每项目, 共享 |
| `userSettings` | `~/.claude/settings.json` | 用户全局 |
| `flagSettings` | Feature flag | 平台管理 |
| `policySettings` | 企业/托管策略 | 最高优先级 |

加载器遍历所有启用的来源。如果策略中设置了 `allowManagedPermissionRulesOnly`, 则所有其他来源被忽略:

```typescript
// src/utils/permissions/permissionsLoader.ts
export function loadAllPermissionRulesFromDisk(): PermissionRule[] {
  if (shouldAllowManagedPermissionRulesOnly()) {
    return getPermissionRulesForSource('policySettings')
  }
  const rules: PermissionRule[] = []
  for (const source of getEnabledSettingSources()) {
    rules.push(...getPermissionRulesForSource(source))
  }
  return rules
}
```

企业管理员可以用单个标志将所有权限锁定到托管策略。

### 六种权限模式

模式控制系统的整体姿态:

| 模式 | 行为 |
|------|------|
| `default` | 应用规则; 未匹配的操作提示用户 |
| `acceptEdits` | 工作目录内的文件编辑自动允许; shell 命令仍需提示 |
| `bypassPermissions` | 几乎一切自动允许, 但 deny 规则, ask 规则和安全检查除外 |
| `dontAsk` | 将每个 `ask` 结果转为 `deny`; 永不提示, 直接拒绝 |
| `plan` | 只读规划模式; 如果用户以 bypass 启动则尊重该设置 |
| `auto` | 使用 AI 分类器代替提示来决定允许/拒绝 |

关键洞察: 即使 `bypassPermissions` 模式也尊重 deny 规则, 显式 ask 规则和安全检查。代码将这些称为 "bypass-immune":

```typescript
// src/utils/permissions/permissions.ts -- hasPermissionsToUseToolInner
if (
  toolPermissionResult?.behavior === 'ask' &&
  toolPermissionResult.decisionReason?.type === 'safetyCheck'
) {
  return toolPermissionResult
}
```

### hasPermissionsToUseTool 流水线

每次 tool call 流经三步内部流水线, 外层包装器再应用基于模式的转换。

**内部流水线** (`hasPermissionsToUseToolInner`):

第一步检查不可绕过的 deny 和 ask 规则:
- 1a. 规则拒绝整个工具 -- DENY
- 1b. 规则对整个工具设置 ask -- ASK
- 1c. `tool.checkPermissions(input, context)` -- 工具特定逻辑
- 1d. 工具实现拒绝 -- DENY
- 1e. 工具要求用户交互 -- ASK (即使在 bypass 模式)
- 1f. 内容特定 ask 规则 -- ASK (bypass-immune)
- 1g. 受保护路径的安全检查 -- ASK (bypass-immune)

第二步检查模式和允许规则:
- 2a. `bypassPermissions` 模式 -- ALLOW
- 2b. 规则允许整个工具 -- ALLOW

第三步应用默认回退:
- 将 passthrough 转为 ASK

**外层包装器** (`hasPermissionsToUseTool`): 如果内部结果是 ASK, 包装器应用模式转换。在 `dontAsk` 模式下 ASK 变为 DENY。在 `auto` 模式下运行 AI 分类器。对 headless agent, 先运行 PermissionRequest hook 再自动拒绝。

### 内容级匹配

工具级匹配检查规则是否适用于整个工具。内容级匹配 — 例如 "这条 bash 命令是否匹配 `npm install` 前缀规则?" — 由每个工具的 `checkPermissions` 方法处理。`getRuleByContentsForTool` 函数构建一个 Map 以高效查找:

```typescript
// src/utils/permissions/permissions.ts
export function getRuleByContentsForTool(
  context, tool, behavior
): Map<string, PermissionRule> {
  // 返回如下 map:
  //   "npm install" -> {source:'userSettings', behavior:'allow', ...}
  //   "prefix:git *" -> {source:'projectSettings', behavior:'allow', ...}
}
```

这允许 Bash 工具对特定命令字符串在单次 Map 查找中检查所有内容级规则, 而非遍历完整规则列表。

### Auto 模式的 AI 分类器

在 `auto` 模式下, Claude Code 运行一个独立的 AI 分类器代替提示用户。这个分类器 (`yoloClassifier.ts`) 以两阶段 XML 模式运行:

- **阶段 1 (快速):** 简短回复, 在 `</block>` 处停止。如果分类器返回 `<block>no</block>` (允许), 执行立即继续。
- **阶段 2 (推理):** 如果阶段 1 阻止了操作, 第二次带链式思维推理的调用减少误报。

分类器接收对话的紧凑摘要, 不是完整上下文。assistant 文本被排除在摘要之外, 防止模型构造影响分类器的文本。设计是 fail-closed: 如果分类器 API 出错, 返回无效数据或超时, 操作被阻止。

```typescript
// src/utils/permissions/yoloClassifier.ts
if (!parsed) {
  return {
    shouldBlock: true,
    reason: 'Invalid classifier response - blocking for safety',
  }
}
```

### 拒绝追踪与断路器

`denialTracking.ts` 中的拒绝追踪系统防止 auto 模式分类器陷入拒绝循环:

```typescript
// src/utils/permissions/denialTracking.ts
const DENIAL_LIMITS = {
  maxConsecutive: 3,   // 连续 3 次 -> 回退到用户提示
  maxTotal: 20,        // 会话总计 20 次 -> 回退到用户提示
}
```

每次分类器拒绝, 两个计数器都递增。任何允许 (即使是基于规则的) 重置连续计数器为零。当任一限制触发, 系统回退到提示用户而非自动拒绝。总计限制触发回退后, 总计计数器重置为零以避免立即再次触发。

## 关键设计决策

**Deny 规则始终优先。** Deny 规则在第一步 (1a) 检查, 早于任何 allow 检查。来自任何来源的 deny 规则都会阻止工具, 即使另一个来源允许它。这是刻意的不对称: 限制始终可能, 永远无法从更低信任来源覆盖限制。

**工具本身参与决策。** 每个工具实现 `checkPermissions(parsedInput, context)`, 允许工具特定逻辑。Bash 工具会拆分复合命令并独立检查每个子命令的内容级规则。

**Passthrough 是默认。** 如果没有规则匹配且工具没有意见, 结果是 `passthrough`, 第三步将其转为 `ask`。这确保新工具默认要求用户批准, 而非静默执行。

**决策携带原因。** 每个权限决策包含 `decisionReason` 字段, 解释为何做出该决策 (规则匹配, 模式, 分类器结果, 安全检查)。这使丰富的错误消息和分析成为可能, 调用方无需重建决策路径。

**Fail-closed 分类器。** Auto 模式分类器设计为任何失败模式 — API 错误, 解析失败, 超时 — 都导致阻止操作。系统永不 fail-open。

## 实际体验

用户在 default 模式下运行 Claude Code, 模型调用 `Bash(npm install)` 时, 流水线检查 deny 规则 (无匹配), ask 规则 (无匹配), Bash 工具自身的 `checkPermissions` (无内容匹配), 最终落到第三步提示用户。如果用户选择 "always allow", 规则 `Bash(npm install)` 以 `allow` 行为写入 `~/.claude/settings.json`。下次调用时, 第 2b 步匹配 allow 规则, 命令无需提示即执行。

在 `auto` 模式下, 同样未匹配的命令触发 AI 分类器而非用户提示。如果分类器连续拒绝三个命令, 系统回退到交互式提示作为安全阀。

受保护路径写入 — 例如编辑 `.git/config` — 无论模式如何都提示。即使使用 `--dangerously-skip-permissions`, 系统仍然在这些路径上暂停确认。

## 总结

- 每条权限规则是来源, 行为和值的三元组; 规则从最多八个来源加载, 企业策略位于顶端。
- 内部流水线在任何 allow 逻辑之前检查 deny 规则和 bypass-immune 安全检查, 确保限制不可被覆盖。
- 六种权限模式从交互式提示 (`default`) 到完全自动化 (`auto`), `bypassPermissions` 仍尊重安全关键检查。
- Auto 模式 AI 分类器使用两阶段方式, 具有 fail-closed 语义和断路器 (连续 3 次或总计 20 次拒绝) 以防止失控循环。
- 内容级匹配委托给每个工具的 `checkPermissions` 方法, 允许 Bash 等工具实现子命令级粒度。
