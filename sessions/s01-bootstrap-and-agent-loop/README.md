# Session 01: Bootstrap Sequence and Agent Loop

## Learning Objectives

- **Trace the full startup path**: from `cli.tsx` fast-path checks through `main.tsx` heavy initialization to the first API call.
- **Understand the generator-based streaming architecture**: the agent loop is *not* a simple `while True` loop -- it is an `AsyncGenerator` pipeline (`query()` / `queryLoop()`) that yields messages as they stream in.
- **Map the QueryEngine class**: the owner of conversation state that bridges the SDK/headless entry point to the inner `query()` generator.
- **Identify tool dispatch mechanics**: how `tool_use` blocks are extracted from streaming responses, dispatched (optionally in parallel via `StreamingToolExecutor`), and their results fed back into the next iteration.

## Real Source Files Covered

| File | Lines | Role |
|------|------:|------|
| `src/entrypoints/cli.tsx` | 302 | Bootstrap entrypoint with fast-path exits |
| `src/main.tsx` | 4,683 | Full CLI initialization, arg parsing, REPL launch |
| `src/QueryEngine.ts` | 1,295 | Conversation-level state owner, `submitMessage()` generator |
| `src/query.ts` | 1,729 | The inner agent loop (`query()` / `queryLoop()`) |

## What shareAI-lab (and most tutorials) Get Wrong

Most open-source "Claude Code clones" model the agent loop as:

```python
while True:
    response = client.messages.create(...)
    if no_tool_use(response):
        break
    results = run_tools(response)
    messages.append(results)
```

The real Claude Code does something fundamentally different:

1. **Generator-based streaming**: `query()` and `queryLoop()` are `async function*` generators that `yield` every message, stream event, tool result, and compaction boundary as they happen. The caller consumes them with `for await (const message of query(...))`.

2. **State machine with typed transitions**: The loop carries a `State` object that is rebuilt at each `continue` site. Each continuation records *why* it continued (`next_turn`, `reactive_compact_retry`, `max_output_tokens_recovery`, `stop_hook_blocking`, etc.) via a typed `transition` discriminant.

3. **Streaming tool execution**: Tools can start executing *while the model is still streaming*. `StreamingToolExecutor` launches tool work as soon as each `tool_use` block arrives, rather than waiting for the full response.

4. **Multi-layer compaction**: Before each API call, the message array passes through snip-compact, microcompact, context-collapse, and autocompact -- each one a separate pipeline stage with its own feature gate.

## Quick Start

```bash
# Run the reimplementation (requires ANTHROPIC_API_KEY in .env)
python sessions/s01-bootstrap-and-agent-loop/reimplementation.py
```

## Files in This Session

- `README.md` -- this file
- `SOURCE_ANALYSIS.md` -- deep annotated walkthrough of the real source
- `reimplementation.py` -- runnable Python reimplementation (~200 LOC)
