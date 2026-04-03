# Chapter 1: The Agent Loop

Claude Code is, at its core, a loop. The user types a message, the model responds — possibly invoking tools — and the loop decides whether to continue or stop. Every feature described in later chapters (tools, permissions, context management) plugs into this loop. Understanding the loop is therefore prerequisite to understanding anything else.

## The Problem

An AI coding assistant must do more than answer questions. It must take actions — reading files, running commands, editing code — and chain those actions across multiple turns until a task is complete. This requires a runtime that can manage a mutable conversation, dispatch tool calls, handle errors, and decide when to keep going versus when to stop.

The naive implementation is straightforward: send messages to an API, get a response, check for tool calls, execute them, append results, repeat. But production realities complicate this picture considerably:

- **Startup latency matters.** Developers notice 300ms of delay. A CLI tool that feels sluggish on startup will not be adopted, regardless of its capabilities.
- **Conversations grow without bound.** A complex refactoring task can generate hundreds of tool calls and thousands of lines of output. Without active context management, the conversation will exceed the model's context window within a single session.
- **The model sometimes hits output token limits mid-sentence.** When generating a long file or a detailed explanation, the model's response may be truncated. The loop must detect this and continue seamlessly, without losing the partial output.
- **API rate limits produce 413 errors** when the conversation exceeds the context window. These must be retried gracefully after compaction, not surfaced to the user as failures.
- **Tool calls that are safe to parallelize should not block each other.** Running three independent file reads sequentially when they could run concurrently wastes time that compounds across a multi-turn session.

Claude Code addresses all of these concerns within a single, carefully structured agent loop.

## How Claude Code Solves It

The implementation spans three files: `cli.tsx` (302 lines) for the entry point, `QueryEngine.ts` (1,295 lines) for conversation state management, and `query.ts` (1,729 lines) for the loop itself. The following sections trace the path from CLI invocation through the loop's steady-state operation.

### Fast Startup

Before the loop can run, the CLI must boot. Claude Code is aggressive about startup time — a concern that might seem minor but compounds across hundreds of invocations per day for an active developer. The entry point in `cli.tsx` (302 lines) checks for trivial flags before doing any heavy imports:

```typescript
// src/entrypoints/cli.tsx ~L33-42
if (args.length === 1 && (args[0] === '--version' || args[0] === '-v')) {
    console.log(`${MACRO.VERSION} (Claude Code)`);
    return;
}
```

This fast path exits in under 5ms. Without it, the static import tree (which triggers side-effect I/O for configuration loading, telemetry initialization, and capability detection) would impose roughly 300ms of latency just to print a version string. The pattern applies to other trivial flags as well — `--help`, `--print-config`, and similar introspection commands all exit before the main initialization path.

The pattern continues in `main.tsx` (4,683 lines), which deliberately places side-effect imports — those that start I/O such as network checks, file reads, and capability probes — before static imports so that asynchronous work begins in parallel with module evaluation. This is not accidental: the import ordering is a performance optimization. Bun evaluates imports sequentially, so placing I/O-initiating imports first means network requests and file system operations are in flight while later modules are being parsed and evaluated. By the time the static initialization completes, much of the async setup has already finished.

The startup sequence also performs capability detection — determining what tools are available (is `git` installed? is `rg` available for grep?), what MCP servers are configured, and what permissions have been pre-approved. This detection happens in parallel with other initialization work, so its latency is largely hidden behind the time spent loading the configuration files and setting up the terminal UI.

The net effect of these optimizations is that Claude Code feels responsive from the moment it is invoked. The version check is instantaneous; interactive startup completes in a few hundred milliseconds; and the first API call can be dispatched before all initialization is finished.

### The Conversation Owner

The loop's state is owned by `QueryEngine`, a class defined in `QueryEngine.ts` (1,295 lines). This class is the central coordinator: it holds the mutable conversation (the message array), manages the system prompt, tracks token usage, and exposes a single entry point for submitting new user turns:

```typescript
// src/QueryEngine.ts ~L209-212
async *submitMessage(
  prompt: string | ContentBlockParam[],
  options?: { uuid?: string; isMeta?: boolean },
): AsyncGenerator<SDKMessage, void, unknown> {
```

`submitMessage` is an `AsyncGenerator`. This is a critical design choice: rather than returning a promise that resolves when the entire turn is finished, it yields events as they arrive. The caller (the CLI's React rendering layer) can process streaming tokens, tool-use blocks, and status updates incrementally. Events are typed — the caller can distinguish between text deltas, tool-use starts, tool-use completions, and status changes — allowing the UI to render each event type differently.

The generator protocol also provides natural backpressure — if the UI falls behind, the generator simply pauses. This matters because Claude Code renders tool outputs and streaming text in a terminal UI built with Ink (a React renderer for terminals). If the model generates faster than the terminal can render, the generator automatically throttles without any explicit flow-control logic. Cancellation is equally natural: calling `.return()` on the generator unwinds the loop cleanly, releasing resources without requiring a separate cancellation token or abort controller. When the user presses Ctrl+C, the generator's `.return()` method propagates through the entire call chain, aborting in-flight API requests and tool executions.

The `QueryEngine` also manages the conversation's lifecycle: persisting conversations to disk for session resumption, tracking which messages have been compacted, and maintaining the mapping between tool-use IDs and their results. This state management complexity is why `QueryEngine.ts` is one of the largest files in the codebase at 1,295 lines.

### Two Nested Generators

Inside `QueryEngine`, the actual loop logic lives in `query.ts` (1,729 lines) — the largest single file involved in the loop. The size reflects the number of edge cases the loop must handle: retries, compaction, recovery, hooks, streaming, and error wrapping all live here. The implementation uses two nested generators:

```typescript
// src/query.ts ~L219-239
export async function* query(params: QueryParams): AsyncGenerator<StreamEvent | Message | ...> {
  const terminal = yield* queryLoop(params, consumedCommandUuids)
  return terminal
}
```

The outer generator, `query()`, handles one-time setup and teardown. This includes parameter validation, initial message preparation, and establishing the set of already-consumed command UUIDs (to prevent replaying commands from previous sessions). The inner generator, `queryLoop()`, is the state machine that runs the actual loop iterations.

This separation keeps initialization logic cleanly separated from the per-turn logic. It also means the inner loop can be restarted independently (for example, after a context compaction that restructures the message array) without re-running initialization.

The `yield*` delegation from `query()` to `queryLoop()` transparently forwards all yielded events to the caller, so the two-generator structure is invisible from the outside — callers interact only with `query()`.

### The State Object

Each iteration of the loop operates on an explicit state object with 9 fields:

```typescript
// src/query.ts ~L241-280
let state: State = {
    messages: params.messages,
    toolUseContext: params.toolUseContext,
    autoCompactTracking: undefined,
    turnCount: 1,
    transition: undefined,
}
```

The `transition` field records _why_ the loop continued on each iteration. Possible values include `"tool_results"` (the normal case: tools produced results that need processing), `"reactive_compact_retry"` (context was too large, compaction was applied), and `"recovery"` (the model hit its output token limit). This field serves as an audit trail — when debugging unexpected loop behavior, the sequence of transition values tells the story of what happened and why.

Every time the loop decides to continue, a fresh state is constructed at the continue site rather than mutating the existing one. The code has 9 such continue sites — one for each transition type — and each constructs a new `State` with the appropriate `transition` value. This pattern makes it impossible for state from one iteration to accidentally leak into another.

The `toolUseContext` field carries the context object that gets threaded through tool execution (described in Chapter 2). The `autoCompactTracking` field monitors token usage across turns to decide when proactive compaction should trigger — it tracks both the running total of tokens consumed and the number of turns since the last compaction, allowing the system to compact based on either absolute size or growth rate.

The `turnCount` tracks iterations for enforcing the maximum turn limit, which prevents runaway loops that could consume unlimited API calls. The maximum is configurable but has a sensible default that allows complex multi-step tasks while preventing infinite loops from a model that repeatedly calls tools without making progress.

### The Main Loop

The `while(true)` loop inside `queryLoop` follows a fixed sequence each iteration. This sequence is the heartbeat of Claude Code — everything the user observes, from streaming text to tool execution to error recovery, is driven by this four-step cycle.

**Step 1: Pre-processing pipeline.** The message array passes through a chain of five compaction stages, each progressively more aggressive at reducing token count:

- `applyToolResultBudget` — Truncates individual tool results that exceed a per-result token budget. Large tool outputs (e.g., a `cat` of a 10,000-line file) are trimmed to a configurable maximum, with a notice appended so the model knows content was omitted.
- `snipCompact` — Removes content from messages that have been explicitly marked as snip-eligible. Messages can be flagged for snipping by earlier processing stages or by tools that know their output will become stale.
- `microcompact` — Applies lightweight compression to reduce message sizes without changing semantics. This includes removing redundant whitespace from tool results, collapsing repeated empty lines, and trimming trailing whitespace.
- `contextCollapse` — Merges adjacent messages of the same role and removes messages whose content has been fully snipped. This reduces the message count, which has a small but non-zero cost in the API's per-message overhead.
- `autocompact` — If total tokens exceed a configurable threshold (calculated as context window size minus `AUTOCOMPACT_BUFFER_TOKENS`), summarizes older messages using a separate, lightweight model call. The summary replaces the original messages, dramatically reducing token count while preserving the essential information. The summary model call is fast (typically under 1 second) because it operates on a focused task with a small output budget.

The first four stages are deterministic string operations with negligible cost (under 1ms combined). The fifth stage (`autocompact`) is conditional and involves a model call, so it only triggers when the token count crosses the threshold. Together, these stages ensure the context window is managed continuously rather than reactively.

This pipeline runs _before_ every API call, not just when context is full. The rationale is explained in the design decisions section below.

**Step 2: API call with streaming.** The request is sent to the Anthropic API with the processed message array, the system prompt (Chapter 4), and the tool definitions (Chapter 2).

As response chunks arrive over the wire, the streaming layer parses them in real time, extracting `tool_use` content blocks as soon as they are complete. The `StreamingToolExecutor` is the key component here: it begins executing tools _while the model is still generating text_, overlapping network latency with tool execution. If the model emits a `tool_use` block for a file read followed by more text, the file read starts immediately — it does not wait for the model to finish generating.

For a response that contains three tool calls and explanatory text, this pipelining can save several hundred milliseconds compared to waiting for the full response before starting any tool execution. The streaming layer also handles partial JSON: tool-use blocks arrive incrementally, and the parser accumulates fragments until a complete block is available. This is necessary because the API streams tokens one at a time, and a `tool_use` block's input JSON may span many tokens.

**Step 3: Tool dispatch.** Completed tool-use blocks are sent to the tool orchestration layer (Chapter 3), which handles permission checks, concurrency batching, and pre/post-execution hooks. The results — one per tool call, containing the tool's output and metadata — are appended to the conversation as tool-result messages.

The tool results become part of the conversation that will be sent on the next iteration. This is how the model "sees" the output of its tool calls: tool results appear as messages in the conversation, formatted according to the API's tool-result protocol. If a tool fails, the error message is formatted as a tool result too, giving the model the information it needs to adjust its approach.

**Step 4: Continuation decision.** The loop evaluates a decision table to determine what happens next. This is the branch point that determines whether the loop continues for another iteration or returns control to the user.

### The Continuation Decision Table

The decision logic can be summarized as follows:

| Condition | Action |
|---|---|
| No `tool_use` blocks in response | `completed` — return to user |
| Tool results present | `next_turn` — append results, continue loop |
| API returns 413 (context too large) | `reactive_compact_retry` — compact aggressively, retry |
| Model hit max output tokens | `recovery` — continue with recovery prompt (up to 3 times) |
| Stop hook fires | `blocking` — pause for user input |
| Turn count exceeds max | Return to user with truncation notice |

Each row in this table corresponds to a different `transition` value in the state object. The conditions are evaluated in priority order: a 413 error takes precedence over tool results (because the request failed and must be retried), and the max-turn limit takes precedence over everything (because it is a hard safety boundary against runaway loops).

The `blocking` transition for stop hooks deserves mention: this is how user-defined hooks (Chapter 3) can pause the loop. A stop hook might fire when the model attempts to make a potentially irreversible change, giving the user a chance to review before the loop continues. When the user approves, the loop resumes from the same state.

The max-output-tokens recovery is bounded by `MAX_OUTPUT_TOKENS_RECOVERY_LIMIT = 3`. If the model hits the output cap three times in succession, the loop gives up rather than burning tokens on a response that may be stuck in a generation loop. This constant was chosen empirically: genuine long responses (like generating a large file) almost always complete within three continuations, while degenerate cases (like the model repeating itself) rarely self-correct after three attempts.

The autocompact system maintains a buffer of `AUTOCOMPACT_BUFFER_TOKENS = 13_000` tokens to ensure there is always room for the model to produce a meaningful response before hitting context limits. This buffer accounts for both the model's output and the overhead of tool results that may be appended in the same turn. The buffer size was calibrated to accommodate a typical model response (1,000-4,000 tokens) plus several medium-sized tool results (file contents, command output) with headroom for variance.

The `reactive_compact_retry` path deserves special attention. When the API returns a 413 error, the conversation has exceeded the model's context window. Rather than failing with an error, the loop triggers an aggressive compaction pass — more aggressive than the routine pre-processing — and retries the request. This path exists because the routine compaction is conservative (to preserve context quality), and occasionally a burst of large tool results can push the conversation over the limit despite the buffer. The reactive path may summarize or remove entire conversation segments that the routine path would preserve, accepting some information loss in exchange for continued operation. From the user's perspective, the retry is invisible except for a slightly longer pause before the response arrives.

## Key Design Decisions

**AsyncGenerator over Promises.** The generator protocol provides streaming, backpressure, and cancellation (via `.return()`) in a single abstraction. A promise-based design would require separate event emitters for streaming, explicit cancellation tokens, and manual backpressure management — more moving parts for the same capability. The generator approach also composes naturally: `yield*` delegation lets the outer generator transparently forward events from the inner generator without intermediate buffering or event re-emission.

**Immutable state at continue sites.** Rather than mutating the loop state in place, each continuation constructs a new state object. This eliminates an entire class of bugs where stale state from a previous iteration leaks into the next — for example, a `turnCount` that was not incremented, or an `autoCompactTracking` that retained data from a pre-compaction state. The cost is a few extra object allocations per turn, which is negligible compared to API call latency (typically 500ms-2s per turn). This pattern also makes the code more readable: at any continue site, the complete set of state for the next iteration is visible in one place, rather than being spread across mutations throughout the function.

**Pre-processing on every turn, not just on overflow.** Running the compaction pipeline before every API call means context size is managed continuously rather than reactively. This avoids the cliff-edge scenario where a conversation suddenly exceeds the context window and requires an expensive emergency compaction. Continuous compaction also produces more predictable token budgets for the model, since the context size stays within a narrower range rather than oscillating between nearly-full and just-compacted. The lightweight stages (`snipCompact`, `microcompact`, `contextCollapse`) have negligible overhead — they are simple string operations that complete in under 1ms. Only `autocompact` involves a model call, and it triggers only when tokens exceed the threshold.

**Feature gates at build time.** Feature flags use `feature('FLAG_NAME')` which resolves at build time via Bun's bundler. Disabled features are dead-code eliminated from external builds entirely — no runtime conditional overhead, no accidental exposure of internal features. This is a stronger guarantee than runtime feature flags, which can be toggled or inspected by determined users. The build-time approach means that the open-source build of Claude Code is physically a different program from the internal build, not the same program with different settings. This has implications for the agent loop specifically: internal builds may have additional transition types, different compaction strategies, or experimental continuation policies that are entirely absent from the public build.

**Streaming tool execution.** Starting tool execution before the model finishes generating is an optimization that saves measurable latency on multi-tool turns. The risk — that the model might "change its mind" later in the response and the early-started tool becomes unnecessary — is mitigated by the fact that the API's tool-use protocol makes tool calls irrevocable once emitted. A `tool_use` block, once streamed, will not be retracted. The `StreamingToolExecutor` exploits this guarantee: as soon as a complete `tool_use` block is parsed from the stream, it is safe to begin execution. The alternative — waiting for the complete response before starting any tool — would add the model's remaining generation time to the latency of every multi-tool turn.

## In Practice

When a developer types a message in Claude Code, the response begins streaming within a few hundred milliseconds. If the model decides to read a file and run a command, those tool calls appear as they are extracted from the stream — the file read may complete before the model has finished generating the text that follows it. The loop continues autonomously — reading, editing, running tests — until the model produces a response with no tool calls, at which point the loop stops and the user sees the final answer.

If the conversation grows long (common during complex refactoring tasks), earlier tool results are quietly compacted to free up context space. The user does not see this happen; the only visible effect is that the model continues to function normally despite a conversation that might span dozens of turns and hundreds of tool calls. If the context grows so large that even routine compaction is insufficient, the reactive path catches the 413 error and applies emergency compaction before retrying — the user might notice a slightly longer pause, but the conversation continues rather than failing.

A concrete example: a developer asks Claude Code to "update all test files to use the new assertion library." The model reads the project configuration, identifies 12 test files, reads each one, edits each one, and runs the test suite. This might require 30+ tool calls across 5-6 loop iterations. The pre-processing pipeline keeps the context manageable throughout: early file-read results are compacted once the edits are done, since the model no longer needs the original file contents. The `StreamingToolExecutor` starts reading the next file while the model is still generating the edit for the current one. The entire operation completes in a fraction of the time it would take with serial execution and no context management.

## Summary

- The agent loop is a `while(true)` state machine inside two nested async generators, owned by `QueryEngine` (which also manages conversation persistence and token tracking).
- Each iteration runs a five-stage pre-processing pipeline (`applyToolResultBudget` through `autocompact`), makes a streaming API call, dispatches tools via the orchestration layer, and evaluates a continuation decision table with six possible outcomes.
- The `StreamingToolExecutor` overlaps tool execution with model generation, starting tools before the response is complete — exploiting the API's guarantee that emitted `tool_use` blocks are irrevocable.
- State is rebuilt at each of the 9 continue sites with a `transition` field recording the reason, providing a full audit trail of loop decisions.
- Context compaction runs proactively on every turn via lightweight stages, with emergency compaction as a fallback for 413 errors. The `AUTOCOMPACT_BUFFER_TOKENS = 13_000` constant ensures the model always has room to respond.
- Fast startup is achieved through early-exit fast paths, parallel I/O via side-effect import ordering, and concurrent capability detection.
