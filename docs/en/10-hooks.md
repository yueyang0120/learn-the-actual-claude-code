# Chapter 10: Hooks

The previous chapters describe systems that Claude Code controls internally -- tools, permissions, context management. But every organization has policies that cannot be anticipated by the core product: one team blocks destructive shell commands, another audits every file write, a third injects domain-specific context before certain tool calls. Hooks provide the extension surface. They allow external code to observe, modify, or block agent behavior at 27 defined event points, all configured declaratively in settings files without modifying source code.

## The Problem

A general-purpose agent cannot encode every organization's rules. Hardcoding "block `rm -rf /`" into the permission system handles one case but ignores the thousands of other policies teams need to enforce. The extension mechanism must satisfy three requirements simultaneously.

First, it must be selective. A hook that fires on every tool call creates noise. The system needs pattern-based matching so that a hook targeting `Bash` tool calls does not fire when the agent reads a file.

Second, it must be composable. Multiple hooks may apply to the same event. An audit hook should log the call, a security hook should check the command, and a context hook should inject additional instructions -- all independently, in sequence.

Third, it must communicate structured data. A simple pass/fail exit code is insufficient when hooks need to modify tool inputs, inject context into the conversation, or make permission decisions. The protocol must support rich responses while remaining simple enough to implement as a shell script.

## How Claude Code Solves It

### Event types and match fields

Claude Code defines 27 hook event types. Each event specifies a match field -- the attribute that matcher patterns compare against. `PreToolUse` matches on tool name, `Notification` matches on notification type, `SessionStart` matches on source, and `Stop` fires unconditionally (no match field).

```typescript
// src/hooks/coreTypes.ts
type HookEventName =
  | "PreToolUse"        // match: tool_name
  | "PostToolUse"       // match: tool_name
  | "PostToolUseFailure"// match: tool_name
  | "SessionStart"      // match: source
  | "SessionEnd"        // match: source
  | "Stop"              // match: none (fires for all)
  | "SubagentStart"     // match: agent_type
  | "PreCompact"        // match: none
  | "PostCompact"       // match: none
  | "Notification"      // match: notification_type
  | "FileChanged"       // match: file_path
  // ... 27 total
```

The match field design means that hook authors do not need to write filtering logic inside their scripts. The engine handles matching before invocation.

### Four hook types

Hooks come in four flavors, each suited to different use cases. The `command` type is the most common: it spawns a shell process. The `prompt` type injects text into the model's context. The `http` type sends a webhook. The `agent` type delegates to a subagent.

```typescript
// src/schemas/hooks.ts
interface HookDefinition {
  type: "command" | "prompt" | "http" | "agent";
  command?: string;     // shell command (command type)
  prompt?: string;      // text to inject (prompt type)
  url?: string;         // endpoint (http type)
  timeout?: number;     // seconds, default 600
  once?: boolean;       // fire once then auto-remove
}
```

The `once` flag supports one-shot hooks that perform initialization work -- setting up a temporary file, registering a resource -- and should not repeat.

### Matcher-based selective firing

Hook configurations in `settings.json` use a two-level structure: a matcher wraps one or more hook definitions. The matcher's `pattern` field supports pipe-delimited alternatives and glob syntax. An optional `if` condition provides additional filtering beyond the match field.

```typescript
// .claude/settings.json
{
  "hooks": {
    "PreToolUse": [
      {
        "matcher": "Bash|Write|Edit",  // pipe-delimited alternatives
        "if": "command contains 'rm'", // additional filter
        "hooks": [
          { "type": "command", "command": "/scripts/safety-check.sh" }
        ]
      }
    ]
  }
}
```

The engine's `getMatchingHooks()` function evaluates matchers against the event's match field. Only hooks whose patterns match (or whose patterns are absent, meaning "match all") proceed to execution.

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

### The execution engine and exit code protocol

The core `executeHooks()` function orchestrates the full lifecycle: gather matching hooks, execute each one, aggregate results. For `command` hooks, execution means spawning a child process with JSON on stdin and parsing JSON from stdout. The exit code carries semantic weight.

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
  return nonBlockingWarning(stderr); // any other code
}
```

Exit code 0 means the hook succeeded and the operation should proceed. Exit code 2 means the hook is actively blocking the operation -- the tool call will not execute. Any other exit code produces a non-blocking warning: the operation proceeds, but the warning appears in the agent's context.

### Structured JSON I/O

The JSON written to stdin contains base fields common to all events (session ID, event name, timestamp) plus event-specific fields (tool name, tool input, file path). The JSON on stdout can carry rich responses far beyond pass/fail.

```typescript
// Hook stdout response structure
interface HookResponse {
  decision?: "approve" | "block";
  hookSpecificOutput?: {
    hookEventName: string;
    permissionDecision?: "allow" | "deny";
    updatedInput?: Record<string, unknown>;  // modify tool input
    additionalContext?: string;               // inject into conversation
  };
}
```

The `updatedInput` field is particularly powerful: a `PreToolUse` hook can rewrite the tool's input before execution. A hook watching `Bash` calls could, for example, prepend `set -euo pipefail` to every command.

### Session-scoped and async hooks

Beyond static configuration, hooks can be registered dynamically during a session. The `addSessionHook` function registers command or prompt hooks; `addFunctionHook` registers TypeScript functions directly. Skills and agents declare hooks in their frontmatter via `registerFrontmatterHooks()`.

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

For subagent contexts, the system automatically converts `Stop` events to `SubagentStop`. This prevents a subagent's stop hook from interfering with the parent agent's lifecycle.

The `AsyncHookRegistry` supports non-blocking hooks that run in the background without delaying the operation they observe. This is appropriate for audit logging and telemetry where latency matters more than synchronous feedback.

## Key Design Decisions

**Exit code 2 for blocking instead of exit code 1.** Exit code 1 is the default for most program failures (uncaught exceptions, assertion errors, missing files). Using it to mean "block this operation" would create false positives whenever a hook script has a bug. Exit code 2 is an explicit, deliberate signal that requires intentional use.

**JSON on stdin/stdout instead of command-line arguments.** Command-line arguments have length limits, require escaping, and cannot represent nested structures. JSON on stdin has no practical size limit and maps directly to the structured data hooks need to consume and produce.

**Matcher patterns at the configuration level instead of inside hook scripts.** Pushing matching into each hook script would duplicate logic across every hook and create inconsistency. Centralized matching in the engine ensures uniform behavior and keeps hook scripts focused on their domain logic.

**Stop-to-SubagentStop conversion.** A subagent should not be able to register a `Stop` hook that fires when the parent session ends. The automatic conversion scopes the hook to the subagent's own lifecycle, preventing unintended cross-agent interference.

## In Practice

A team configures a `PreToolUse` hook matching `Bash` that runs a script checking for dangerous patterns (`rm -rf`, `chmod 777`, database drop commands). When the agent attempts a matching command, the hook receives the full command text on stdin, inspects it, and exits with code 2 if the command is dangerous. The agent sees the block reason and reformulates its approach.

Separately, a `PostToolUse` hook matching `Write|Edit` sends a webhook to a Slack channel whenever files are modified, providing an audit trail. Because this hook uses the `AsyncHookRegistry`, it does not slow down the agent's execution.

A skill registered via frontmatter adds a session-scoped `PreToolUse` hook that injects domain-specific context ("Always use the v2 API endpoint") before certain tool calls. When the skill's session ends, the hook is automatically cleaned up.

## Summary

- 27 hook event types cover the full agent lifecycle, from session start through tool use, compaction, notification, and shutdown.
- Four hook types (command, prompt, http, agent) support different execution models, with command hooks being the most common.
- The exit code protocol (0 = pass, 2 = block, other = warning) provides clear flow control semantics that avoid confusion with generic program failures.
- Structured JSON on stdin/stdout enables hooks to modify tool inputs, inject context, and make permission decisions -- far beyond simple pass/fail.
- Session-scoped hooks and the AsyncHookRegistry support dynamic registration and non-blocking execution, keeping the system composable and performant.
