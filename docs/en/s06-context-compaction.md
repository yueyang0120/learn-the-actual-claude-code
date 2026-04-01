# Session 06 -- Context Compaction

s01 > s02 > s03 > s04 > s05 | **s06** > s07 > s08 > s09 > s10 | s11 > s12 > s13 > s14

---

> *"The context window is the only true constraint on an agent's memory."*
>
> **Harness layer**: This session covers the memory management subsystem that
> sits between the agent loop and the API. Every token you send costs money and
> occupies limited space. Context compaction keeps the conversation useful
> without blowing past the window.

---

## Problem

LLMs have a fixed context window. Claude Sonnet gives you 200K tokens, but a
busy coding session can burn through that in minutes -- tool results alone can
be thousands of tokens each. If you do nothing, the conversation hits the wall,
the API rejects the request, and the user's flow is broken.

You need a system that:

- Detects when context pressure is rising
- Reclaims space without losing critical information
- Degrades gracefully if summarization fails
- Works automatically so the user never has to think about it

## Solution

Claude Code uses a **four-layer compaction architecture**. Each layer is
progressively more aggressive, and they cascade automatically.

```
                    200K context window
  +--------------------------------------------------+
  |                                                    |
  |  Layer 1: Micro-compact                           |
  |  Replace old tool results with stubs              |
  |  Cost: zero LLM calls                             |
  |                                                    |
  |  Layer 2: Auto-compact (session memory)           |
  |  Use extracted session notes as the summary       |
  |  Cost: zero LLM calls                             |
  |                                                    |
  |  Layer 3: Auto-compact (LLM summarization)        |
  |  Ask the model to summarize the conversation      |
  |  Cost: one LLM call                               |
  |                                                    |
  |  Layer 4: Manual compact                          |
  |  User triggers /compact explicitly                |
  |                                                    |
  +--------+-----------------------------------------+
           |
           v
  Threshold = effective_window - 13,000 buffer
  Circuit breaker trips after 3 consecutive failures
```

The key arithmetic:

```
effective_window   = context_window - min(max_output_tokens, 20000)
auto_compact_threshold = effective_window - 13,000
```

For a 200K window with 16K output tokens, that gives a threshold of **171,000
tokens** -- cross it, and compaction kicks in automatically.

## How It Works

### Threshold Calculation

The engine calculates how much space you actually have, then subtracts a
safety buffer.

```python
# agents/s06_context_compaction.py -- mirroring autoCompact.ts:33-91

MAX_OUTPUT_TOKENS_FOR_SUMMARY = 20_000
AUTOCOMPACT_BUFFER_TOKENS = 13_000

def get_effective_context_window_size(
    context_window: int = 200_000,
    max_output_tokens: int = 16_000,
) -> int:
    """autoCompact.ts:33-49 -- usable window after reserving output tokens."""
    reserved = min(max_output_tokens, MAX_OUTPUT_TOKENS_FOR_SUMMARY)
    return context_window - reserved

def get_auto_compact_threshold(
    context_window: int = 200_000,
    max_output_tokens: int = 16_000,
) -> int:
    """autoCompact.ts:72-91 -- token count that triggers auto-compact."""
    effective = get_effective_context_window_size(context_window, max_output_tokens)
    return effective - AUTOCOMPACT_BUFFER_TOKENS
```

### Micro-Compact (Layer 1)

Before every API request, the engine replaces old tool results with short
stubs. Only the most recent 5 tool results are kept intact.

```python
# agents/s06_context_compaction.py -- mirroring microCompact.ts

COMPACTABLE_TOOLS = {
    "Read", "Bash", "Grep", "Glob",
    "WebSearch", "WebFetch", "Edit", "Write",
}

TIME_BASED_MC_CLEARED_MESSAGE = "[Old tool result content cleared]"

class MicroCompact:
    def __init__(self, keep_recent: int = 5):
        self.keep_recent = max(1, keep_recent)

    def compact_time_based(self, messages: list[Message]) -> tuple[list[Message], int]:
        """Replace old tool results with stubs. Zero LLM calls."""
        tool_ids: list[str] = []
        for msg in messages:
            for tr in msg.tool_results:
                if tr.tool_name in COMPACTABLE_TOOLS:
                    tool_ids.append(tr.tool_use_id)

        keep_set = set(tool_ids[-self.keep_recent:])
        clear_set = set(tid for tid in tool_ids if tid not in keep_set)
        # ... replace content with TIME_BASED_MC_CLEARED_MESSAGE
```

### Circuit Breaker

If LLM summarization fails 3 times in a row, the engine stops trying. This
prevents runaway API costs when the model is struggling.

```python
# agents/s06_context_compaction.py -- mirroring autoCompact.ts:260-265

MAX_CONSECUTIVE_AUTOCOMPACT_FAILURES = 3

def auto_compact_if_needed(self, messages, force_failure=False):
    # Circuit breaker check
    if self.tracking.consecutive_failures >= MAX_CONSECUTIVE_AUTOCOMPACT_FAILURES:
        print("  [circuit breaker] Skipping -- too many failures")
        return messages, False

    # ... try session memory compaction first, then LLM summarization
    # On success: reset consecutive_failures to 0
    # On failure: increment consecutive_failures
```

### Session Memory Compaction (Layer 2)

When session notes exist, the engine can skip the LLM call entirely. It
replaces older messages with the session memory summary and keeps only
recent messages verbatim.

```python
# agents/s06_context_compaction.py -- mirroring sessionMemoryCompact.ts

SM_COMPACT_MIN_TOKENS = 10_000
SM_COMPACT_MIN_TEXT_MESSAGES = 5
SM_COMPACT_MAX_TOKENS = 40_000

class SessionMemoryCompact:
    def try_compact(self, messages, auto_compact_threshold):
        if not self.session_memory:
            return None

        # Keep recent messages, replace the rest with session notes
        summary_msg = Message(
            role=MessageRole.SYSTEM,
            content=f"[Session Memory Summary]\n{self.session_memory}",
            is_compact_summary=True,
        )
        result = [summary_msg] + messages_to_keep

        # Safety check: bail if still over threshold
        if sum(m.token_estimate() for m in result) >= auto_compact_threshold:
            return None
        return result
```

### Progressive Warning States

As the context fills up, the user gets progressively more urgent feedback.

```python
# agents/s06_context_compaction.py -- mirroring autoCompact.ts:93-145

def calculate_token_warning_state(token_usage, context_window, max_output_tokens):
    auto_threshold = get_auto_compact_threshold(context_window, max_output_tokens)
    effective = get_effective_context_window_size(context_window, max_output_tokens)

    return TokenWarningState(
        percent_left=max(0, round(((auto_threshold - token_usage) / auto_threshold) * 100)),
        is_above_warning_threshold=token_usage >= auto_threshold - 20_000,
        is_above_error_threshold=token_usage >= auto_threshold - 20_000,
        is_above_auto_compact_threshold=token_usage >= auto_threshold,
        is_at_blocking_limit=token_usage >= effective - 3_000,
    )
```

## What Changed

| Component | Before | After |
|-----------|--------|-------|
| Context management | None -- conversations hit the wall | Four-layer automatic compaction |
| Old tool results | Persist forever, wasting tokens | Replaced with stubs after 5 more recent calls |
| Summarization failure | Infinite retry loop | Circuit breaker trips after 3 failures |
| Session notes | Not used for compaction | Can replace LLM summarization entirely |
| User feedback | Binary: works or crashes | Progressive warnings at 4 threshold levels |
| Token budget | Entire context window | `effective_window - 13,000` buffer for safety |

## Try It

```bash
# Run the compaction engine demo
python agents/s06_context_compaction.py
```

The demo simulates a multi-turn conversation and shows:

1. **Threshold arithmetic** -- how the real constants translate to trigger points
2. **Micro-compact** -- old tool results being replaced with stubs
3. **Auto-compact** -- LLM summarization kicking in when the threshold is crossed
4. **Session memory** -- bypassing the LLM when session notes are available
5. **Circuit breaker** -- the engine stopping after 3 consecutive failures

Experiment with different values:

- Change `CONTEXT_WINDOW` to see how smaller models behave
- Set `AUTOCOMPACT_BUFFER_TOKENS` to 0 and watch compaction trigger too late
- Force failures to watch the circuit breaker trip
