# Chapter 5: Permissions

Every tool call in Claude Code passes through a permission decision engine before execution. The system in `permissions.ts` (1,486 lines) evaluates rules from multiple sources, enforces safety invariants that survive even the most permissive modes, and delegates ambiguous cases to either the user or an AI classifier. This chapter follows directly from the tool orchestration layer described in Chapter 4: once the system prompt has been assembled and a tool call is ready to execute, the permission pipeline is the final gate.

## The Problem

An agent that can run arbitrary shell commands and write to any file on disk needs guardrails. A naive allow/deny list is insufficient for three reasons.

First, rules originate from multiple sources with different trust levels. An enterprise policy must override a user preference, and a user preference must override a project default. A single flat list cannot express this hierarchy.

Second, certain paths and operations must always prompt the user regardless of configuration. If a user enables "bypass all permissions" mode to speed up a coding session, the system must still prevent silent writes to `.git/`, `.claude/`, or shell configuration files. These are bypass-immune safety checks.

Third, in non-interactive contexts such as CI pipelines or background agents, there is no user to prompt. The system needs an automated fallback, but that fallback must itself have a circuit breaker to prevent runaway denial loops.

## How Claude Code Solves It

### The PermissionRule Triple

Every rule in the permission system is a triple of source, behavior, and value:

```typescript
// src/types/permissions.ts
type PermissionRule = {
  source: PermissionRuleSource     // WHERE the rule came from
  ruleBehavior: PermissionBehavior // WHAT it does: 'allow' | 'deny' | 'ask'
  ruleValue: PermissionRuleValue   // WHICH tool (and optional content)
}

type PermissionRuleValue = {
  toolName: string       // e.g. "Bash", "Write", "mcp__server1__tool1"
  ruleContent?: string   // e.g. "npm install", "prefix:git *"
}
```

Rules are persisted in settings JSON as strings with the format `Tool(content)`. The parser in `permissionRuleParser.ts` handles this syntax:

```typescript
// src/utils/permissions/permissionRuleParser.ts
permissionRuleValueFromString('Bash')
// => { toolName: 'Bash' }

permissionRuleValueFromString('Bash(npm install)')
// => { toolName: 'Bash', ruleContent: 'npm install' }
```

The parser finds the first unescaped `(` and last unescaped `)`. Special cases include `Bash()` and `Bash(*)`, both treated as tool-wide rules with no content filter.

### Rule Sources and Loading

Rules come from eight possible sources, listed here in ascending trust:

| Source | Example Path | Scope |
|--------|-------------|-------|
| `session` | (in-memory) | Ephemeral, current session only |
| `command` | Inline `/allow` during session | Current session |
| `cliArg` | `--allow`, `--deny` flags | Current invocation |
| `localSettings` | `.claude/settings.local.json` | Per-project, gitignored |
| `projectSettings` | `.claude/settings.json` | Per-project, shared |
| `userSettings` | `~/.claude/settings.json` | User-global |
| `flagSettings` | Feature flags | Platform-managed |
| `policySettings` | Enterprise/managed policy | Highest priority |

The loader iterates all enabled sources. If `allowManagedPermissionRulesOnly` is set in policy, all other sources are ignored:

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

This design means an enterprise administrator can lock down all permissions to a managed policy with a single flag.

### The Six Permission Modes

Modes control the overall posture of the system:

| Mode | Behavior |
|------|----------|
| `default` | Rules apply; unmatched actions prompt the user |
| `acceptEdits` | File edits in the working directory auto-allow; shell commands still prompt |
| `bypassPermissions` | Nearly everything auto-allows, except deny rules, ask rules, and safety checks |
| `dontAsk` | Converts every `ask` result to `deny`; never prompts, just rejects |
| `plan` | Read-only planning mode; respects bypass if the user started with it |
| `auto` | Uses an AI classifier to decide allow/deny instead of prompting |

The critical insight: even `bypassPermissions` mode respects deny rules, explicit ask rules, and safety checks. The code refers to these as "bypass-immune":

```typescript
// src/utils/permissions/permissions.ts -- hasPermissionsToUseToolInner
if (
  toolPermissionResult?.behavior === 'ask' &&
  toolPermissionResult.decisionReason?.type === 'safetyCheck'
) {
  return toolPermissionResult
}
```

### The hasPermissionsToUseTool Pipeline

Every tool call flows through a three-step inner pipeline, followed by an outer wrapper that applies mode-based transformations.

**Inner pipeline** (`hasPermissionsToUseToolInner`):

Step 1 checks deny and ask rules that cannot be bypassed:
- 1a. Entire tool denied by rule -- DENY
- 1b. Entire tool has ask rule -- ASK
- 1c. `tool.checkPermissions(input, context)` -- tool-specific logic
- 1d. Tool implementation denied -- DENY
- 1e. Tool requires user interaction -- ASK (even in bypass mode)
- 1f. Content-specific ask rule -- ASK (bypass-immune)
- 1g. Safety check on protected paths -- ASK (bypass-immune)

Step 2 checks mode and allow rules:
- 2a. `bypassPermissions` mode -- ALLOW
- 2b. Entire tool has allow rule -- ALLOW

Step 3 applies the default fallback:
- Convert passthrough to ASK

**Outer wrapper** (`hasPermissionsToUseTool`): if the inner result is ASK, the wrapper applies mode transformations. In `dontAsk` mode, ASK becomes DENY. In `auto` mode, the system runs the AI classifier. For headless agents, it runs PermissionRequest hooks and then auto-denies.

### Content-Level Matching

Tool-level matching checks whether a rule applies to the entire tool. Content-level matching -- for example, "does this bash command match the `npm install` prefix rule?" -- is handled by each tool's `checkPermissions` method. The `getRuleByContentsForTool` function builds a Map for efficient lookup:

```typescript
// src/utils/permissions/permissions.ts
export function getRuleByContentsForTool(
  context, tool, behavior
): Map<string, PermissionRule> {
  // Returns a map like:
  //   "npm install" -> {source:'userSettings', behavior:'allow', ...}
  //   "prefix:git *" -> {source:'projectSettings', behavior:'allow', ...}
}
```

This allows the Bash tool, for instance, to check a specific command string against all content-level rules in a single Map lookup rather than iterating the full rule list.

### The Auto-Mode AI Classifier

When in `auto` mode, instead of prompting the user, Claude Code runs a separate AI classifier to decide whether a tool call is safe. This classifier (`yoloClassifier.ts`) operates in a 2-stage XML mode:

- **Stage 1 (fast):** A short response, stopped on `</block>`. If the classifier returns `<block>no</block>` (allow), execution proceeds immediately.
- **Stage 2 (thinking):** If stage 1 blocked the action, a second call with chain-of-thought reasoning reduces false positives.

The classifier receives a compact transcript of the conversation, not the full context. Assistant text is excluded from this transcript to prevent the model from crafting text that influences the classifier. The design is fail-closed: if the classifier API errors out, returns garbage, or times out, the action is blocked.

```typescript
// src/utils/permissions/yoloClassifier.ts
if (!parsed) {
  return {
    shouldBlock: true,
    reason: 'Invalid classifier response - blocking for safety',
  }
}
```

### Denial Tracking with Circuit Breaker

The denial tracking system in `denialTracking.ts` prevents the auto-mode classifier from getting stuck in a deny loop:

```typescript
// src/utils/permissions/denialTracking.ts
const DENIAL_LIMITS = {
  maxConsecutive: 3,   // 3 in a row -> fall back to prompting
  maxTotal: 20,        // 20 total in session -> fall back to prompting
}
```

On each classifier deny, both counters increment. On any allow (even rule-based), the consecutive counter resets to zero. When either limit is hit, the system falls back to prompting the user instead of auto-denying. After the total limit triggers fallback, the total counter resets to zero to avoid immediate re-triggering.

## Key Design Decisions

**Deny rules always win.** Deny rules are checked first (step 1a), before any allow check. A deny rule from any source blocks the tool, even if another source allows it. This is a deliberate asymmetry: it is always possible to restrict, never possible to override a restriction from a lower-trust source.

**The tool itself participates.** Each tool implements `checkPermissions(parsedInput, context)`, which allows tool-specific logic. The Bash tool, for example, splits compound commands and checks each subcommand against content-level rules independently.

**Passthrough is the default.** If no rule matches and the tool has no opinion, the result is `passthrough`, which step 3 converts to `ask`. This ensures that new tools default to requiring user approval rather than silently executing.

**Decisions carry their reason.** Every permission decision includes a `decisionReason` field explaining why it was made (rule match, mode, classifier result, safety check). This enables rich error messages and analytics without requiring the caller to reconstruct the decision path.

**Fail-closed classifier.** The auto-mode classifier is designed so that any failure mode -- API error, parse failure, timeout -- results in blocking the action. The system never fails open.

## In Practice

When a user runs Claude Code in default mode and the model calls `Bash(npm install)`, the pipeline checks deny rules (none match), then ask rules (none match), then the Bash tool's own `checkPermissions` (no content match), and finally falls through to step 3, which prompts the user. If the user selects "always allow," a rule `Bash(npm install)` is written to `~/.claude/settings.json` with behavior `allow`. On the next invocation, step 2b matches the allow rule and the command executes without prompting.

In `auto` mode, the same unmatched command triggers the AI classifier instead of a user prompt. If the classifier denies three commands consecutively, the system falls back to interactive prompting as a safety valve.

Protected path writes -- for example, editing `.git/config` -- always prompt regardless of mode. This is observable even when using `--dangerously-skip-permissions`: the system still pauses for confirmation on these paths.

## Summary

- Every permission rule is a triple of source, behavior, and value; rules are loaded from up to eight sources with enterprise policy at the top.
- The inner pipeline checks deny rules and bypass-immune safety checks before any allow logic, ensuring that restrictions cannot be overridden.
- Six permission modes range from interactive prompting (`default`) to fully automated (`auto`), with `bypassPermissions` still respecting safety-critical checks.
- The auto-mode AI classifier uses a 2-stage approach with fail-closed semantics and a circuit breaker (3 consecutive or 20 total denials) to prevent runaway loops.
- Content-level matching delegates to each tool's `checkPermissions` method, allowing tools like Bash to implement subcommand-level granularity.
