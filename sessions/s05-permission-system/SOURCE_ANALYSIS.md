# Source Analysis -- The Permission System

## 1. PermissionRule Structure

Every permission rule in Claude Code is a triple:

```typescript
// src/types/permissions.ts
type PermissionRule = {
  source: PermissionRuleSource      // WHERE the rule came from
  ruleBehavior: PermissionBehavior  // WHAT it does: 'allow' | 'deny' | 'ask'
  ruleValue: PermissionRuleValue    // WHICH tool (and optional content)
}

type PermissionRuleValue = {
  toolName: string        // e.g. "Bash", "Write", "mcp__server1__tool1"
  ruleContent?: string    // e.g. "npm install", "prefix:git *"
}
```

### String Format

Rules are stored in settings JSON as strings like `"Bash(npm install)"`. The
parser in `permissionRuleParser.ts` handles the `Tool(content)` syntax with
proper escaping of parentheses:

```typescript
// src/utils/permissions/permissionRuleParser.ts
permissionRuleValueFromString('Bash')
// => { toolName: 'Bash' }

permissionRuleValueFromString('Bash(npm install)')
// => { toolName: 'Bash', ruleContent: 'npm install' }

permissionRuleValueFromString('Bash(python -c "print\\(1\\)")')
// => { toolName: 'Bash', ruleContent: 'python -c "print(1)"' }
```

The parser finds the first unescaped `(` and last unescaped `)`. Special cases:
- `Bash()` and `Bash(*)` are treated as tool-wide rules (no content).
- Legacy tool names are normalized: `Task` -> `Agent`, `KillShell` -> `TaskStop`.

### Rule Sources

```typescript
// src/types/permissions.ts
type PermissionRuleSource =
  | 'userSettings'      // ~/.claude/settings.json (user-global)
  | 'projectSettings'   // .claude/settings.json (per-project)
  | 'localSettings'     // .claude/settings.local.json (per-project, gitignored)
  | 'flagSettings'      // Feature flags
  | 'policySettings'    // Enterprise/managed policy (highest priority)
  | 'cliArg'            // --allow, --deny flags on the command line
  | 'command'           // Inline /allow commands during a session
  | 'session'           // Ephemeral rules that last only this session
```

The loader in `permissionsLoader.ts` iterates all enabled sources. If
`allowManagedPermissionRulesOnly` is set in policySettings, ONLY managed rules
are loaded -- all other sources are ignored:

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

### Settings JSON Shape

Rules are stored in settings JSON under `permissions.allow`, `permissions.deny`,
and `permissions.ask` arrays:

```json
{
  "permissions": {
    "allow": ["Bash(npm install)", "Write", "Read"],
    "deny": ["Bash(rm -rf /)", "mcp__untrusted_server"],
    "ask": ["Bash(npm publish:*)"]
  }
}
```

---

## 2. Permission Modes

Modes control the overall permission posture. They are defined in
`src/types/permissions.ts` and configured in `PermissionMode.ts`:

| Mode | Behavior |
|------|----------|
| `default` | Normal operation: rules apply, unmatched actions prompt the user |
| `acceptEdits` | File edits in the working directory are auto-allowed; bash still prompts |
| `bypassPermissions` | Nearly everything is auto-allowed, EXCEPT deny rules, ask rules, and safety checks |
| `dontAsk` | Converts every `ask` result to `deny` -- never prompts, just rejects |
| `plan` | Read-only planning mode; respects bypass if the user started with it |
| `auto` | (Internal) Uses an AI classifier to decide allow/deny instead of prompting |

Key insight: even `bypassPermissions` mode respects deny rules, explicit ask
rules, and safety checks. The code comments call these "bypass-immune":

```typescript
// src/utils/permissions/permissions.ts -- hasPermissionsToUseToolInner
// 1g. Safety checks (e.g. .git/, .claude/, .vscode/, shell configs) are
// bypass-immune -- they must prompt even in bypassPermissions mode.
if (
  toolPermissionResult?.behavior === 'ask' &&
  toolPermissionResult.decisionReason?.type === 'safetyCheck'
) {
  return toolPermissionResult
}
```

---

## 3. The hasPermissionsToUseTool Pipeline

This is the core entry point. Every tool call flows through
`hasPermissionsToUseTool`, which wraps `hasPermissionsToUseToolInner` with
post-processing for mode transformations (dontAsk, auto-mode classifier,
headless agent handling).

### Inner Pipeline Steps

```
hasPermissionsToUseToolInner(tool, input, context):

Step 1: Check deny/ask/tool-specific rules (CANNOT be bypassed)
  1a. Entire tool is denied by rule         -> DENY
  1b. Entire tool has ask rule              -> ASK (unless sandbox auto-allow)
  1c. Tool.checkPermissions(input, context) -> tool-specific check
  1d. Tool implementation denied            -> DENY
  1e. Tool requires user interaction        -> ASK (even in bypass mode)
  1f. Content-specific ask rule from tool   -> ASK (bypass-immune)
  1g. Safety check (protected paths)        -> ASK (bypass-immune)

Step 2: Check mode and allow rules
  2a. bypassPermissions mode?               -> ALLOW
  2b. Entire tool has allow rule?           -> ALLOW

Step 3: Default fallback
  Convert passthrough to ask                -> ASK
```

### Outer Wrapper Processing

After `hasPermissionsToUseToolInner` returns, the outer
`hasPermissionsToUseTool` applies mode-based transformations:

```
If result is ALLOW:
  Reset consecutive denial counter (auto mode)
  Return ALLOW

If result is ASK:
  dontAsk mode?        -> Convert to DENY
  auto mode?           -> Run AI classifier
    acceptEdits check  -> Fast-path ALLOW for safe edits
    allowlisted tool?  -> Fast-path ALLOW
    Run classifier     -> ALLOW or DENY based on AI decision
    Denial limit hit?  -> Fall back to prompting (ASK)
  headless agent?      -> Run PermissionRequest hooks, then auto-DENY
```

### Rule Matching

Tool-level matching checks if a rule applies to the entire tool:

```typescript
// src/utils/permissions/permissions.ts
function toolMatchesRule(tool, rule): boolean {
  // Rule must not have content to match the entire tool
  if (rule.ruleValue.ruleContent !== undefined) {
    return false
  }
  // Direct name match
  if (rule.ruleValue.toolName === nameForRuleMatch) {
    return true
  }
  // MCP server-level: rule "mcp__server1" matches "mcp__server1__tool1"
  // Also "mcp__server1__*" matches all tools from server1
  return ruleInfo !== null && toolInfo !== null &&
    (ruleInfo.toolName === undefined || ruleInfo.toolName === '*') &&
    ruleInfo.serverName === toolInfo.serverName
}
```

Content-level matching (e.g., "does this bash command match the `npm install`
prefix rule?") is handled by each tool's `checkPermissions` method. The
`getRuleByContentsForTool` function builds a Map of content strings to rules for
efficient lookup:

```typescript
// src/utils/permissions/permissions.ts
export function getRuleByContentsForTool(context, tool, behavior):
    Map<string, PermissionRule> {
  // Returns a map like:
  //   "npm install" -> {source:'userSettings', behavior:'allow', ...}
  //   "prefix:git *" -> {source:'projectSettings', behavior:'allow', ...}
}
```

---

## 4. Bash Command Classification

### The Auto-Mode AI Classifier (yoloClassifier.ts)

When in `auto` mode, instead of prompting the user, Claude Code runs a
**separate AI classifier** to decide if a tool call is safe. This is a
full side-query to the Anthropic API with its own system prompt and transcript.

The classifier operates in three modes:

1. **Tool-use mode** (legacy) -- Forces the model to call a `classify_result`
   tool with `{thinking, shouldBlock, reason}`.
2. **2-stage XML mode** (`both`, `fast`, `thinking`):
   - **Stage 1 (fast)**: Short response, stop on `</block>`. If it says
     `<block>no</block>` (allow), return immediately.
   - **Stage 2 (thinking)**: If stage 1 blocked, run again with chain-of-thought
     (`<thinking>...</thinking>`) to reduce false positives.

```typescript
// src/utils/permissions/yoloClassifier.ts
const XML_S1_SUFFIX = '\nErr on the side of blocking. <block> immediately.'
const XML_S2_SUFFIX = '\nReview the classification process and follow it carefully...'
```

The classifier receives a compact transcript of the conversation (user messages
+ tool calls) and the action being evaluated. Each tool controls what the
classifier sees via its `toAutoClassifierInput` method.

### Transcript Building

The classifier does NOT see the full conversation. It sees a compact projection:

```typescript
// yoloClassifier.ts
function toCompactBlock(block, role, lookup): string {
  if (block.type === 'tool_use') {
    // Tool controls its own representation via toAutoClassifierInput
    const encoded = tool.toAutoClassifierInput(input)
    return `${block.name} ${encoded}\n`  // e.g. "Bash rm -rf /"
  }
  if (block.type === 'text' && role === 'user') {
    return `User: ${block.text}\n`
  }
}
```

Important: assistant text is **excluded** from the transcript to prevent the
model from crafting text that influences the classifier.

### Bash Classifier (bashClassifier.ts)

The external build has a stub implementation -- the real bash-specific classifier
is internal-only. However, the architecture is visible:

```typescript
// src/utils/permissions/bashClassifier.ts (stub)
export type ClassifierResult = {
  matches: boolean
  matchedDescription?: string
  confidence: 'high' | 'medium' | 'low'
  reason: string
}

export type ClassifierBehavior = 'deny' | 'ask' | 'allow'

// Full impl would use AI to classify individual bash commands
// against user-defined "prompt:" rules
export async function classifyBashCommand(
  command: string,
  cwd: string,
  descriptions: string[],
  behavior: ClassifierBehavior,
  signal: AbortSignal,
): Promise<ClassifierResult> { ... }
```

The internal implementation likely uses an LLM to check if a bash command
matches natural-language descriptions provided in `Bash(prompt: ...)` rules.

### Fail-Closed Design

The classifier is designed to **fail closed** -- if the classifier API errors
out, returns garbage, or times out, the action is blocked:

```typescript
// yoloClassifier.ts
if (!toolUseBlock) {
  return {
    shouldBlock: true,
    reason: 'Classifier returned no tool use block - blocking for safety',
  }
}
// Parse failure -> block
if (!parsed) {
  return {
    shouldBlock: true,
    reason: 'Invalid classifier response - blocking for safety',
  }
}
```

---

## 5. Permission Persistence and Updates

### Loading from Disk

`permissionsLoader.ts` reads each settings file, parses the JSON, and converts
the `permissions.allow`, `permissions.deny`, `permissions.ask` arrays into
`PermissionRule[]`:

```typescript
// permissionsLoader.ts
function settingsJsonToRules(data, source): PermissionRule[] {
  const rules: PermissionRule[] = []
  for (const behavior of ['allow', 'deny', 'ask']) {
    const behaviorArray = data.permissions[behavior]
    if (behaviorArray) {
      for (const ruleString of behaviorArray) {
        rules.push({
          source,
          ruleBehavior: behavior,
          ruleValue: permissionRuleValueFromString(ruleString),
        })
      }
    }
  }
  return rules
}
```

### Adding Rules

When a user approves a tool and selects "always allow", the rule is persisted:

```typescript
// permissionsLoader.ts
export function addPermissionRulesToSettings({ ruleValues, ruleBehavior }, source): boolean {
  const settingsData = getSettingsForSource(source) || ...
  const existingRules = settingsData.permissions[ruleBehavior] || []
  // Deduplicate via normalize roundtrip (handles legacy name aliases)
  const existingRulesSet = new Set(
    existingRules.map(raw => permissionRuleValueToString(permissionRuleValueFromString(raw)))
  )
  const newRules = ruleStrings.filter(rule => !existingRulesSet.has(rule))
  // ...write back to settings file
}
```

### Syncing on File Change

When settings files change on disk (e.g., user edits `.claude/settings.json`),
the permission context is rebuilt from scratch:

```typescript
// permissions.ts
export function syncPermissionRulesFromDisk(context, rules): ToolPermissionContext {
  // 1. Clear all disk-based source:behavior combos
  // 2. Apply new rules from disk
  // This ensures deleted rules are actually removed
}
```

---

## 6. Denial Tracking with Limits and Circuit Breaker

The denial tracking system in `denialTracking.ts` prevents the auto-mode
classifier from getting stuck in a deny loop:

```typescript
// src/utils/permissions/denialTracking.ts
type DenialTrackingState = {
  consecutiveDenials: number
  totalDenials: number
}

const DENIAL_LIMITS = {
  maxConsecutive: 3,   // 3 in a row -> fall back to prompting
  maxTotal: 20,        // 20 total in session -> fall back to prompting
}

function shouldFallbackToPrompting(state): boolean {
  return (
    state.consecutiveDenials >= DENIAL_LIMITS.maxConsecutive ||
    state.totalDenials >= DENIAL_LIMITS.maxTotal
  )
}
```

The lifecycle:
1. **On classifier deny**: `recordDenial` increments both counters.
2. **On any allow** (even rule-based): `recordSuccess` resets `consecutiveDenials` to 0.
3. **When limit hit**: The system falls back to prompting the user (ASK) instead
   of auto-denying. For headless agents, it throws an `AbortError`.
4. **After total limit fallback**: The total counter is reset to 0 to avoid
   immediately re-triggering.

```typescript
// permissions.ts -- handleDenialLimitExceeded
if (hitTotalLimit) {
  persistDenialState(context, {
    ...denialState,
    totalDenials: 0,        // Reset to prevent immediate re-trigger
    consecutiveDenials: 0,
  })
}
```

---

## 7. How canUseTool is Threaded Through ToolUseContext

The permission check function is provided as `CanUseToolFn` and threaded through
the tool execution context:

```typescript
// src/hooks/useCanUseTool.ts (inferred from types)
type CanUseToolFn = (
  tool: Tool,
  input: { [key: string]: unknown },
  context: ToolUseContext,
  assistantMessage: AssistantMessage,
  toolUseID: string,
) => Promise<PermissionDecision>
```

The `ToolUseContext` carries everything needed for permission checks:

```typescript
// src/types/permissions.ts
type ToolPermissionContext = {
  readonly mode: PermissionMode
  readonly additionalWorkingDirectories: ReadonlyMap<string, ...>
  readonly alwaysAllowRules: ToolPermissionRulesBySource
  readonly alwaysDenyRules: ToolPermissionRulesBySource
  readonly alwaysAskRules: ToolPermissionRulesBySource
  readonly isBypassPermissionsModeAvailable: boolean
  readonly shouldAvoidPermissionPrompts?: boolean
  // ...
}
```

The context is stored in React-like app state and can be updated when rules
change (e.g., user approves a tool). Async subagents use
`localDenialTracking` since their `setAppState` is a no-op.

### Key Design Decisions

1. **Rules are gathered from ALL sources** via `PERMISSION_RULE_SOURCES.flatMap(source => ...)`
   -- this means a deny rule from any source blocks the tool, even if another
   source allows it.

2. **Deny rules always win** -- checked first (step 1a), before any allow check.

3. **The tool itself participates** -- `tool.checkPermissions(parsedInput, context)`
   lets each tool implement custom permission logic (e.g., Bash splits the
   command and checks subcommand rules).

4. **Passthrough is the default** -- if no rule matches and the tool has no
   opinion, the result is `passthrough`, which gets converted to `ask` in step 3.

5. **Permission decisions carry their reason** -- every decision includes a
   `decisionReason` that explains WHY it was made (rule, mode, classifier, hook,
   safety check, etc.), enabling rich error messages and analytics.
