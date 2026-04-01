# Session 06 -- Context Compaction

## Overview

Context compaction is Claude Code's memory management system. Every LLM has a
finite context window (e.g. 200K tokens for Claude Sonnet). As a conversation
grows, Claude Code must decide what to keep, what to summarize, and what to
discard -- all without the user losing momentum. This session dissects the
four-layer compaction architecture that makes long sessions viable.

## Learning Objectives

1. **Threshold arithmetic** -- Understand how `getEffectiveContextWindowSize()`
   and `getAutoCompactThreshold()` convert a model's raw context window into
   actionable trigger points.
2. **Auto-compact lifecycle** -- Follow `shouldAutoCompact()` through
   `autoCompactIfNeeded()`: guards, session-memory-first strategy, LLM-based
   summarization, and post-compact restoration of files/plans/skills.
3. **Circuit breaker pattern** -- See why `MAX_CONSECUTIVE_AUTOCOMPACT_FAILURES=3`
   exists (a real incident wasted ~250K API calls/day) and how the failure
   counter threads through `AutoCompactTrackingState`.
4. **Micro-compact** -- Distinguish the two micro-compact strategies:
   *time-based* (content-clear old tool results when the cache is cold) vs.
   *cached* (use the `cache_edits` API to delete entries without invalidating
   the warm prefix).
5. **Session memory compaction** -- Learn how extracted session memories can
   replace LLM-based summarization, preserving recent messages verbatim while
   using structured notes as the summary.
6. **Token warning states** -- Trace `calculateTokenWarningState()` and see how
   warning, error, auto-compact, and blocking thresholds give the user
   progressive feedback as context pressure mounts.

## Source Files (Real Claude Code)

| File | Lines | Role |
|------|-------|------|
| `src/services/compact/autoCompact.ts` | ~351 | Threshold math, auto-compact orchestration, circuit breaker |
| `src/services/compact/compact.ts` | ~1706 | Full/partial compaction via LLM summarization, post-compact restoration |
| `src/services/compact/microCompact.ts` | ~531 | Lightweight in-place tool-result replacement (time-based + cached) |
| `src/services/compact/sessionMemoryCompact.ts` | ~631 | Memory-based compaction using extracted session notes |

## What the Reimplementation Approximates

The Python reimplementation (`reimplementation.py`) captures the core decision
engine:

- **Threshold calculation** matching the real constants (`AUTOCOMPACT_BUFFER_TOKENS`,
  `WARNING_THRESHOLD_BUFFER_TOKENS`, `MAX_OUTPUT_TOKENS_FOR_SUMMARY`).
- **Circuit breaker** that stops retrying after 3 consecutive failures.
- **Micro-compact** that replaces old tool results with short stubs, simulating
  both time-based content clearing and the concept of cached editing.
- **Auto-compact** that invokes an LLM summarization stub when the token count
  exceeds the threshold.
- **Session memory compaction** stub that demonstrates the "keep recent + use
  notes as summary" pattern.
- A **runnable demo** that simulates a multi-turn conversation, showing each
  compaction strategy triggering at the right moment.

### What It Does NOT Cover

- Actual API streaming, forked-agent prompt-cache sharing, and PTL retry loops.
- Post-compact file/plan/skill attachment restoration (real code re-injects the
  5 most recently read files, active plans, invoked skills, deferred tool
  schemas, and MCP instructions).
- Partial compaction (the "compact from here" / "compact up to here" UX).
- GrowthBook feature flags and remote config fetching.
- Transcript writing, session metadata re-appending, and hook execution.

## How to Use This Session

1. Read `SOURCE_ANALYSIS.md` for a guided walkthrough of the real code with
   annotated snippets.
2. Run `python reimplementation.py` to see compaction in action.
3. Experiment: change `CONTEXT_WINDOW` or `AUTOCOMPACT_BUFFER_TOKENS` in the
   Python file and observe how the trigger points shift.
4. Try making the circuit breaker trip by forcing failures, then see how the
   engine stops retrying.
