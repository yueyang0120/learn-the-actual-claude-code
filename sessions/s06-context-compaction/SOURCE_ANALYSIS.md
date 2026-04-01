# Source Analysis -- Context Compaction

## 1. Key Constants

All constants live in `src/services/compact/autoCompact.ts` unless noted.

```typescript
// Reserve 20K tokens for the compaction summary output.
// Based on p99.99 of compact summary output being 17,387 tokens.
const MAX_OUTPUT_TOKENS_FOR_SUMMARY = 20_000

// Buffer between the effective window and the auto-compact trigger.
export const AUTOCOMPACT_BUFFER_TOKENS = 13_000

// Additional buffer below auto-compact threshold for user warnings.
export const WARNING_THRESHOLD_BUFFER_TOKENS = 20_000

// Same buffer for error-level warnings.
export const ERROR_THRESHOLD_BUFFER_TOKENS = 20_000

// Thin buffer for the hard blocking limit (manual compact only).
export const MANUAL_COMPACT_BUFFER_TOKENS = 3_000

// Circuit breaker: stop after 3 consecutive failures.
// BQ 2026-03-10: 1,279 sessions had 50+ consecutive failures
// wasting ~250K API calls/day globally.
const MAX_CONSECUTIVE_AUTOCOMPACT_FAILURES = 3
```

**Why these numbers matter:** A 200K context window model gets an effective
window of 180K (200K minus 20K reserved for output). The auto-compact threshold
is then 180K - 13K = 167K tokens. The warning threshold fires at 167K - 20K =
147K tokens. These layered thresholds give the system (and the user) progressive
signals as context pressure rises.

---

## 2. getEffectiveContextWindowSize()

**File:** `autoCompact.ts:33-49`

This function answers: "How many tokens can actually be used for conversation
context?"

```typescript
export function getEffectiveContextWindowSize(model: string): number {
  const reservedTokensForSummary = Math.min(
    getMaxOutputTokensForModel(model),
    MAX_OUTPUT_TOKENS_FOR_SUMMARY,       // 20,000
  )
  let contextWindow = getContextWindowForModel(model, getSdkBetas())

  // Allow env override for testing
  const autoCompactWindow = process.env.CLAUDE_CODE_AUTO_COMPACT_WINDOW
  if (autoCompactWindow) {
    const parsed = parseInt(autoCompactWindow, 10)
    if (!isNaN(parsed) && parsed > 0) {
      contextWindow = Math.min(contextWindow, parsed)
    }
  }

  return contextWindow - reservedTokensForSummary
}
```

**Key decisions:**
- Takes the *minimum* of the model's max output tokens and 20K. This means a
  model with 8K max output only reserves 8K, not the full 20K.
- Allows an environment variable to artificially shrink the window for testing.
- The result is used as the denominator in all threshold calculations downstream.

---

## 3. getAutoCompactThreshold()

**File:** `autoCompact.ts:72-91`

Once we have the effective window, the threshold is simply:

```typescript
export function getAutoCompactThreshold(model: string): number {
  const effectiveContextWindow = getEffectiveContextWindowSize(model)
  const autocompactThreshold =
    effectiveContextWindow - AUTOCOMPACT_BUFFER_TOKENS  // -13,000

  // Override for easier testing of autocompact
  const envPercent = process.env.CLAUDE_AUTOCOMPACT_PCT_OVERRIDE
  if (envPercent) {
    const parsed = parseFloat(envPercent)
    if (!isNaN(parsed) && parsed > 0 && parsed <= 100) {
      const percentageThreshold = Math.floor(
        effectiveContextWindow * (parsed / 100),
      )
      return Math.min(percentageThreshold, autocompactThreshold)
    }
  }

  return autocompactThreshold
}
```

**Worked example (Sonnet, 200K window, 16K max output):**
```
contextWindow            = 200,000
reservedTokensForSummary = min(16,000, 20,000) = 16,000
effectiveContextWindow   = 200,000 - 16,000 = 184,000
autocompactThreshold     = 184,000 - 13,000 = 171,000
```

The percentage override lets QA test compaction at, say, 50% of the window
(`CLAUDE_AUTOCOMPACT_PCT_OVERRIDE=50`) without waiting for a real long session.

---

## 4. AutoCompactTrackingState

**File:** `autoCompact.ts:51-60`

This type threads compaction history through the main query loop:

```typescript
export type AutoCompactTrackingState = {
  compacted: boolean          // Has this session ever compacted?
  turnCounter: number         // Turns since last compaction
  turnId: string              // Unique ID for the current turn
  consecutiveFailures?: number // Circuit breaker counter
}
```

**How it flows:**
1. The main query loop creates this state and passes it to `autoCompactIfNeeded()`.
2. On success, `consecutiveFailures` resets to 0.
3. On failure, `consecutiveFailures` increments.
4. The state's `compacted` flag plus `turnCounter` is passed into
   `RecompactionInfo` so telemetry can diagnose re-compaction loops (a real
   problem: compaction output can itself exceed the threshold, triggering an
   immediate re-compact on the next turn).

---

## 5. calculateTokenWarningState()

**File:** `autoCompact.ts:93-145`

This function produces a five-field status object that drives the UI:

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

**Threshold cascade (ascending severity):**

| Threshold | Tokens Left (approx) | Effect |
|-----------|---------------------|--------|
| Warning | 20K below auto-compact | Yellow indicator in UI |
| Error | Same as warning (symmetric) | Red indicator in UI |
| Auto-compact | 13K below effective window | Triggers LLM summarization |
| Blocking | 3K below effective window | Blocks further input until user runs `/compact` |

The `percentLeft` calculation uses the auto-compact threshold (not the full
window) as the denominator when auto-compact is enabled. This means the
percentage reflects "how close to triggering auto-compact" rather than "how close
to the absolute limit."

```typescript
const percentLeft = Math.max(
  0,
  Math.round(((threshold - tokenUsage) / threshold) * 100),
)
```

---

## 6. MicroCompact

**File:** `src/services/compact/microCompact.ts`

Micro-compact is the lightweight, pre-request compaction layer. It runs *before*
every API call and does not invoke an LLM. There are two flavors:

### 6a. Time-Based MicroCompact (Cold Cache)

When the user returns after a long idle period (cache is cold), the server will
rewrite the entire prefix anyway. So micro-compact clears old tool results
in-place:

```typescript
function maybeTimeBasedMicrocompact(
  messages: Message[],
  querySource: QuerySource | undefined,
): MicrocompactResult | null {
  const trigger = evaluateTimeBasedTrigger(messages, querySource)
  if (!trigger) return null

  const compactableIds = collectCompactableToolIds(messages)
  const keepRecent = Math.max(1, config.keepRecent)
  const keepSet = new Set(compactableIds.slice(-keepRecent))
  const clearSet = new Set(compactableIds.filter(id => !keepSet.has(id)))

  // Replace old tool_result content with a stub
  // ...
  return { ...block, content: TIME_BASED_MC_CLEARED_MESSAGE }
  // TIME_BASED_MC_CLEARED_MESSAGE = '[Old tool result content cleared]'
}
```

**Key details:**
- Only clears tools in `COMPACTABLE_TOOLS` (Read, Bash, Grep, Glob, WebSearch,
  WebFetch, Edit, Write).
- Always keeps at least 1 recent tool result (`Math.max(1, config.keepRecent)`).
- Mutates message content directly (the cache is cold, so no prefix to preserve).
- Resets cached-MC state to prevent stale references on the next turn.

### 6b. Cached MicroCompact (Warm Cache)

When the cache is warm, we cannot mutate message content without invalidating
the cached prefix. Instead, this path uses the API's `cache_edits` mechanism:

```typescript
async function cachedMicrocompactPath(
  messages: Message[],
  querySource: QuerySource | undefined,
): Promise<MicrocompactResult> {
  // Register new tool results in state
  // Determine which to delete based on trigger/keep thresholds
  const toolsToDelete = mod.getToolResultsToDelete(state)
  if (toolsToDelete.length > 0) {
    const cacheEdits = mod.createCacheEditsBlock(state, toolsToDelete)
    pendingCacheEdits = cacheEdits
    // Messages returned UNCHANGED -- edits applied at API layer
    return {
      messages,
      compactionInfo: { pendingCacheEdits: { ... } },
    }
  }
  return { messages }
}
```

**Critical distinction:** This path does NOT modify the `messages` array. It
queues a `cache_edits` block that tells the API "delete these tool_use_ids from
the cached context." The API handles the actual removal, preserving the rest of
the cached prefix for a cache hit.

**Consumed by the API layer:**
```typescript
export function consumePendingCacheEdits() {
  const edits = pendingCacheEdits
  pendingCacheEdits = null  // One-shot: caller must pin after insertion
  return edits
}
```

---

## 7. Session Memory Compaction

**File:** `src/services/compact/sessionMemoryCompact.ts`

This is an alternative to LLM-based summarization. Instead of asking an LLM to
summarize the conversation, session memory compaction uses the structured notes
that the Session Memory system has been extracting throughout the conversation.

### The Algorithm

```
1. Check: Is session memory enabled and non-empty?
2. Find lastSummarizedMessageId (the boundary between "already in session memory"
   and "new messages")
3. Calculate messages to keep:
   a. Start from lastSummarizedMessageId
   b. Expand backwards until: >= 10K tokens AND >= 5 text-block messages
   c. Stop if maxTokens (40K) reached
   d. Adjust to not split tool_use/tool_result pairs
4. Build result: [boundary marker] + [session memory as summary] + [kept messages]
5. Safety check: if post-compact size still exceeds threshold, return null
   (fall back to LLM compact)
```

### Configuration

```typescript
export const DEFAULT_SM_COMPACT_CONFIG: SessionMemoryCompactConfig = {
  minTokens: 10_000,           // Keep at least 10K tokens of recent messages
  minTextBlockMessages: 5,     // Keep at least 5 messages with text
  maxTokens: 40_000,           // Hard cap: don't keep more than 40K
}
```

### Tool Pair Preservation

A subtle but critical function ensures API invariants are maintained:

```typescript
export function adjustIndexToPreserveAPIInvariants(
  messages: Message[],
  startIndex: number,
): number {
  // If kept messages contain tool_results, include the matching
  // assistant messages with tool_use blocks
  // If assistant messages share message.id with earlier thinking
  // blocks, include those too
}
```

Without this, compaction could produce orphaned `tool_result` blocks (referencing
`tool_use` blocks that were summarized away), causing API errors.

### Priority Over LLM Compact

In `autoCompactIfNeeded()`, session memory compaction runs *first*:

```typescript
// EXPERIMENT: Try session memory compaction first
const sessionMemoryResult = await trySessionMemoryCompaction(
  messages,
  toolUseContext.agentId,
  recompactionInfo.autoCompactThreshold,
)
if (sessionMemoryResult) {
  // Skip LLM-based compaction entirely
  return { wasCompacted: true, compactionResult: sessionMemoryResult }
}

// Only fall through to LLM compact if session memory didn't handle it
const compactionResult = await compactConversation(...)
```

---

## 8. Circuit Breaker Pattern

**File:** `autoCompact.ts:258-349`

The circuit breaker exists because of a real production incident. Sessions where
context was irrecoverably over the limit (e.g., a single enormous tool result
that even compaction couldn't shrink below the threshold) would hammer the API
with doomed compaction attempts on every single turn.

```typescript
// Circuit breaker: stop retrying after N consecutive failures.
if (
  tracking?.consecutiveFailures !== undefined &&
  tracking.consecutiveFailures >= MAX_CONSECUTIVE_AUTOCOMPACT_FAILURES  // 3
) {
  return { wasCompacted: false }
}
```

**The failure counter lifecycle:**

```typescript
// In the try block (success):
return {
  wasCompacted: true,
  compactionResult,
  consecutiveFailures: 0,  // Reset on success
}

// In the catch block (failure):
const prevFailures = tracking?.consecutiveFailures ?? 0
const nextFailures = prevFailures + 1
if (nextFailures >= MAX_CONSECUTIVE_AUTOCOMPACT_FAILURES) {
  logForDebugging(
    `autocompact: circuit breaker tripped after ${nextFailures} ` +
    `consecutive failures -- skipping future attempts this session`,
    { level: 'warn' },
  )
}
return { wasCompacted: false, consecutiveFailures: nextFailures }
```

**Why 3?** Before this circuit breaker, BQ analysis found 1,279 sessions with
50+ consecutive failures (up to 3,272 in a single session), wasting approximately
250K API calls per day. The value 3 was chosen to give transient errors a couple
of retries while cutting off the long tail of hopeless loops.

---

## 9. The Full Auto-Compact Flow

Putting it all together, here is the decision tree that runs between every turn
in the main query loop:

```
autoCompactIfNeeded()
  |
  +-- Is compaction disabled? --> return (no-op)
  |
  +-- Circuit breaker tripped (>= 3 failures)? --> return (no-op)
  |
  +-- shouldAutoCompact()
  |     |
  |     +-- Is this a forked agent (session_memory, compact)? --> false
  |     +-- Is auto-compact disabled? --> false
  |     +-- Is reactive-compact mode enabled? --> false
  |     +-- Is context-collapse enabled? --> false
  |     +-- tokenCount = estimate(messages) - snipTokensFreed
  |     +-- threshold = getAutoCompactThreshold(model)
  |     +-- return tokenCount >= threshold
  |
  +-- shouldCompact is false? --> return (no-op)
  |
  +-- trySessionMemoryCompaction()  [runs first]
  |     |
  |     +-- Success? --> return result, reset failure counter
  |     +-- null? --> fall through to LLM compact
  |
  +-- compactConversation()
  |     |
  |     +-- Success? --> return result, reset failures to 0
  |     +-- Failure? --> increment failures, return wasCompacted: false
  |
  +-- (On next turn, the loop runs again with updated tracking state)
```

---

## 10. Post-Compact Restoration

**File:** `compact.ts:517-585`

After compaction replaces the conversation with a summary, Claude Code restores
critical context so the model does not lose its bearings:

1. **Recent files** (up to 5, within 50K token budget, max 5K per file)
2. **Active plan** (if a plan file exists)
3. **Plan mode instructions** (if the user is in plan mode)
4. **Invoked skills** (up to 25K budget, max 5K per skill, most-recent-first)
5. **Deferred tool schemas** (tools discovered pre-compact)
6. **Agent listings and MCP instructions** (re-announced from current state)
7. **Session start hooks** (re-executed to restore CLAUDE.md, etc.)

This restoration is why `truePostCompactTokenCount` often differs significantly
from the raw summary token count -- the restored context can add 20-50K tokens.
