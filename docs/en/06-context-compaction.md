# Chapter 6: Context Compaction

Long conversations accumulate context until they exceed the model's token window. The compaction system in `autoCompact.ts` (351 lines) and its supporting modules manages this pressure through four progressive layers of intervention, from lightweight tool-result pruning to full LLM-based summarization. This chapter follows from the permission system in Chapter 5: once tool calls have been authorized and executed, their results consume context that must eventually be reclaimed.

## The Problem

A model with a 200K token context window sounds generous until a session involves dozens of file reads, grep results, and bash outputs. Each tool result can be thousands of tokens. After 30-40 turns of active coding, the conversation approaches the window limit.

The naive solution -- truncating old messages -- destroys information the model needs to maintain coherent behavior. Simply summarizing the entire conversation with an LLM call is expensive and can itself fail. And because the Anthropic API uses prefix caching, any mutation to early messages invalidates the cached prefix, causing a full re-read on the next request.

The engineering challenge is to reclaim tokens progressively, preserving recent context and critical state, while respecting the caching layer and handling failure gracefully.

## How Claude Code Solves It

### Threshold Arithmetic

The system defines layered thresholds derived from two base constants:

```typescript
// src/services/compact/autoCompact.ts
const MAX_OUTPUT_TOKENS_FOR_SUMMARY = 20_000
export const AUTOCOMPACT_BUFFER_TOKENS = 13_000
export const WARNING_THRESHOLD_BUFFER_TOKENS = 20_000
export const MANUAL_COMPACT_BUFFER_TOKENS = 3_000
```

The effective context window is the model's raw window minus a reservation for the compaction summary output:

```typescript
export function getEffectiveContextWindowSize(model: string): number {
  const reservedTokensForSummary = Math.min(
    getMaxOutputTokensForModel(model),
    MAX_OUTPUT_TOKENS_FOR_SUMMARY,       // 20,000
  )
  let contextWindow = getContextWindowForModel(model, getSdkBetas())
  return contextWindow - reservedTokensForSummary
}
```

The auto-compact threshold is then the effective window minus a 13,000-token buffer:

```typescript
export function getAutoCompactThreshold(model: string): number {
  const effectiveContextWindow = getEffectiveContextWindowSize(model)
  return effectiveContextWindow - AUTOCOMPACT_BUFFER_TOKENS  // -13,000
}
```

For a 200K-window model with 16K max output, the numbers work out as follows: effective window = 200,000 - 16,000 = 184,000; auto-compact threshold = 184,000 - 13,000 = 171,000. Warnings fire 20K below the auto-compact threshold. A hard blocking limit sits just 3K below the effective window, where only manual `/compact` can help.

### Layer 1: Micro-Compact

Micro-compact is the lightweight, pre-request layer. It runs before every API call and does not invoke an LLM. It has two flavors depending on whether the API prefix cache is warm or cold.

**Time-based micro-compact (cold cache).** When the user returns after a long idle period and the cache is cold, the server will rewrite the entire prefix anyway. Micro-compact replaces old tool results with stubs:

```typescript
// src/services/compact/microCompact.ts
const compactableIds = collectCompactableToolIds(messages)
const keepRecent = Math.max(1, config.keepRecent)
const keepSet = new Set(compactableIds.slice(-keepRecent))
const clearSet = new Set(compactableIds.filter(id => !keepSet.has(id)))
// Replace content: '[Old tool result content cleared]'
```

Only tools in `COMPACTABLE_TOOLS` are eligible: Read, Bash, Grep, Glob, WebSearch, WebFetch, Edit, and Write. At least one recent result is always preserved. The mutation is safe because the cache is already cold.

**Cached micro-compact (warm cache).** When the cache is warm, mutating message content would invalidate the cached prefix. Instead, this path queues a `cache_edits` block that instructs the API to delete specific tool-use IDs from the cached context:

```typescript
const cacheEdits = mod.createCacheEditsBlock(state, toolsToDelete)
pendingCacheEdits = cacheEdits
// Messages returned UNCHANGED -- edits applied at API layer
return { messages, compactionInfo: { pendingCacheEdits: { ... } } }
```

The distinction is critical: this path does not modify the `messages` array. The API handles the actual removal, preserving the rest of the cached prefix for a cache hit.

### Layer 2: Session Memory Compact

If the session memory system has been extracting structured notes throughout the conversation, those notes can serve as a summary without any LLM call. This layer runs before LLM summarization and short-circuits it on success.

```typescript
// In autoCompactIfNeeded():
const sessionMemoryResult = await trySessionMemoryCompaction(
  messages,
  toolUseContext.agentId,
  recompactionInfo.autoCompactThreshold,
)
if (sessionMemoryResult) {
  return { wasCompacted: true, compactionResult: sessionMemoryResult }
}
// Only fall through to LLM compact if session memory didn't handle it
```

The algorithm finds the boundary between already-summarized and new messages, then keeps a window of recent messages (at least 10K tokens, at least 5 text-block messages, capped at 40K tokens). A subtle adjustment function ensures tool-use/tool-result pairs are never split across the boundary, which would produce orphaned references and API errors.

### Layer 3: LLM Summarization

When session memory compaction is unavailable or insufficient, the system asks the model to summarize the conversation. The summary output is capped at 20,000 tokens (based on a p99.99 measurement of 17,387 tokens in production). This is the most expensive layer and the one most likely to fail.

### Layer 4: Manual /compact

The user can always invoke `/compact` directly. This triggers the same LLM summarization but is available even when auto-compaction has been disabled or the circuit breaker has tripped.

### The Token Warning Cascade

The function `calculateTokenWarningState()` produces a five-field status object that drives progressive UI signals:

```typescript
export function calculateTokenWarningState(
  tokenUsage: number,
  model: string,
): {
  percentLeft: number
  isAboveWarningThreshold: boolean
  isAboveErrorThreshold: boolean
  isAboveAutoCompactThreshold: boolean
  isAtBlockingLimit: boolean
}
```

| Threshold | Distance from Effective Window | Effect |
|-----------|-------------------------------|--------|
| Warning | ~33K below | Yellow indicator in UI |
| Error | ~33K below (symmetric) | Red indicator in UI |
| Auto-compact | 13K below | Triggers automatic compaction |
| Blocking | 3K below | Blocks further input until manual `/compact` |

The `percentLeft` calculation uses the auto-compact threshold as its denominator, so the percentage reflects proximity to automatic compaction rather than the absolute window limit.

### Circuit Breaker

A production incident revealed that sessions with irrecoverably oversized context would hammer the API with doomed compaction attempts every turn. Analysis found 1,279 sessions with 50+ consecutive failures, wasting approximately 250,000 API calls per day.

The circuit breaker stops retrying after three consecutive failures:

```typescript
const MAX_CONSECUTIVE_AUTOCOMPACT_FAILURES = 3

if (
  tracking?.consecutiveFailures !== undefined &&
  tracking.consecutiveFailures >= MAX_CONSECUTIVE_AUTOCOMPACT_FAILURES
) {
  return { wasCompacted: false }
}
```

The failure counter resets to zero on any successful compaction. The value three was chosen to allow transient errors a couple of retries while cutting off the long tail of hopeless loops.

### Tracking State

The `AutoCompactTrackingState` type threads compaction history through the main query loop:

```typescript
export type AutoCompactTrackingState = {
  compacted: boolean           // Has this session ever compacted?
  turnCounter: number          // Turns since last compaction
  turnId: string               // Unique ID for the current turn
  consecutiveFailures?: number // Circuit breaker counter
}
```

The `compacted` flag combined with `turnCounter` feeds into telemetry to diagnose re-compaction loops, a real problem where compaction output itself exceeds the threshold and triggers immediate re-compaction on the next turn.

### Post-Compact Restoration

After compaction replaces the conversation with a summary, the model would lose critical context. The restoration phase re-injects:

1. **Recent files** -- up to 5 files, within a 50K token budget, max 5K per file
2. **Active plan** -- if a plan file exists
3. **Plan mode instructions** -- if the user is in plan mode
4. **Invoked skills** -- up to 25K budget, max 5K per skill, most-recent-first
5. **Deferred tool schemas** -- tools discovered before compaction
6. **Agent listings and MCP instructions** -- re-announced from current state
7. **Session start hooks** -- re-executed to restore CLAUDE.md and similar context

This is why the actual post-compact token count often differs significantly from the raw summary size. Restored context can add 20-50K tokens.

## Key Design Decisions

**Progressive layers rather than a single strategy.** Micro-compact is free and runs every turn. Session memory compact avoids an LLM call. LLM summarization is the fallback of last resort. This layering minimizes cost while maximizing responsiveness.

**Two micro-compact flavors for cache awareness.** The cold-cache path mutates messages directly (safe because there is nothing cached to invalidate). The warm-cache path uses `cache_edits` to preserve the prefix. This distinction is invisible to the rest of the system but critical for API cost.

**Circuit breaker at three failures.** The value is a pragmatic compromise: low enough to stop runaway loops quickly, high enough to tolerate one or two transient API errors. The counter resets on success, so intermittent failures do not accumulate across healthy compactions.

**Restoration after compaction.** Rather than hoping the summary captures everything, the system explicitly re-injects known-critical context. This is more expensive in tokens but far more reliable than depending on summarization quality.

## In Practice

During a typical coding session, micro-compact silently prunes old tool results as the conversation grows. The user sees no indication of this. As context approaches the warning threshold, a yellow indicator appears in the UI. If the session crosses the auto-compact threshold, compaction runs between turns -- first trying session memory, then falling back to LLM summarization. The user sees a brief "Compacting conversation..." message.

After compaction, the model receives a summary plus restored files, plans, and skills. The conversation continues seamlessly, though references to very early turns may be lost. If the user notices degraded context, they can run `/compact` manually to force a fresh summarization.

In pathological cases -- for example, a single tool result that exceeds the effective window -- the circuit breaker trips after three failed attempts. The UI shows an error-level warning and the user must resolve the situation manually, typically by starting a new conversation.

## Summary

- Four progressive compaction layers (micro-compact, session memory, LLM summarization, manual) minimize cost while keeping sessions within the token window.
- Micro-compact operates in two modes depending on cache temperature: direct mutation when cold, API-layer `cache_edits` when warm.
- Threshold arithmetic derives warning, auto-compact, and blocking limits from the model's context window and output token reservation.
- A circuit breaker (3 consecutive failures) prevents runaway compaction loops that were observed wasting 250K API calls per day in production.
- Post-compact restoration re-injects recent files, active plans, skills, and hooks so the model retains critical operational context.
