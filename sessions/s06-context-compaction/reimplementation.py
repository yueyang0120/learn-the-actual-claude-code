"""
Session 06 -- Context Compaction (Reimplementation)

A runnable Python model of Claude Code's four-layer context compaction system.
Demonstrates threshold calculation, micro-compact, auto-compact with LLM
summarization, session memory compaction, and the circuit breaker pattern.

Source mapping:
  - autoCompact.ts  -> CompactionEngine (threshold math, auto-compact, circuit breaker)
  - microCompact.ts -> MicroCompact (tool result replacement)
  - compact.ts      -> CompactionEngine._llm_summarize() (LLM-based summarization stub)
  - sessionMemoryCompact.ts -> SessionMemoryCompact (memory-based compaction)
"""

from __future__ import annotations

import hashlib
import textwrap
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


# ---------------------------------------------------------------------------
# Constants -- mirroring autoCompact.ts
# ---------------------------------------------------------------------------

# src/services/compact/autoCompact.ts:30
MAX_OUTPUT_TOKENS_FOR_SUMMARY = 20_000

# src/services/compact/autoCompact.ts:62-64
AUTOCOMPACT_BUFFER_TOKENS = 13_000
WARNING_THRESHOLD_BUFFER_TOKENS = 20_000
ERROR_THRESHOLD_BUFFER_TOKENS = 20_000
MANUAL_COMPACT_BUFFER_TOKENS = 3_000

# src/services/compact/autoCompact.ts:70
MAX_CONSECUTIVE_AUTOCOMPACT_FAILURES = 3

# src/services/compact/microCompact.ts:36
TIME_BASED_MC_CLEARED_MESSAGE = "[Old tool result content cleared]"

# src/services/compact/sessionMemoryCompact.ts:57-61
SM_COMPACT_MIN_TOKENS = 10_000
SM_COMPACT_MIN_TEXT_MESSAGES = 5
SM_COMPACT_MAX_TOKENS = 40_000

# Simulated model properties (Sonnet-like)
CONTEXT_WINDOW = 200_000
MAX_OUTPUT_TOKENS = 16_000

# Tools eligible for micro-compact (microCompact.ts:41-50)
COMPACTABLE_TOOLS = {
    "Read", "Bash", "Grep", "Glob",
    "WebSearch", "WebFetch", "Edit", "Write",
}


# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------

class MessageRole(Enum):
    USER = "user"
    ASSISTANT = "assistant"
    SYSTEM = "system"


@dataclass
class ToolResult:
    """A tool invocation result embedded in a user message."""
    tool_use_id: str
    tool_name: str
    content: str


@dataclass
class Message:
    role: MessageRole
    content: str
    tool_results: list[ToolResult] = field(default_factory=list)
    is_compact_summary: bool = False
    timestamp: float = field(default_factory=time.time)

    def token_estimate(self) -> int:
        """Rough token estimate: ~4 chars per token, padded by 4/3.
        Mirrors microCompact.ts estimateMessageTokens()."""
        chars = len(self.content)
        for tr in self.tool_results:
            chars += len(tr.content)
        return int((chars / 4) * (4 / 3))


# src/services/compact/autoCompact.ts:51-60
@dataclass
class AutoCompactTrackingState:
    compacted: bool = False
    turn_counter: int = 0
    turn_id: str = ""
    consecutive_failures: int = 0


@dataclass
class TokenWarningState:
    """Output of calculateTokenWarningState() -- autoCompact.ts:93-145"""
    percent_left: int
    is_above_warning_threshold: bool
    is_above_error_threshold: bool
    is_above_auto_compact_threshold: bool
    is_at_blocking_limit: bool


# ---------------------------------------------------------------------------
# Threshold arithmetic -- autoCompact.ts:33-91
# ---------------------------------------------------------------------------

def get_effective_context_window_size(
    context_window: int = CONTEXT_WINDOW,
    max_output_tokens: int = MAX_OUTPUT_TOKENS,
) -> int:
    """autoCompact.ts:33-49 -- usable window after reserving output tokens."""
    reserved = min(max_output_tokens, MAX_OUTPUT_TOKENS_FOR_SUMMARY)
    return context_window - reserved


def get_auto_compact_threshold(
    context_window: int = CONTEXT_WINDOW,
    max_output_tokens: int = MAX_OUTPUT_TOKENS,
) -> int:
    """autoCompact.ts:72-91 -- token count that triggers auto-compact."""
    effective = get_effective_context_window_size(context_window, max_output_tokens)
    return effective - AUTOCOMPACT_BUFFER_TOKENS


def calculate_token_warning_state(
    token_usage: int,
    context_window: int = CONTEXT_WINDOW,
    max_output_tokens: int = MAX_OUTPUT_TOKENS,
    auto_compact_enabled: bool = True,
) -> TokenWarningState:
    """autoCompact.ts:93-145 -- progressive warning levels."""
    auto_threshold = get_auto_compact_threshold(context_window, max_output_tokens)
    effective = get_effective_context_window_size(context_window, max_output_tokens)

    threshold = auto_threshold if auto_compact_enabled else effective
    percent_left = max(0, round(((threshold - token_usage) / threshold) * 100))

    warning_threshold = threshold - WARNING_THRESHOLD_BUFFER_TOKENS
    error_threshold = threshold - ERROR_THRESHOLD_BUFFER_TOKENS
    blocking_limit = effective - MANUAL_COMPACT_BUFFER_TOKENS

    return TokenWarningState(
        percent_left=percent_left,
        is_above_warning_threshold=token_usage >= warning_threshold,
        is_above_error_threshold=token_usage >= error_threshold,
        is_above_auto_compact_threshold=(
            auto_compact_enabled and token_usage >= auto_threshold
        ),
        is_at_blocking_limit=token_usage >= blocking_limit,
    )


# ---------------------------------------------------------------------------
# MicroCompact -- microCompact.ts
# ---------------------------------------------------------------------------

class MicroCompact:
    """Lightweight pre-request compaction that replaces old tool results.

    Two strategies mirror microCompact.ts:
    - time_based: content-clear when the cache is cold (long idle gap)
    - cached: queue edits for the API layer without changing messages
    """

    def __init__(self, keep_recent: int = 5):
        self.keep_recent = max(1, keep_recent)  # microCompact.ts:461

    def compact_time_based(self, messages: list[Message]) -> tuple[list[Message], int]:
        """maybeTimeBasedMicrocompact() -- mutate message content directly.

        Returns (new_messages, tokens_saved).
        """
        # Collect compactable tool result IDs in order
        tool_ids: list[str] = []
        for msg in messages:
            for tr in msg.tool_results:
                if tr.tool_name in COMPACTABLE_TOOLS:
                    tool_ids.append(tr.tool_use_id)

        keep_set = set(tool_ids[-self.keep_recent:])
        clear_set = set(tid for tid in tool_ids if tid not in keep_set)

        if not clear_set:
            return messages, 0

        tokens_saved = 0
        new_messages: list[Message] = []
        for msg in messages:
            new_results: list[ToolResult] = []
            for tr in msg.tool_results:
                if tr.tool_use_id in clear_set and tr.content != TIME_BASED_MC_CLEARED_MESSAGE:
                    tokens_saved += int(len(tr.content) / 4 * (4 / 3))
                    new_results.append(ToolResult(
                        tool_use_id=tr.tool_use_id,
                        tool_name=tr.tool_name,
                        content=TIME_BASED_MC_CLEARED_MESSAGE,
                    ))
                else:
                    new_results.append(tr)
            new_messages.append(Message(
                role=msg.role,
                content=msg.content,
                tool_results=new_results,
                is_compact_summary=msg.is_compact_summary,
                timestamp=msg.timestamp,
            ))
        return new_messages, tokens_saved

    def get_cached_edits(self, messages: list[Message]) -> list[str]:
        """cachedMicrocompactPath() -- return IDs to delete at API layer.

        Real code queues a cache_edits block; we return the list of tool IDs
        that would be deleted.  Messages remain unchanged.
        """
        tool_ids: list[str] = []
        for msg in messages:
            for tr in msg.tool_results:
                if tr.tool_name in COMPACTABLE_TOOLS:
                    tool_ids.append(tr.tool_use_id)

        keep_set = set(tool_ids[-self.keep_recent:])
        return [tid for tid in tool_ids if tid not in keep_set]


# ---------------------------------------------------------------------------
# Session Memory Compact -- sessionMemoryCompact.ts
# ---------------------------------------------------------------------------

class SessionMemoryCompact:
    """Uses extracted session notes instead of LLM summarization.

    sessionMemoryCompact.ts:514-630
    """

    def __init__(self, session_memory: Optional[str] = None):
        self.session_memory = session_memory
        self.last_summarized_index: Optional[int] = None

    def set_memory(self, content: str, last_summarized_index: int) -> None:
        self.session_memory = content
        self.last_summarized_index = last_summarized_index

    def try_compact(
        self,
        messages: list[Message],
        auto_compact_threshold: int,
    ) -> Optional[list[Message]]:
        """trySessionMemoryCompaction() -- returns compacted messages or None."""
        if not self.session_memory:
            return None

        # Determine messages to keep
        # sessionMemoryCompact.ts:571-581
        last_idx = (
            self.last_summarized_index
            if self.last_summarized_index is not None
            else len(messages) - 1
        )
        start_index = last_idx + 1 if last_idx >= 0 else len(messages)

        # Expand backwards to meet minimums (sessionMemoryCompact.ts:364-393)
        total_tokens = 0
        text_msg_count = 0
        for i in range(start_index, len(messages)):
            total_tokens += messages[i].token_estimate()
            if messages[i].content.strip():
                text_msg_count += 1

        i = start_index - 1
        while i >= 0:
            msg_tokens = messages[i].token_estimate()
            total_tokens += msg_tokens
            if messages[i].content.strip():
                text_msg_count += 1
            start_index = i
            if total_tokens >= SM_COMPACT_MAX_TOKENS:
                break
            if (
                total_tokens >= SM_COMPACT_MIN_TOKENS
                and text_msg_count >= SM_COMPACT_MIN_TEXT_MESSAGES
            ):
                break
            i -= 1

        messages_to_keep = messages[start_index:]

        # Build the compacted result
        summary_msg = Message(
            role=MessageRole.SYSTEM,
            content=f"[Session Memory Summary]\n{self.session_memory}",
            is_compact_summary=True,
        )
        result = [summary_msg] + messages_to_keep

        # Safety check: if still over threshold, bail out
        post_tokens = sum(m.token_estimate() for m in result)
        if post_tokens >= auto_compact_threshold:
            return None

        return result


# ---------------------------------------------------------------------------
# CompactionEngine -- orchestrates all strategies
# ---------------------------------------------------------------------------

class CompactionEngine:
    """Top-level compaction orchestrator mirroring autoCompactIfNeeded().

    Integrates:
      - Micro-compact (time-based and cached)
      - Session memory compaction
      - LLM-based auto-compact
      - Circuit breaker for repeated failures
    """

    def __init__(
        self,
        context_window: int = CONTEXT_WINDOW,
        max_output_tokens: int = MAX_OUTPUT_TOKENS,
    ):
        self.context_window = context_window
        self.max_output_tokens = max_output_tokens
        self.tracking = AutoCompactTrackingState()
        self.micro = MicroCompact(keep_recent=5)
        self.session_memory = SessionMemoryCompact()

        # Derived thresholds
        self.effective_window = get_effective_context_window_size(
            context_window, max_output_tokens
        )
        self.auto_compact_threshold = get_auto_compact_threshold(
            context_window, max_output_tokens
        )

    def token_count(self, messages: list[Message]) -> int:
        return sum(m.token_estimate() for m in messages)

    def warning_state(self, messages: list[Message]) -> TokenWarningState:
        usage = self.token_count(messages)
        return calculate_token_warning_state(
            usage, self.context_window, self.max_output_tokens
        )

    # -- Auto-compact entry point (autoCompact.ts:241-351) --

    def auto_compact_if_needed(
        self,
        messages: list[Message],
        force_failure: bool = False,
    ) -> tuple[list[Message], bool]:
        """Run auto-compact if the token count exceeds the threshold.

        Returns (possibly_compacted_messages, was_compacted).
        """
        # Circuit breaker (autoCompact.ts:260-265)
        if self.tracking.consecutive_failures >= MAX_CONSECUTIVE_AUTOCOMPACT_FAILURES:
            print(f"  [circuit breaker] Skipping -- {self.tracking.consecutive_failures} "
                  f"consecutive failures (limit: {MAX_CONSECUTIVE_AUTOCOMPACT_FAILURES})")
            return messages, False

        usage = self.token_count(messages)
        if usage < self.auto_compact_threshold:
            return messages, False

        print(f"  [auto-compact] Triggered: {usage:,} tokens >= "
              f"threshold {self.auto_compact_threshold:,}")

        # Strategy 1: Session memory compaction (autoCompact.ts:288-310)
        sm_result = self.session_memory.try_compact(
            messages, self.auto_compact_threshold
        )
        if sm_result is not None:
            print(f"  [session-memory-compact] Used session notes as summary, "
                  f"keeping {len(sm_result) - 1} recent messages")
            self.tracking.compacted = True
            self.tracking.turn_counter = 0
            self.tracking.consecutive_failures = 0
            return sm_result, True

        # Strategy 2: LLM-based summarization (autoCompact.ts:312-333)
        if force_failure:
            # Simulate failure for circuit breaker demo
            self.tracking.consecutive_failures += 1
            print(f"  [auto-compact] FAILED (consecutive: "
                  f"{self.tracking.consecutive_failures}/"
                  f"{MAX_CONSECUTIVE_AUTOCOMPACT_FAILURES})")
            if self.tracking.consecutive_failures >= MAX_CONSECUTIVE_AUTOCOMPACT_FAILURES:
                print("  [circuit breaker] TRIPPED -- skipping future attempts")
            return messages, False

        summary = self._llm_summarize(messages)
        self.tracking.compacted = True
        self.tracking.turn_counter = 0
        self.tracking.consecutive_failures = 0  # Reset on success
        return summary, True

    def _llm_summarize(self, messages: list[Message]) -> list[Message]:
        """Stub for compactConversation() in compact.ts.

        Real code sends the full conversation to an LLM with a summarization
        prompt, streams the response, and builds post-compact messages with
        file/plan/skill attachments.  We simulate with a deterministic hash.
        """
        content_hash = hashlib.md5(
            "".join(m.content for m in messages).encode()
        ).hexdigest()[:8]

        n_messages = len(messages)
        n_tool_results = sum(len(m.tool_results) for m in messages)
        pre_tokens = self.token_count(messages)

        summary_text = (
            f"[LLM Compact Summary #{content_hash}]\n"
            f"Summarized {n_messages} messages ({pre_tokens:,} tokens) "
            f"with {n_tool_results} tool results.\n"
            f"Key topics: {', '.join(set(m.content[:30] for m in messages[:3]))}"
        )

        return [Message(
            role=MessageRole.SYSTEM,
            content=summary_text,
            is_compact_summary=True,
        )]

    # -- Pre-request micro-compact --

    def micro_compact(self, messages: list[Message]) -> tuple[list[Message], int]:
        """Run time-based micro-compact before an API request."""
        return self.micro.compact_time_based(messages)


# ---------------------------------------------------------------------------
# Demo: simulated multi-turn conversation with compaction
# ---------------------------------------------------------------------------

def make_tool_result(idx: int, tool_name: str, size: int = 2000) -> ToolResult:
    """Create a simulated tool result of approximately `size` characters."""
    return ToolResult(
        tool_use_id=f"tool_{idx}",
        tool_name=tool_name,
        content=f"Result from {tool_name} #{idx}: " + "x" * size,
    )


def print_separator(label: str) -> None:
    print(f"\n{'='*60}")
    print(f"  {label}")
    print(f"{'='*60}\n")


def demo() -> None:
    # Use a small window so compaction triggers quickly in the demo
    small_window = 8_000
    small_max_output = 2_000

    engine = CompactionEngine(
        context_window=small_window,
        max_output_tokens=small_max_output,
    )

    print_separator("Threshold Arithmetic")
    effective = get_effective_context_window_size(small_window, small_max_output)
    threshold = get_auto_compact_threshold(small_window, small_max_output)
    print(f"Context window:       {small_window:>8,}")
    print(f"Max output tokens:    {small_max_output:>8,}")
    print(f"Reserved for summary: {min(small_max_output, MAX_OUTPUT_TOKENS_FOR_SUMMARY):>8,}")
    print(f"Effective window:     {effective:>8,}")
    print(f"Auto-compact buffer:  {AUTOCOMPACT_BUFFER_TOKENS:>8,}")
    print(f"Auto-compact thresh:  {threshold:>8,}")
    print(f"  (Note: threshold is negative because buffer > effective window")
    print(f"   in this tiny demo. Real models have 200K+ windows.)")

    # Override for demo purposes: set a workable threshold
    engine.auto_compact_threshold = int(effective * 0.75)
    engine.effective_window = effective
    print(f"\n  [demo override] Setting threshold to 75% of effective: "
          f"{engine.auto_compact_threshold:,}")

    print_separator("Phase 1: Building Conversation")
    messages: list[Message] = []

    # Simulate 6 turns with tool results
    tools = ["Read", "Bash", "Grep", "Glob", "Read", "Bash"]
    for i in range(6):
        # User message
        user_msg = Message(
            role=MessageRole.USER,
            content=f"Turn {i+1}: Please run {tools[i]} on the project " + "." * 200,
        )
        messages.append(user_msg)

        # Assistant message with tool result
        asst_msg = Message(
            role=MessageRole.ASSISTANT,
            content=f"I'll run {tools[i]} for you. Here are the results:",
            tool_results=[make_tool_result(i, tools[i], size=800)],
        )
        messages.append(asst_msg)

        usage = engine.token_count(messages)
        ws = engine.warning_state(messages)
        print(f"  Turn {i+1}: {usage:>6,} tokens | "
              f"{ws.percent_left}% left | "
              f"warn={ws.is_above_warning_threshold} "
              f"compact={ws.is_above_auto_compact_threshold}")

    print_separator("Phase 2: Micro-Compact (Time-Based)")
    pre_count = engine.token_count(messages)
    messages, saved = engine.micro_compact(messages)
    post_count = engine.token_count(messages)
    print(f"  Before: {pre_count:,} tokens")
    print(f"  Saved:  ~{saved:,} tokens")
    print(f"  After:  {post_count:,} tokens")
    print(f"  Kept last {engine.micro.keep_recent} tool results intact")

    # Show which tool results were cleared
    for msg in messages:
        for tr in msg.tool_results:
            status = "CLEARED" if tr.content == TIME_BASED_MC_CLEARED_MESSAGE else "kept"
            print(f"    {tr.tool_use_id} ({tr.tool_name}): {status}")

    print_separator("Phase 3: Auto-Compact (LLM Summarization)")
    # Add more messages to push over the threshold
    for i in range(6, 10):
        messages.append(Message(
            role=MessageRole.USER,
            content=f"Turn {i+1}: Another question " + "." * 300,
        ))
        messages.append(Message(
            role=MessageRole.ASSISTANT,
            content=f"Here's my answer for turn {i+1} " + "." * 300,
            tool_results=[make_tool_result(i, "Read", size=600)],
        ))

    usage = engine.token_count(messages)
    print(f"  Pre-compact: {usage:,} tokens ({len(messages)} messages)")

    messages, compacted = engine.auto_compact_if_needed(messages)
    if compacted:
        usage = engine.token_count(messages)
        print(f"  Post-compact: {usage:,} tokens ({len(messages)} messages)")
        print(f"  Summary: {messages[0].content[:120]}...")

    print_separator("Phase 4: Session Memory Compact")
    # Reset and demonstrate session memory compaction
    engine.tracking = AutoCompactTrackingState()
    messages = []
    for i in range(8):
        messages.append(Message(
            role=MessageRole.USER,
            content=f"Turn {i+1}: Work on feature #{i+1} " + "." * 200,
        ))
        messages.append(Message(
            role=MessageRole.ASSISTANT,
            content=f"Done with feature #{i+1}. Here is what I did: " + "." * 200,
            tool_results=[make_tool_result(i, "Edit", size=400)],
        ))

    # Set up session memory (simulating what SessionMemory extracts over time)
    engine.session_memory.set_memory(
        content=(
            "## Current Task\n"
            "Working on features 1-8 for the project.\n\n"
            "## Key Decisions\n"
            "- Feature 1: Implemented using pattern A\n"
            "- Feature 5: Refactored to use pattern B\n\n"
            "## Open Issues\n"
            "- Feature 8 needs testing\n"
        ),
        last_summarized_index=10,  # Messages 0-10 are summarized
    )

    usage = engine.token_count(messages)
    print(f"  Pre-compact: {usage:,} tokens ({len(messages)} messages)")
    print(f"  Session memory available: {len(engine.session_memory.session_memory or '')} chars")

    messages, compacted = engine.auto_compact_if_needed(messages)
    if compacted:
        usage = engine.token_count(messages)
        print(f"  Post-compact: {usage:,} tokens ({len(messages)} messages)")
        if messages[0].is_compact_summary:
            print(f"  Summary type: Session Memory (no LLM call needed)")

    print_separator("Phase 5: Circuit Breaker")
    engine2 = CompactionEngine(
        context_window=small_window,
        max_output_tokens=small_max_output,
    )
    engine2.auto_compact_threshold = 100  # Very low to always trigger

    dummy_messages = [
        Message(role=MessageRole.USER, content="Hello " * 50),
        Message(role=MessageRole.ASSISTANT, content="World " * 50),
    ]

    for attempt in range(5):
        print(f"  Attempt {attempt + 1}:")
        _, was_compacted = engine2.auto_compact_if_needed(
            dummy_messages, force_failure=True
        )
        if not was_compacted and engine2.tracking.consecutive_failures >= MAX_CONSECUTIVE_AUTOCOMPACT_FAILURES:
            print(f"  --> All further attempts will be skipped\n")

    # One more attempt to show circuit breaker in action
    print(f"  Attempt 6 (after breaker tripped):")
    engine2.auto_compact_if_needed(dummy_messages, force_failure=True)

    print_separator("Summary of Real Constants")
    print(f"  MAX_OUTPUT_TOKENS_FOR_SUMMARY   = {MAX_OUTPUT_TOKENS_FOR_SUMMARY:>8,}")
    print(f"  AUTOCOMPACT_BUFFER_TOKENS       = {AUTOCOMPACT_BUFFER_TOKENS:>8,}")
    print(f"  WARNING_THRESHOLD_BUFFER_TOKENS  = {WARNING_THRESHOLD_BUFFER_TOKENS:>8,}")
    print(f"  MANUAL_COMPACT_BUFFER_TOKENS     = {MANUAL_COMPACT_BUFFER_TOKENS:>8,}")
    print(f"  MAX_CONSECUTIVE_FAILURES         = {MAX_CONSECUTIVE_AUTOCOMPACT_FAILURES:>8}")
    print(f"  SM_COMPACT_MIN_TOKENS            = {SM_COMPACT_MIN_TOKENS:>8,}")
    print(f"  SM_COMPACT_MIN_TEXT_MESSAGES      = {SM_COMPACT_MIN_TEXT_MESSAGES:>8}")
    print(f"  SM_COMPACT_MAX_TOKENS            = {SM_COMPACT_MAX_TOKENS:>8,}")
    print()


if __name__ == "__main__":
    demo()
