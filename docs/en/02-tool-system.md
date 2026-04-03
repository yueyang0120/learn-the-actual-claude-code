# Chapter 2: The Tool System

The agent loop described in Chapter 1 decides _when_ to call tools. This chapter describes _what_ a tool is — its interface, its behavioral flags, how tools are registered and assembled into the pool that the model sees. The tool system is the contract between Claude Code's runtime and the dozens of capabilities it offers.

## The Problem

A coding assistant needs many tools: file reading, file writing, shell execution, web search, LSP queries, and more. Each tool has different safety characteristics. Reading a file is harmless; running `rm -rf /` is catastrophic. Some tools can run concurrently; others must be serialized. Some are always available; others depend on feature flags or external servers.

A naive approach — a switch statement with one case per tool name — does not scale. Adding a new tool would require modifying the dispatch logic, the permission logic, the concurrency logic, and the prompt assembly logic. With 20+ built-in tools and an unlimited number of MCP tools, this approach would produce unmaintainable code within months. The system needs a uniform interface that encapsulates all of these concerns per tool, so that the core loop remains oblivious to what any individual tool does.

There is a further subtlety: the same tool can have different safety characteristics depending on its _input_. The Bash tool running `ls` is read-only; the Bash tool running `rm` is destructive. A static type-level annotation cannot capture this. The safety classification must be a function of the input, evaluated at call time rather than at registration time.

## How Claude Code Solves It

### The Tool Interface

Every tool in Claude Code implements a generic `Tool` interface defined in `Tool.ts` (792 lines). This file is one of the most important type definitions in the codebase: it establishes the contract that every tool — built-in or external — must satisfy. The interface contains over 30 fields, but the conceptually important ones are:

```typescript
// src/Tool.ts — simplified interface
interface Tool {
  name: string
  inputSchema: object
  isReadOnly(input): boolean
  isConcurrencySafe(input): boolean
  call(input, context: ToolUseContext): AsyncGenerator<ToolResult>
  checkPermissions(input, context): PermissionResult
  prompt(): string
}
```

Several aspects of this design deserve attention.

**`isReadOnly(input)` and `isConcurrencySafe(input)` are functions, not booleans.** They receive the tool's input and return a classification for that specific invocation. This is how the Bash tool can report itself as read-only when the command is `ls` but read-write when the command is `rm`. The orchestration layer (Chapter 3) calls these functions at dispatch time to decide whether a given tool invocation can run in parallel with others.

The implementation for a tool like Bash inspects the command string to make this determination. A command beginning with `cat`, `ls`, `head`, or `grep` is classified as read-only; commands containing `>`, `>>`, `rm`, `mv`, or `chmod` are classified as read-write. The classification is necessarily heuristic — it cannot parse arbitrary shell pipelines with perfect accuracy — but it errs on the side of caution. An unrecognized command is treated as read-write. This heuristic approach is pragmatic: a full shell parser would be complex and fragile, while the heuristic handles the common cases correctly and defaults safely for edge cases.

**`call()` is an AsyncGenerator.** Like the agent loop itself, tool execution uses generators rather than promises. A tool can yield intermediate progress events (useful for long-running operations like shell commands that produce output over time) before yielding its final result. The generator protocol also allows the runtime to abort a tool mid-execution via `.return()`. For a shell command that is running indefinitely, this provides clean cancellation without requiring explicit timeout logic inside the tool. The generator also enables the UI to show partial output as a tool runs — when a test suite produces output line by line, the user sees it incrementally rather than all at once when the suite finishes.

**`prompt()` returns a string.** Each tool contributes its own section to the system prompt (Chapter 4), describing its capabilities and usage conventions to the model. This keeps tool-specific prompt text co-located with tool-specific code rather than centralized in a single prompt file. When a tool's behavior changes — for instance, when a new flag is added to the Bash tool — the prompt text can be updated in the same file, ensuring documentation stays in sync with implementation.

The prompt text is not just a description; it often contains instructions that shape the model's behavior. For example, the FileRead tool's prompt might instruct the model to request specific line ranges for large files rather than reading the entire file, directly influencing how the model uses the tool and how much context budget is consumed.

**`checkPermissions(input, context)` is separate from `call()`.** Permission checking happens before execution and can return `allow`, `deny`, or `ask` (prompt the user). Separating it from the call itself means the permission system can be tested, audited, and overridden independently. It also means the orchestration layer can check permissions for all tools in a batch before executing any of them, allowing it to prompt the user once for multiple tool calls rather than interrupting execution repeatedly.

The permission result can also carry metadata: when the result is `ask`, it includes a human-readable description of what the tool intends to do ("Write 45 lines to /src/app.tsx"), which is displayed in the permission prompt. This description is generated by the tool itself, not by the orchestration layer, because only the tool knows how to interpret its own input in human terms.

### Additional Interface Fields

Beyond the core fields shown above, the `Tool` interface includes roughly two dozen additional fields. These fall into categories: identity fields, behavioral flags, and integration markers. The most significant:

- `aliases`: Alternative names the model can use to invoke the tool. This handles cases where the model generates a plausible but non-canonical tool name (e.g., `ReadFile` instead of `FileRead`). Aliases are checked before fuzzy matching, making them the preferred mechanism for handling known name variations.
- `searchHint`: Text used for fuzzy-matching when the model generates a completely unrecognized tool name. The runtime computes edit distance between the generated name and each tool's name and searchHint, returning the closest match if it is within a configurable threshold. This graceful degradation means tool calls rarely fail due to minor naming errors.
- `shouldDefer`: Whether execution should be deferred to a later phase. Some tools (like those that modify the conversation itself or update the system prompt) must run after all other tools in the batch have completed, because their effects depend on the results of other tools.
- `isMcp` and `isLsp`: Boolean flags indicating whether the tool is backed by MCP (Model Context Protocol) or LSP (Language Server Protocol). These flags affect error handling (MCP tools may have transient server errors that warrant retries), timeout behavior (LSP operations have different latency profiles than local tools), and how the tool is displayed in the UI (external tools show their server origin).
- `isDestructive`: A static flag for tools that are _always_ destructive regardless of input. Unlike `isReadOnly` (which is input-dependent and defaults to false), `isDestructive` is a fixed property. Tools marked destructive trigger additional confirmation prompts beyond the normal permission system — the user must explicitly approve each invocation, even in auto-approve mode.
- `strict`: Whether to use strict mode for input schema validation. When enabled, the tool rejects any input that does not exactly match its schema, ensuring type safety. When disabled, extra fields are tolerated — useful for MCP tools where schema compliance may be imperfect due to version mismatches between server and client.

### Fail-Closed Defaults

Tools are constructed via a `buildTool()` factory that provides default values for every behavioral flag:

```typescript
// src/Tool.ts — buildTool defaults (conceptual)
function buildTool(partial: Partial<Tool>): Tool {
  return {
    isEnabled: () => false,           // disabled until opted in
    isReadOnly: () => false,          // assumed side-effecting
    isConcurrencySafe: () => false,   // assumed unsafe for concurrency
    isDestructive: false,             // not marked destructive
    isMcp: false,
    isLsp: false,
    strict: false,
    ...partial,                       // override with provided values
  }
}
```

This is a fail-closed design. If a tool author forgets to set a flag, the tool will be disabled, treated as having side effects, and serialized. The failure mode is overly cautious rather than overly permissive.

Consider the alternative: if `isEnabled` defaulted to `true`, a tool with an incomplete definition could run with no safety checks. If `isConcurrencySafe` defaulted to `true`, a tool with unintended side effects could corrupt state when run in parallel. If `isReadOnly` defaulted to `true`, a destructive tool could bypass permission checks entirely. Each of these scenarios is worse than the conservative default.

The `buildTool()` pattern also provides a single place where defaults are defined, making it straightforward to audit what happens when a field is omitted. A grep for `buildTool` finds every tool definition in the codebase; inspecting the factory function reveals the default behavior for any omitted field.

This design is particularly important for MCP tools, which are defined by external servers and may have incomplete or incorrect metadata. An MCP server that fails to declare its tool as read-write will have that tool treated as read-write by default — the correct conservative assumption. The fail-closed defaults act as a safety net for the entire MCP ecosystem.

### The ToolUseContext Object

Every tool call receives a `ToolUseContext` — a large context object (~40 fields) threaded through the entire tool execution pipeline. The fields fall into several logical groups:

- **Options**: Configuration such as the current working directory, model parameters, feature flags, and permission mode (auto-approve vs. ask). Also includes the user's configured hooks, the session's MCP server list, and output format preferences.
- **State**: The current message array, abort signal, turn count, and session ID. Also includes the set of files that have been read or written during this session, which some tools use to provide more accurate caching behavior.
- **UI Callbacks**: Functions to update the terminal display — progress indicators, permission prompts, streaming output renderers. These are injected as callbacks rather than imported directly, which decouples tool execution from any specific UI framework. This decoupling is what allows the same tool implementations to work in both the interactive CLI and in headless/API mode.
- **Tracking**: Telemetry hooks for recording execution time, token usage, and error rates. These hooks are no-ops in open-source builds but active in internal builds, implemented via the same build-time feature gate mechanism described in Chapter 1.
- **Metadata**: Information about the current session, user identity (for permission scoping), and environment (OS, shell, git state). Also includes the conversation's UUID, which is used to scope file locks and temporary state.

Rather than passing these as separate arguments (which would make every tool signature unwieldy and every new field a breaking change), they are bundled into a single typed object. This makes it straightforward to add new context fields without modifying every tool's function signature — a significant advantage in a codebase where new context is frequently added as features evolve.

The `ToolUseContext` is constructed once per agent loop turn and shared (read-only) across all tool invocations in that turn. Tools should not mutate it; if a tool needs to communicate state changes back to the loop, it uses the `MessageUpdate` mechanism described in Chapter 3. This read-only convention is enforced by convention and code review rather than by TypeScript's `readonly` modifier, since deep readonly types add significant type-system complexity for marginal safety gains in a codebase where all tool authors are internal contributors or following the MCP protocol.

One notable aspect of the context object is that it includes the current message array — meaning tools can inspect the conversation history if needed. The FileWrite tool, for example, might check whether the file was recently read (by scanning for a prior FileRead result in the messages) to warn if the model is writing without having read the current contents. This introspection capability is powerful but used sparingly to avoid coupling tools to conversation structure.

### Tool Registration and Assembly

Tools are registered in `tools.ts` (389 lines). The `getAllBaseTools()` function returns the list of built-in tools, with conditional registration gated by feature flags:

```typescript
// src/tools.ts — conceptual structure
function getAllBaseTools(): Tool[] {
  const tools = [
    bashTool,
    fileReadTool,
    fileWriteTool,
    globTool,
    grepTool,
    lspTool,
    notebookEditTool,
    // ...approximately 20 built-in tools
  ]
  if (feature('NOTEBOOK_EDIT')) {
    tools.push(notebookEditTool)
  }
  if (feature('TASK_TOOL')) {
    tools.push(taskTool)
  }
  // ...
  return tools
}
```

The `feature()` function resolves at build time through Bun's bundler. When a feature flag is disabled, the entire conditional branch — including the tool's import — is dead-code eliminated from the build output. This means external (non-Anthropic) builds of Claude Code physically cannot contain internal-only tools, rather than merely hiding them behind a runtime check. The distinction matters for security: a runtime check can be bypassed by a motivated user; dead-code elimination cannot. It also matters for bundle size: unreachable tools and their dependencies are not included in the shipped binary.

The final tool pool is assembled by `assembleToolPool()`, which performs four steps in sequence:

1. **Collect built-in tools** from `getAllBaseTools()`. This is the base set of ~20 tools that ship with Claude Code.

2. **Add MCP tools** discovered from connected servers. MCP tools are named with a prefix convention: `mcp__<server>__<tool>` (e.g., `mcp__github__create_issue`). The double-underscore delimiter was chosen because it is unlikely to appear in natural tool or server names, making parsing unambiguous.

3. **Deduplicate by name.** Built-in tools take precedence over MCP tools with the same name, preventing an MCP server from shadowing a core tool. This is a security measure: a malicious MCP server cannot replace the `Bash` tool with a trojanized version.

4. **Sort alphabetically by name.** This ensures prompt cache stability (explained below).

The alphabetical sort is not cosmetic. Because the tool list is included in the system prompt (Chapter 4), a stable ordering ensures that the prompt's token sequence is identical across requests with the same tool set. This maximizes cache hit rates on the API's prompt caching layer.

Without stable ordering — if tools were in insertion order or hash-map iteration order — the prompt would differ between requests even when the tool set has not changed. Each different ordering would invalidate the cache, forcing the API to reprocess thousands of tokens of identical content. Alphabetical sorting eliminates this source of cache variance at zero runtime cost.

## Key Design Decisions

**Input-dependent behavioral flags.** The decision to make `isReadOnly` and `isConcurrencySafe` functions of the input — rather than static properties of the tool — is the single most important design choice in the tool system.

It enables a single `Bash` tool to serve all shell commands while still providing the orchestrator with accurate safety metadata per invocation. The alternative — separate `BashRead` and `BashWrite` tools — would double the tool count and complicate the model's tool selection. It would also force the model to predict at tool-selection time whether a command has side effects, which is a classification task better handled by deterministic heuristics than by the model's judgment. The model's job is to decide _what_ to do; the tool's job is to classify _how safe_ that action is.

**Fail-closed defaults via `buildTool()`.** Making the safe default the _absent_ default means that partially defined tools cannot accidentally run with elevated privileges. This is especially important in a system where third-party MCP tools can be added dynamically — a malformed MCP tool definition should not gain unintended capabilities through missing fields.

The fail-closed approach also means that the system is safe by default when new behavioral flags are added to the interface: existing tools that do not set the new flag get the conservative default automatically, requiring no updates to dozens of tool definitions.

**Tools own their prompt text.** Co-locating `prompt()` with the tool implementation means that when a tool's behavior changes, its documentation to the model changes in the same commit. A centralized prompt file would inevitably drift from reality as tools are modified independently by different contributors. The co-location pattern scales naturally: adding a new tool adds its documentation automatically; removing a tool removes its documentation. No coordination is required.

**A single large context object.** The `ToolUseContext` approach trades discoverability (one must look up the type to know what is available) for extensibility (new fields require no signature changes). In a rapidly evolving codebase where new context is added frequently, extensibility wins. The type system ensures that tools accessing non-existent fields will fail at compile time, mitigating the discoverability concern. The alternative — passing individual arguments — would create functions with 10+ parameters, which is worse for both readability and maintenance.

## In Practice

When the model emits a `tool_use` block — say, `{ name: "Bash", input: { command: "ls -la" } }` — the runtime looks up the `Bash` tool by name, calls `isReadOnly({ command: "ls -la" })` (which returns `true`), calls `isConcurrencySafe({ command: "ls -la" })` (which returns `true`), checks permissions, then invokes `call()`. Because this invocation is read-only and concurrency-safe, it can run in parallel with other safe tool calls in the same batch.

A subsequent `{ name: "Bash", input: { command: "npm install" } }` would return `false` for both flags, forcing serialized execution with a permission prompt. The user sees a confirmation dialog; only after approval does the command execute.

If the model generates an unrecognized tool name — say, `ReadFile` instead of `FileRead` — the runtime checks aliases first, then falls back to fuzzy matching via `searchHint`. In most cases, the correct tool is found and execution proceeds transparently. If no match is found, a structured error is returned to the model, which typically self-corrects on the next turn.

The tool system's uniformity also simplifies debugging. Every tool call follows the same path: lookup, classify, check permissions, execute. Telemetry events have the same shape regardless of which tool produced them. Permission prompts have the same format. Error messages follow the same structure. This consistency means that understanding one tool call means understanding all tool calls — there are no special cases hidden in the orchestration layer.

For tool authors (whether writing built-in tools or MCP server tools), the interface provides clear guidance: implement the required methods, set the behavioral flags accurately, and the runtime handles everything else — scheduling, permissions, streaming, error recovery, and prompt assembly.

## Summary

- Every tool implements a uniform `Tool` interface with ~30 fields covering identity, schema, behavior, execution, permissions, and prompt contribution. The interface is defined in `Tool.ts` (792 lines).
- `isReadOnly` and `isConcurrencySafe` are functions of the tool's input, enabling the same tool (notably Bash) to have different safety profiles per invocation. Classification is heuristic and errs on the side of caution.
- `buildTool()` provides fail-closed defaults: tools are disabled, side-effecting, and serialized unless they explicitly declare otherwise. This is especially important for MCP tools with potentially incomplete metadata.
- Tool registration in `tools.ts` (389 lines) is feature-gated at build time via Bun dead-code elimination; disabled tools are physically absent from the build output, not merely hidden.
- `assembleToolPool()` merges built-in and MCP tools, deduplicates (built-in wins over MCP to prevent shadowing), and sorts alphabetically for prompt cache stability.
- The `ToolUseContext` (~40 fields) bundles all runtime context into a single typed object, enabling extensibility without breaking tool signatures.
