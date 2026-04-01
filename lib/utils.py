"""
Shared utilities for the agent system.

Based on real Claude Code utilities from:
  - src/utils/tokens.ts (token estimation)
  - src/services/tokenEstimation.ts
  - src/utils/messages.ts (message helpers)
"""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv


def load_env() -> None:
    """Load .env file from project root."""
    env_path = Path(__file__).resolve().parents[1] / ".env"
    load_dotenv(env_path)


def get_api_key() -> str:
    load_env()
    key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not key:
        raise ValueError("ANTHROPIC_API_KEY not set. Copy .env.example to .env and add your key.")
    return key


def get_model() -> str:
    load_env()
    return os.environ.get("MODEL_ID", "claude-sonnet-4-6")


# ---------------------------------------------------------------------------
# Token estimation
# ---------------------------------------------------------------------------

def estimate_tokens(text: str) -> int:
    """
    Rough token estimate. Real Claude Code uses a more sophisticated
    estimator in src/services/tokenEstimation.ts that accounts for
    different content types and caching.

    Rule of thumb: ~4 chars per token for English text.
    """
    return max(1, len(text) // 4)


def estimate_message_tokens(messages: list[dict]) -> int:
    """Estimate total tokens across all messages."""
    total = 0
    for msg in messages:
        content = msg.get("content", "")
        if isinstance(content, str):
            total += estimate_tokens(content)
        elif isinstance(content, list):
            for block in content:
                if isinstance(block, dict):
                    # Text blocks
                    if "text" in block:
                        total += estimate_tokens(block["text"])
                    # Tool use blocks
                    elif "input" in block:
                        total += estimate_tokens(str(block["input"]))
                    # Tool result blocks
                    elif "content" in block:
                        total += estimate_tokens(str(block["content"]))
    return total


# ---------------------------------------------------------------------------
# Constants from the real source
# ---------------------------------------------------------------------------

# From src/services/compact/autoCompact.ts
AUTOCOMPACT_BUFFER_TOKENS = 13_000
WARNING_THRESHOLD_BUFFER_TOKENS = 20_000
MAX_CONSECUTIVE_AUTOCOMPACT_FAILURES = 3
MAX_OUTPUT_TOKENS_FOR_SUMMARY = 20_000
MANUAL_COMPACT_BUFFER_TOKENS = 3_000

# From src/services/tools/toolOrchestration.ts
DEFAULT_MAX_TOOL_USE_CONCURRENCY = 10

# From src/constants/prompts.ts (inferred from system prompt structure)
MAX_MEMORY_LINES = 200
MAX_MEMORY_BYTES = 25_000
