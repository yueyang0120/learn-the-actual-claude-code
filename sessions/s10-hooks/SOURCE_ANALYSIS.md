# Source Analysis: The Hooks System

## Table of Contents

1. [Hook Event Types](#1-hook-event-types)
2. [Hook Definition in Settings](#2-hook-definition-in-settings)
3. [Hook Execution: executePreToolHooks / executePostToolHooks](#3-hook-execution)
4. [Hook JSON I/O Schema](#4-hook-json-io-schema)
5. [Hook Responses: Allow, Deny, Modify](#5-hook-responses)
6. [registerFrontmatterHooks -- Skills Registering Hooks](#6-registerfrontmatterhooks)
7. [sessionHooks -- Per-Session Hook State](#7-sessionhooks)
8. [AsyncHookRegistry -- Non-Blocking Hooks](#8-asynchookregistry)

---

## 1. Hook Event Types

Claude Code defines **27 hook event types** in `src/entrypoints/sdk/coreTypes.ts`.
These cover the full lifecycle of a session, from startup through tool execution
to shutdown:

```typescript
// src/entrypoints/sdk/coreTypes.ts
export const HOOK_EVENTS = [
  'PreToolUse',         // Before a tool executes
  'PostToolUse',        // After successful tool execution
  'PostToolUseFailure', // After a tool execution fails
  'Notification',       // When a notification is sent (permission prompt, idle, etc.)
  'UserPromptSubmit',   // When the user submits a prompt
  'SessionStart',       // When a new session starts (startup, resume, clear, compact)
  'SessionEnd',         // When a session is ending
  'Stop',               // Right before Claude concludes its response
  'StopFailure',        // When the turn ends due to an API error
  'SubagentStart',      // When a subagent (Agent tool call) starts
  'SubagentStop',       // When a subagent concludes its response
  'PreCompact',         // Before conversation compaction
  'PostCompact',        // After conversation compaction
  'PermissionRequest',  // When a permission dialog is displayed
  'PermissionDenied',   // After auto mode classifier denies a tool call
  'Setup',              // Repo setup hooks for init and maintenance
  'TeammateIdle',       // When a teammate is about to go idle
  'TaskCreated',        // When a task is being created
  'TaskCompleted',      // When a task is being marked as completed
  'Elicitation',        // When an MCP server requests user input
  'ElicitationResult',  // After a user responds to an MCP elicitation
  'ConfigChange',       // When configuration files change during a session
  'WorktreeCreate',     // Create an isolated worktree
  'WorktreeRemove',     // Remove a previously created worktree
  'InstructionsLoaded', // When an instruction file (CLAUDE.md) is loaded
  'CwdChanged',         // After the working directory changes
  'FileChanged',        // When a watched file changes
] as const
```

Each event has **metadata** defined in `hooksConfigManager.ts` that describes:
- A human-readable summary
- Detailed description with exit code semantics
- Optional `matcherMetadata` indicating what field the matcher filters on

For example, PreToolUse matches on `tool_name`, Notification matches on
`notification_type`, and SessionStart matches on `source` (startup/resume/clear/compact).

```typescript
// src/utils/hooks/hooksConfigManager.ts -- event metadata (abbreviated)
PreToolUse: {
  summary: 'Before tool execution',
  description:
    'Exit code 0 - stdout/stderr not shown\n'
    'Exit code 2 - show stderr to model and block tool call\n'
    'Other exit codes - show stderr to user only but continue',
  matcherMetadata: {
    fieldToMatch: 'tool_name',
    values: toolNames,    // dynamically populated from registered tools
  },
},
```

### Exit Code Protocol

Every hook event follows a consistent exit code convention:
- **Exit 0**: Success. Behavior varies by event (stdout may go to model, user, or nowhere).
- **Exit 2**: Blocking error. Stderr is shown to the model, and the operation may be blocked.
- **Other codes**: Non-blocking error. Stderr shown to user only; execution continues.

---

## 2. Hook Definition in Settings

Hooks are configured in `.claude/settings.json` under a `hooks` key. The structure
uses a **two-level nesting**: event type -> array of matcher configs -> array of hooks.

### Schema Architecture

The schema is defined in `src/schemas/hooks.ts` as a discriminated union of four
hook types:

```typescript
// src/schemas/hooks.ts -- the four hook types
const BashCommandHookSchema = z.object({
  type: z.literal('command'),
  command: z.string(),        // Shell command to execute
  if: IfConditionSchema(),    // Optional permission-rule-syntax filter
  shell: z.enum(SHELL_TYPES).optional(),  // 'bash' or 'powershell'
  timeout: z.number().positive().optional(),
  statusMessage: z.string().optional(),
  once: z.boolean().optional(),     // Run once then remove
  async: z.boolean().optional(),    // Run in background
  asyncRewake: z.boolean().optional(), // Background + wake model on exit 2
})

const PromptHookSchema = z.object({
  type: z.literal('prompt'),
  prompt: z.string(),         // Prompt with $ARGUMENTS placeholder
  if: IfConditionSchema(),
  timeout: z.number().positive().optional(),
  model: z.string().optional(),  // e.g., "claude-sonnet-4-6"
  statusMessage: z.string().optional(),
  once: z.boolean().optional(),
})

const HttpHookSchema = z.object({
  type: z.literal('http'),
  url: z.string().url(),      // URL to POST hook input JSON to
  if: IfConditionSchema(),
  timeout: z.number().positive().optional(),
  headers: z.record(z.string(), z.string()).optional(),
  allowedEnvVars: z.array(z.string()).optional(),
  statusMessage: z.string().optional(),
  once: z.boolean().optional(),
})

const AgentHookSchema = z.object({
  type: z.literal('agent'),
  prompt: z.string(),         // Verification prompt
  if: IfConditionSchema(),
  timeout: z.number().positive().optional(),
  model: z.string().optional(),
  statusMessage: z.string().optional(),
  once: z.boolean().optional(),
})
```

These are combined into the discriminated union:

```typescript
export const HookCommandSchema = lazySchema(() =>
  z.discriminatedUnion('type', [
    BashCommandHookSchema,
    PromptHookSchema,
    AgentHookSchema,
    HttpHookSchema,
  ])
)
```

### Matcher Configuration

Each event maps to an array of matcher configs. The matcher is a string pattern
that filters when the hooks fire:

```typescript
export const HookMatcherSchema = lazySchema(() =>
  z.object({
    matcher: z.string().optional(),  // Pattern to match (e.g., tool name "Write")
    hooks: z.array(HookCommandSchema()),
  })
)

export const HooksSchema = lazySchema(() =>
  z.partialRecord(z.enum(HOOK_EVENTS), z.array(HookMatcherSchema()))
)
```

### Example Configuration

```json
{
  "hooks": {
    "PreToolUse": [
      {
        "matcher": "Bash",
        "hooks": [
          {
            "type": "command",
            "command": "python3 /path/to/security-check.py",
            "timeout": 10
          }
        ]
      }
    ],
    "PostToolUse": [
      {
        "matcher": "Write",
        "hooks": [
          {
            "type": "command",
            "command": "echo '{\"decision\": \"approve\"}'",
            "statusMessage": "Running post-write checks..."
          }
        ]
      }
    ],
    "Stop": [
      {
        "hooks": [
          {
            "type": "agent",
            "prompt": "Verify that unit tests ran and passed."
          }
        ]
      }
    ]
  }
}
```

### The `if` Condition Filter

Before even spawning a hook process, the `if` field uses **permission rule syntax**
to pre-filter. For example, `"if": "Bash(git *)"` only fires the hook when the
Bash tool is called with a git command. This avoids process spawn overhead for
irrelevant invocations.

---

## 3. Hook Execution

### The Core Engine: `executeHooks()`

The central function is `executeHooks()` in `src/utils/hooks.ts` (~300 lines). It is
an async generator that yields `AggregatedHookResult` objects:

```typescript
// src/utils/hooks.ts (line ~1952)
async function* executeHooks({
  hookInput,
  toolUseID,
  matchQuery,
  signal,
  timeoutMs = TOOL_HOOK_EXECUTION_TIMEOUT_MS,  // 10 minutes default
  toolUseContext,
  messages,
  forceSyncExecution,
  requestPrompt,
  toolInputSummary,
}: { ... }): AsyncGenerator<AggregatedHookResult> {
```

The execution flow is:

1. **Trust check**: Skip all hooks if workspace trust not accepted (security measure).
2. **Match hooks**: Call `getMatchingHooks()` to find hooks matching the event + query.
3. **Fast path for internal hooks**: If all hooks are callbacks (analytics, attribution),
   execute them without the full machinery.
4. **Yield progress messages**: For each hook, yield a progress indicator to the UI.
5. **Parallel execution**: All hooks run in parallel via `Promise.all()`, each with
   individual timeouts.
6. **Aggregate results**: Combine individual hook results into `AggregatedHookResult`.

### getMatchingHooks()

This function (line ~1603) determines which hooks fire for a given event:

```typescript
// src/utils/hooks.ts (line ~1603)
export async function getMatchingHooks(
  appState: AppState | undefined,
  sessionId: string,
  hookEvent: HookEvent,
  hookInput: HookInput,
  tools?: Tools,
): Promise<MatchedHook[]> {
  const hookMatchers = getHooksConfig(appState, sessionId, hookEvent)

  // Determine match query from event type
  let matchQuery: string | undefined
  switch (hookInput.hook_event_name) {
    case 'PreToolUse':
    case 'PostToolUse':
    case 'PostToolUseFailure':
    case 'PermissionRequest':
    case 'PermissionDenied':
      matchQuery = hookInput.tool_name   // Match on tool name
      break
    case 'SessionStart':
      matchQuery = hookInput.source      // Match on "startup" | "resume" | etc.
      break
    case 'Notification':
      matchQuery = hookInput.notification_type
      break
    // ... other event types
  }

  // Filter matchers by pattern
  const filteredMatchers = matchQuery
    ? hookMatchers.filter(
        matcher => !matcher.matcher || matchesPattern(matchQuery, matcher.matcher)
      )
    : hookMatchers  // No matcher = fire for all
```

Hooks are then deduplicated by type (command, prompt, agent, http) to prevent
the same hook from firing twice when inherited from multiple settings scopes.

### executePreToolHooks()

This is the public entry point for pre-tool-use hooks:

```typescript
// src/utils/hooks.ts (line ~3394)
export async function* executePreToolHooks<ToolInput>(
  toolName: string,
  toolUseID: string,
  toolInput: ToolInput,
  toolUseContext: ToolUseContext,
  permissionMode?: string,
  signal?: AbortSignal,
  timeoutMs: number = TOOL_HOOK_EXECUTION_TIMEOUT_MS,
): AsyncGenerator<AggregatedHookResult> {
  // Early exit if no hooks registered for PreToolUse
  if (!hasHookForEvent('PreToolUse', appState, sessionId)) {
    return
  }

  // Build the hook input with base context
  const hookInput: PreToolUseHookInput = {
    ...createBaseHookInput(permissionMode, undefined, toolUseContext),
    hook_event_name: 'PreToolUse',
    tool_name: toolName,
    tool_input: toolInput,
    tool_use_id: toolUseID,
  }

  yield* executeHooks({
    hookInput,
    toolUseID,
    matchQuery: toolName,
    signal,
    timeoutMs,
    toolUseContext,
  })
}
```

### executePostToolHooks()

Similarly for post-tool-use, but includes the tool response:

```typescript
// src/utils/hooks.ts (line ~3450)
export async function* executePostToolHooks<ToolInput, ToolResponse>(
  toolName: string,
  toolUseID: string,
  toolInput: ToolInput,
  toolResponse: ToolResponse,
  toolUseContext: ToolUseContext,
  permissionMode?: string,
  signal?: AbortSignal,
  timeoutMs: number = TOOL_HOOK_EXECUTION_TIMEOUT_MS,
): AsyncGenerator<AggregatedHookResult> {
  const hookInput: PostToolUseHookInput = {
    ...createBaseHookInput(permissionMode, undefined, toolUseContext),
    hook_event_name: 'PostToolUse',
    tool_name: toolName,
    tool_input: toolInput,
    tool_response: toolResponse,
    tool_use_id: toolUseID,
  }

  yield* executeHooks({ hookInput, toolUseID, matchQuery: toolName, ... })
}
```

### Shell Command Spawning

For `command`-type hooks, the engine spawns a child process via `child_process.spawn()`:
- The hook input JSON is piped to **stdin**
- **stdout** is captured and parsed for JSON responses
- **stderr** is captured for error messages
- The process runs with the session's environment variables
- A per-hook timeout aborts the process if it hangs

---

## 4. Hook JSON I/O Schema

### Input Schema (What Hooks Receive)

Every hook receives a **JSON object on stdin** with a common base plus event-specific
fields:

```typescript
// Base fields (always present)
{
  session_id: string,
  transcript_path: string,
  cwd: string,
  hook_event_name: HookEvent,
  permission_mode?: string,
  agent_id?: string,      // Present when called from a subagent
  agent_type?: string,
}

// PreToolUse adds:
{
  tool_name: string,      // e.g., "Bash", "Write", "Edit"
  tool_input: object,     // The tool's input arguments
  tool_use_id: string,
}

// PostToolUse adds:
{
  tool_name: string,
  tool_input: object,
  tool_response: object,  // The tool's output
  tool_use_id: string,
}

// Notification adds:
{
  message: string,
  title?: string,
  notification_type: string,
}
```

### Output Schema (What Hooks Return)

Hook output is read from **stdout** and must be valid JSON matching the
`hookJSONOutputSchema`. There are two response types:

#### Sync Response (blocking)

```typescript
// src/types/hooks.ts -- syncHookResponseSchema
{
  continue?: boolean,         // false = stop Claude's response
  suppressOutput?: boolean,   // true = hide stdout from transcript
  stopReason?: string,        // message shown when continue is false
  decision?: 'approve' | 'block',
  reason?: string,            // explanation for the decision
  systemMessage?: string,     // warning shown to user
  hookSpecificOutput?: {
    hookEventName: 'PreToolUse',
    permissionDecision?: 'allow' | 'deny' | 'ask',
    permissionDecisionReason?: string,
    updatedInput?: Record<string, unknown>,  // modify tool input!
    additionalContext?: string,
  } | {
    hookEventName: 'PostToolUse',
    additionalContext?: string,
    updatedMCPToolOutput?: unknown,  // modify MCP tool output!
  } | {
    hookEventName: 'PermissionRequest',
    decision: {
      behavior: 'allow' | 'deny',
      updatedInput?: Record<string, unknown>,
      updatedPermissions?: PermissionUpdate[],
      message?: string,
    },
  }
  // ... more event-specific variants
}
```

#### Async Response (non-blocking)

```typescript
{
  async: true,
  asyncTimeout?: number,  // polling timeout in ms (default 15000)
}
```

When a hook returns `{async: true}`, the process is registered in the
`AsyncHookRegistry` and polled for completion later.

### Validation

```typescript
// src/utils/hooks.ts (line ~382)
function validateHookJson(jsonString: string):
  { json: HookJSONOutput } | { validationError: string } {
  const parsed = jsonParse(jsonString)
  const validation = hookJSONOutputSchema().safeParse(parsed)
  if (validation.success) {
    return { json: validation.data }
  }
  // Return formatted Zod validation errors
}
```

If stdout does not start with `{`, it is treated as **plain text** rather than JSON.
This allows simple hooks to just print messages without conforming to the schema.

---

## 5. Hook Responses: Allow, Deny, Modify

The `processHookJSONOutput()` function (line ~489) translates validated hook JSON
into `HookResult` objects that control Claude's behavior:

### Permission Decisions

```typescript
// src/utils/hooks.ts (line ~489)
function processHookJSONOutput({ json, command, ... }): Partial<HookResult> {
  const result: Partial<HookResult> = {}

  // Top-level decision field
  if (json.decision) {
    switch (json.decision) {
      case 'approve':
        result.permissionBehavior = 'allow'
        break
      case 'block':
        result.permissionBehavior = 'deny'
        result.blockingError = {
          blockingError: json.reason || 'Blocked by hook',
          command,
        }
        break
    }
  }

  // PreToolUse-specific permission via hookSpecificOutput
  if (json.hookSpecificOutput?.hookEventName === 'PreToolUse') {
    switch (json.hookSpecificOutput.permissionDecision) {
      case 'allow':
        result.permissionBehavior = 'allow'
        break
      case 'deny':
        result.permissionBehavior = 'deny'
        result.blockingError = { ... }
        break
      case 'ask':
        result.permissionBehavior = 'ask'  // Defer to user
        break
    }
  }
}
```

### Input Modification

PreToolUse hooks can **modify the tool's input** before execution:

```typescript
// In processHookJSONOutput:
if (json.hookSpecificOutput.updatedInput) {
  result.updatedInput = json.hookSpecificOutput.updatedInput
}
```

This is powerful: a hook can rewrite a Bash command, change a file path in a Write
tool call, or add parameters to any tool invocation.

### Continuation Control

```typescript
if (syncJson.continue === false) {
  result.preventContinuation = true
  if (syncJson.stopReason) {
    result.stopReason = syncJson.stopReason
  }
}
```

Setting `continue: false` stops Claude from continuing its response after the
current turn.

### Context Injection

Multiple event types support `additionalContext`, which gets appended to the
conversation as a system message visible to the model:

```typescript
case 'PostToolUse':
  result.additionalContext = json.hookSpecificOutput.additionalContext
  break
```

---

## 6. registerFrontmatterHooks()

Skills and agents can declare hooks in their frontmatter. These get registered
as **session-scoped hooks** that live only during the skill/agent execution.

```typescript
// src/utils/hooks/registerFrontmatterHooks.ts
export function registerFrontmatterHooks(
  setAppState: (updater: (prev: AppState) => AppState) => void,
  sessionId: string,
  hooks: HooksSettings,
  sourceName: string,
  isAgent: boolean = false,
): void {
  if (!hooks || Object.keys(hooks).length === 0) {
    return
  }

  let hookCount = 0

  for (const event of HOOK_EVENTS) {
    const matchers = hooks[event]
    if (!matchers || matchers.length === 0) {
      continue
    }

    // KEY: For agents, convert Stop -> SubagentStop
    // because subagents trigger SubagentStop, not Stop
    let targetEvent: HookEvent = event
    if (isAgent && event === 'Stop') {
      targetEvent = 'SubagentStop'
    }

    for (const matcherConfig of matchers) {
      const matcher = matcherConfig.matcher ?? ''
      for (const hook of matcherConfig.hooks) {
        addSessionHook(setAppState, sessionId, targetEvent, matcher, hook)
        hookCount++
      }
    }
  }
}
```

Key design decisions:
- **Stop -> SubagentStop conversion**: When an agent registers a Stop hook, it
  is automatically converted to SubagentStop because subagents fire SubagentStop
  (not Stop) when they complete.
- **Session scoping**: Each hook is stored under the agent/skill's session ID,
  so cleanup is automatic when the session ends.
- **Transparent integration**: Frontmatter hooks participate in the same matching
  and execution pipeline as settings hooks.

---

## 7. sessionHooks -- Per-Session Hook State

Session hooks provide **ephemeral, in-memory** hooks that cannot be persisted to
`settings.json`. There are two kinds:

### Command/Prompt Hooks (via addSessionHook)

```typescript
// src/utils/hooks/sessionHooks.ts
export function addSessionHook(
  setAppState: ...,
  sessionId: string,
  event: HookEvent,
  matcher: string,
  hook: HookCommand,
  onHookSuccess?: OnHookSuccess,
  skillRoot?: string,
): void {
  addHookToSession(setAppState, sessionId, event, matcher, hook, onHookSuccess, skillRoot)
}
```

### Function Hooks (via addFunctionHook)

Function hooks are TypeScript callbacks that run in-process. They are used for
validation checks (e.g., ensuring structured output):

```typescript
export type FunctionHook = {
  type: 'function'
  id?: string            // For removal
  timeout?: number
  callback: FunctionHookCallback  // (messages, signal?) => boolean
  errorMessage: string
  statusMessage?: string
}

export function addFunctionHook(
  setAppState: ...,
  sessionId: string,
  event: HookEvent,
  matcher: string,
  callback: FunctionHookCallback,
  errorMessage: string,
  options?: { timeout?: number; id?: string },
): string {
  const id = options?.id || `function-hook-${Date.now()}-${Math.random()}`
  const hook: FunctionHook = {
    type: 'function',
    id,
    timeout: options?.timeout || 5000,
    callback,
    errorMessage,
  }
  addHookToSession(setAppState, sessionId, event, matcher, hook)
  return id
}
```

### State Storage

Session hooks use a **mutable Map** for performance under high concurrency:

```typescript
// Map (not Record) so .set/.delete don't change the container's identity.
// This matters under high-concurrency: parallel() with N schema-mode agents
// fires N addFunctionHook calls in one synchronous tick. With a Record + spread,
// each call cost O(N) to copy (O(N^2) total) plus fired all ~30 store listeners.
// With Map: .set() is O(1), return prev means zero listener fires.
export type SessionHooksState = Map<string, SessionStore>
```

### Lifecycle

- `addSessionHook()` / `addFunctionHook()`: Register hooks for a session
- `removeSessionHook()` / `removeFunctionHook()`: Remove specific hooks
- `getSessionHooks()`: Retrieve non-function hooks for an event
- `getSessionFunctionHooks()`: Retrieve function hooks for an event
- `clearSessionHooks()`: Remove all hooks when session ends

---

## 8. AsyncHookRegistry -- Non-Blocking Hooks

The `AsyncHookRegistry` (`src/utils/hooks/AsyncHookRegistry.ts`) manages hooks that
run in the background while Claude continues processing:

### Registration

When a hook returns `{async: true}`, it enters the registry:

```typescript
// src/utils/hooks/AsyncHookRegistry.ts
export function registerPendingAsyncHook({
  processId, hookId, asyncResponse, hookName, hookEvent, command, shellCommand,
}: { ... }): void {
  const timeout = asyncResponse.asyncTimeout || 15000  // Default 15s
  pendingHooks.set(processId, {
    processId,
    hookId,
    hookName,
    hookEvent,
    command,
    startTime: Date.now(),
    timeout,
    responseAttachmentSent: false,
    shellCommand,
    stopProgressInterval,
  })
}
```

### Polling

The query loop periodically checks for completed async hooks:

```typescript
export async function checkForAsyncHookResponses(): Promise<Array<{
  processId: string
  response: SyncHookJSONOutput
  hookName: string
  hookEvent: HookEvent
  stdout: string
  stderr: string
  exitCode?: number
}>> {
  // For each pending hook:
  // 1. Check if shell command completed
  // 2. Parse stdout for JSON response
  // 3. Return responses and clean up completed hooks
}
```

### AsyncRewake Pattern

A special variant, `asyncRewake`, runs in the background but **wakes the model**
if the hook exits with code 2 (blocking error):

```typescript
// src/utils/hooks.ts (line ~184)
function executeInBackground({ ..., asyncRewake, ... }) {
  if (asyncRewake) {
    void shellCommand.result.then(async result => {
      if (result.code === 2) {
        // Enqueue as task-notification to wake the model
        enqueuePendingNotification({
          value: wrapInSystemReminder(
            `Stop hook blocking error from command "${hookName}": ${stderr || stdout}`
          ),
          mode: 'task-notification',
        })
      }
    })
    return true
  }
  // Normal async: register in AsyncHookRegistry for polling
  registerPendingAsyncHook({ ... })
}
```

### Cleanup

```typescript
export async function finalizePendingAsyncHooks(): Promise<void> {
  const hooks = Array.from(pendingHooks.values())
  await Promise.all(hooks.map(async hook => {
    if (hook.shellCommand?.status === 'completed') {
      await finalizeHook(hook, result.code, 'success'/'error')
    } else {
      hook.shellCommand?.kill()
      await finalizeHook(hook, 1, 'cancelled')
    }
  }))
  pendingHooks.clear()
}
```

---

## Architecture Summary

```
.claude/settings.json                    Skill Frontmatter
        |                                       |
        v                                       v
   HooksSchema                        registerFrontmatterHooks()
   (Zod validation)                             |
        |                                       v
        +----------> getHooksConfig() <---- sessionHooks
                           |                 (Map<sessionId, SessionStore>)
                           v
                    getMatchingHooks()
                    (pattern matching + dedup)
                           |
                           v
                     executeHooks()
                     (async generator)
                           |
             +-------------+-------------+
             |             |             |
             v             v             v
         command        prompt        callback
      (spawn shell)  (LLM eval)   (in-process)
             |             |             |
             v             v             v
      parseHookOutput  hookResponse   HookResult
             |             |             |
             +------+------+------+------+
                    |             |
                    v             v
            sync response    async response
         (processHookJSON)  (AsyncHookRegistry)
                    |             |
                    v             v
          AggregatedHookResult   poll later
          (permission, context,
           blocking errors)
```

---

## Key Design Insights

1. **Defense in depth**: Every hook execution path checks workspace trust first.
   This prevents RCE from malicious `.claude/settings.json` in cloned repos.

2. **Async generator pattern**: `executeHooks()` is an async generator, allowing
   it to yield progress updates incrementally while hooks run in parallel.

3. **Performance optimization**: Internal-only hooks (analytics, attribution) take
   a fast path that skips span/progress/JSON overhead (measured: -70% latency).

4. **Mutable Map for session hooks**: Uses Map instead of Record/spread to avoid
   O(N^2) copying under high concurrency from parallel agents.

5. **Four hook types**: The `command`/`prompt`/`agent`/`http` taxonomy lets users
   choose the right tool: shell scripts for simple checks, LLM evaluation for
   semantic analysis, agentic verification for complex multi-step checks, and
   HTTP webhooks for external service integration.

6. **Deduplication**: Hooks inherited from multiple settings scopes are deduplicated
   by their content (command string, prompt text, URL) to prevent double-firing.
