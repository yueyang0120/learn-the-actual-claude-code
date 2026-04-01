# s06: Context Compaction

`s01 > s02 > s03 > s04 > s05 | [ s06 ] s07 > s08 > s09 > s10 | s11 > s12 > s13 > s14`

> "Every token you keep is a token you pay for. Compaction buys you more conversation."

## Problem

LLMs have a fixed context window. A busy coding session burns through 200K tokens fast -- tool results alone can be thousands of tokens each. Without management, the conversation hits the wall and the user's flow breaks.

## Solution

Claude Code uses a four-layer compaction cascade. Each layer is progressively more aggressive.

```
  Context Window (200K)
  +--------------------------------------------------+
  | Layer 1: Micro-compact                           |
  |   Replace old tool results with stubs (free)     |
  |                                                  |
  | Layer 2: Session memory compact                  |
  |   Use session notes as summary (free)            |
  |                                                  |
  | Layer 3: LLM summarization                       |
  |   Ask the model to summarize (1 API call)        |
  |                                                  |
  | Layer 4: Manual /compact                         |
  |   User triggers explicitly                       |
  +--------------------------------------------------+
       Threshold = effective_window - 13,000 buffer
       Circuit breaker after 3 consecutive failures
```

Cross the threshold and compaction kicks in automatically.

## How It Works

### Step 1: Threshold math

The engine calculates usable space, then subtracts a safety buffer. Source: `autoCompact.ts`.

```python
# agents/s06_context_compaction.py (simplified)

def get_effective_window(context_window=200_000, max_output=16_000):
    reserved = min(max_output, 20_000)
    return context_window - reserved

def get_auto_compact_threshold(context_window=200_000, max_output=16_000):
    effective = get_effective_window(context_window, max_output)
    return effective - 13_000  # buffer
```

For a 200K window with 16K output tokens, the threshold is **171,000 tokens**.

### Step 2: Micro-compact (Layer 1)

Before every API request, old tool results are replaced with stubs. Only the most recent 5 are kept intact. Zero LLM calls. Source: `microCompact.ts`.

```python
COMPACTABLE_TOOLS = {"Read", "Bash", "Grep", "Glob", "WebSearch", "WebFetch", "Edit", "Write"}
CLEARED_MSG = "[Old tool result content cleared]"

class MicroCompact:
    def __init__(self, keep_recent=5):
        self.keep_recent = keep_recent

    def compact(self, messages):
        tool_ids = [tr.id for msg in messages for tr in msg.tool_results
                    if tr.tool_name in COMPACTABLE_TOOLS]
        keep = set(tool_ids[-self.keep_recent:])
        # Replace everything else with CLEARED_MSG
```

### Step 3: Session memory compact (Layer 2)

When session notes exist, the engine replaces older messages with a summary. No LLM call needed. Source: `sessionMemoryCompact.ts`.

```python
class SessionMemoryCompact:
    def try_compact(self, messages, threshold):
        if not self.session_memory:
            return None  # fall through to Layer 3
        summary = Message(content=f"[Session Memory]\n{self.session_memory}")
        result = [summary] + recent_messages
        if token_count(result) >= threshold:
            return None  # still too big
        return result
```

### Step 4: Circuit breaker

If LLM summarization fails 3 times in a row, the engine stops trying. Prevents runaway API costs. Source: `autoCompact.ts:260-265`.

```python
MAX_FAILURES = 3

def auto_compact_if_needed(self, messages):
    if self.consecutive_failures >= MAX_FAILURES:
        return messages, False  # circuit breaker tripped
    # ... try session memory, then LLM summarization
    # on success: reset failures to 0
    # on failure: increment failures
```

### Step 5: Progressive warnings

As context fills up, the user gets progressively urgent feedback at four threshold levels: warning, error, auto-compact, and blocking limit. Source: `autoCompact.ts:93-145`.

## What Changed

| Component | Before (s05) | After (s06) |
|-----------|-------------|-------------|
| Context management | None -- hits the wall | Four-layer automatic compaction |
| Old tool results | Persist forever | Replaced with stubs after 5 newer calls |
| Summarization failure | Infinite retry | Circuit breaker after 3 failures |
| Session notes | Not used | Can replace LLM summarization entirely |
| User feedback | Binary: works or crashes | Progressive warnings at 4 levels |
| Token budget | Full window | `effective_window - 13,000` safety buffer |

## Try It

```bash
cd learn-the-actual-claude-code
python agents/s06_context_compaction.py
```

The demo simulates a multi-turn conversation and shows each compaction layer in action.

Try these prompts with Claude Code to see compaction happen live:

- Start a long session and watch the context percentage in the status bar
- Type `/compact` to trigger manual compaction
- Ask Claude to read many large files back-to-back, then check which tool results got stubbed
