"""
Session 01 Reimplementation: Bootstrap & Agent Loop
====================================================

A runnable Python reimplementation of Claude Code's agent loop architecture.

Key insight: the real agent loop (src/query.ts) is NOT a simple while-loop.
It is a generator-based streaming pipeline where each message, tool result,
and stream event is yielded to the caller as it happens.

This reimplementation captures that architecture:
  - Bootstrap fast-path pattern (from src/entrypoints/cli.tsx)
  - QueryEngine class as conversation owner (from src/QueryEngine.ts)
  - Generator-based query loop with typed transitions (from src/query.ts)
  - Streaming tool dispatch (simplified from StreamingToolExecutor)

Usage:
    python sessions/s01-bootstrap-and-agent-loop/reimplementation.py
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, AsyncGenerator, Literal

# -- path setup so we can import from lib/ --
PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT))

import anthropic
from lib.utils import get_api_key, get_model

# ---------------------------------------------------------------------------
# Constants (from src/query.ts and src/utils/context.ts)
# ---------------------------------------------------------------------------

MAX_OUTPUT_TOKENS_RECOVERY_LIMIT = 3          # src/query.ts ~L164
MAX_TURNS_DEFAULT = 30                        # default safety limit
TOOL_NAME = "bash"

# ---------------------------------------------------------------------------
# Tool definition -- single bash tool for demo
# (Real Claude Code registers tools via src/tools.ts with a full Tool interface)
# ---------------------------------------------------------------------------

TOOLS = [
    {
        "name": TOOL_NAME,
        "description": (
            "Execute a bash command and return its output. "
            "The command runs in a subprocess with a timeout."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "command": {
                    "type": "string",
                    "description": "The bash command to execute",
                }
            },
            "required": ["command"],
        },
    }
]


def execute_bash_tool(command: str) -> str:
    """Execute a bash command and return output.
    Simplified version of BashTool from src/tools/BashTool/."""
    try:
        result = subprocess.run(
            command,
            shell=True,
            capture_output=True,
            text=True,
            timeout=30,
        )
        output = result.stdout
        if result.stderr:
            output += f"\n[stderr]: {result.stderr}"
        if result.returncode != 0:
            output += f"\n[exit code]: {result.returncode}"
        return output.strip() or "(no output)"
    except subprocess.TimeoutExpired:
        return "[error]: command timed out after 30 seconds"
    except Exception as e:
        return f"[error]: {e}"


# ---------------------------------------------------------------------------
# Typed transitions (from src/query/transitions.ts)
# In the real code, Terminal and Continue are discriminated unions.
# ---------------------------------------------------------------------------

TransitionReason = Literal[
    "next_turn",
    "max_output_tokens_recovery",
    "completed",
    "max_turns",
    "aborted",
    "model_error",
]


@dataclass
class Terminal:
    """The loop exited. Mirrors src/query/transitions.ts Terminal type."""
    reason: str
    error: Exception | None = None


@dataclass
class LoopState:
    """Mutable cross-iteration state. Mirrors the State type in src/query.ts ~L204-217.
    Each continue-site rebuilds this object atomically."""
    messages: list[dict[str, Any]]
    turn_count: int = 1
    max_output_tokens_recovery_count: int = 0
    transition_reason: TransitionReason | None = None


# ---------------------------------------------------------------------------
# The query generator -- the actual agent loop
# (Mirrors src/query.ts queryLoop(), simplified)
# ---------------------------------------------------------------------------

async def query_loop(
    *,
    client: anthropic.AsyncAnthropic,
    model: str,
    system_prompt: str,
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]],
    max_turns: int = MAX_TURNS_DEFAULT,
) -> AsyncGenerator[dict[str, Any], None]:
    """
    The agent loop as an async generator.

    Real code: src/query.ts ~L241-1728 (queryLoop)

    KEY DIFFERENCE from tutorials: this is a GENERATOR that yields each message
    as it happens, not a function that returns the final result. The caller
    consumes the stream with `async for message in query_loop(...)`.
    """
    state = LoopState(messages=list(messages))

    # src/query.ts ~L307: while (true) {
    while True:
        turn_messages = list(state.messages)

        # -- Yield a turn-start marker (like stream_request_start in real code) --
        yield {"type": "turn_start", "turn": state.turn_count}

        # -- API call (src/query.ts ~L659-708: for await ... deps.callModel) --
        try:
            response = await client.messages.create(
                model=model,
                max_tokens=8192,
                system=system_prompt,
                messages=turn_messages,
                tools=tools,
            )
        except Exception as e:
            yield {"type": "error", "error": str(e)}
            return  # Terminal: model_error

        # -- Yield the assistant message --
        # Real code yields each content block as it streams (L748-825).
        # We simplify to yielding the complete response.
        assistant_content = response.content
        yield {
            "type": "assistant",
            "content": assistant_content,
            "stop_reason": response.stop_reason,
            "usage": {
                "input_tokens": response.usage.input_tokens,
                "output_tokens": response.usage.output_tokens,
            },
        }

        # -- Extract tool_use blocks (src/query.ts ~L829-835) --
        tool_use_blocks = [
            block for block in assistant_content
            if block.type == "tool_use"
        ]

        # -- If no tool_use, check for completion (src/query.ts ~L1062) --
        if not tool_use_blocks:
            # src/query.ts ~L1357: return { reason: 'completed' }
            yield {"type": "terminal", "reason": "completed"}
            return

        # -- Dispatch tools (src/query.ts ~L1380-1408) --
        # Real code uses StreamingToolExecutor for parallel execution during streaming.
        # We simplify to sequential execution after the full response.
        tool_results: list[dict[str, Any]] = []
        for tool_block in tool_use_blocks:
            tool_name = tool_block.name
            tool_input = tool_block.input
            tool_use_id = tool_block.id

            # Execute the tool
            if tool_name == TOOL_NAME:
                result_text = execute_bash_tool(tool_input.get("command", ""))
            else:
                result_text = f"[error]: unknown tool '{tool_name}'"

            # Yield the tool result as it happens
            yield {
                "type": "tool_result",
                "tool_use_id": tool_use_id,
                "tool_name": tool_name,
                "result": result_text,
            }

            tool_results.append({
                "type": "tool_result",
                "tool_use_id": tool_use_id,
                "content": result_text,
            })

        # -- Build next iteration's messages (src/query.ts ~L1715-1727) --
        next_turn = state.turn_count + 1

        # Check max turns (src/query.ts ~L1704-1712)
        if next_turn > max_turns:
            yield {"type": "terminal", "reason": "max_turns", "turn_count": next_turn}
            return

        # src/query.ts ~L1715-1727: Atomic state rebuild at continue site
        state = LoopState(
            messages=[
                *turn_messages,
                {"role": "assistant", "content": _serialize_content(assistant_content)},
                {"role": "user", "content": tool_results},
            ],
            turn_count=next_turn,
            max_output_tokens_recovery_count=0,
            transition_reason="next_turn",
        )
        # falls to top of while(True) -- same as real code


def _serialize_content(content_blocks) -> list[dict[str, Any]]:
    """Convert SDK content blocks to API-compatible dicts."""
    result = []
    for block in content_blocks:
        if block.type == "text":
            result.append({"type": "text", "text": block.text})
        elif block.type == "tool_use":
            result.append({
                "type": "tool_use",
                "id": block.id,
                "name": block.name,
                "input": block.input,
            })
    return result


# ---------------------------------------------------------------------------
# QueryEngine -- conversation state owner
# (Mirrors src/QueryEngine.ts ~L184-207)
# ---------------------------------------------------------------------------

class QueryEngine:
    """
    Owns the query lifecycle and session state for a conversation.

    Real code: src/QueryEngine.ts
    One QueryEngine per conversation. Each submit_message() call starts a new
    turn within the same conversation.
    """

    def __init__(self, *, client: anthropic.AsyncAnthropic, model: str, system_prompt: str):
        self.client = client
        self.model = model
        self.system_prompt = system_prompt
        self.messages: list[dict[str, Any]] = []
        self.total_usage = {"input_tokens": 0, "output_tokens": 0}

    async def submit_message(self, prompt: str) -> AsyncGenerator[dict[str, Any], None]:
        """
        Generator that yields messages for one turn of conversation.

        Real code: src/QueryEngine.ts ~L209 (async *submitMessage)
        """
        # Add the user message
        self.messages.append({"role": "user", "content": prompt})

        # Delegate to the inner query loop (src/QueryEngine.ts ~L675)
        async for message in query_loop(
            client=self.client,
            model=self.model,
            system_prompt=self.system_prompt,
            messages=self.messages,
            tools=TOOLS,
        ):
            # Track usage (src/QueryEngine.ts ~L789-799)
            if message.get("type") == "assistant" and "usage" in message:
                usage = message["usage"]
                self.total_usage["input_tokens"] += usage.get("input_tokens", 0)
                self.total_usage["output_tokens"] += usage.get("output_tokens", 0)

            # Update conversation history for completed assistant messages
            if message.get("type") == "assistant":
                self.messages.append({
                    "role": "assistant",
                    "content": _serialize_content(message["content"]),
                })
            elif message.get("type") == "tool_result":
                # Tool results are appended as user messages
                if (self.messages
                        and self.messages[-1].get("role") == "user"
                        and isinstance(self.messages[-1].get("content"), list)
                        and self.messages[-1]["content"]
                        and isinstance(self.messages[-1]["content"][0], dict)
                        and self.messages[-1]["content"][0].get("type") == "tool_result"):
                    self.messages[-1]["content"].append({
                        "type": "tool_result",
                        "tool_use_id": message["tool_use_id"],
                        "content": message["result"],
                    })
                else:
                    self.messages.append({
                        "role": "user",
                        "content": [{
                            "type": "tool_result",
                            "tool_use_id": message["tool_use_id"],
                            "content": message["result"],
                        }],
                    })

            yield message


# ---------------------------------------------------------------------------
# Bootstrap fast-path (mirrors src/entrypoints/cli.tsx ~L33-42)
# ---------------------------------------------------------------------------

def bootstrap_fast_path(args: list[str]) -> bool:
    """Check for fast-path exits before loading heavy dependencies.
    Real code: src/entrypoints/cli.tsx ~L33-42"""
    if len(args) == 1 and args[0] in ("--version", "-v"):
        print("s01-reimplementation 0.1.0 (learn-the-actual-claude-code)")
        return True
    if len(args) == 1 and args[0] == "--help":
        print("Usage: python reimplementation.py [--version] [prompt]")
        print("  Interactive agent loop reimplementation based on real Claude Code source.")
        return True
    return False


# ---------------------------------------------------------------------------
# Interactive main
# ---------------------------------------------------------------------------

async def main():
    """
    Interactive loop. Mirrors the REPL -> QueryEngine -> query() pipeline.

    Bootstrap sequence:
      1. Fast-path check (cli.tsx)
      2. Initialize client (main.tsx heavy init, simplified)
      3. Create QueryEngine (one per conversation)
      4. REPL loop: read prompt -> engine.submit_message() -> print yielded messages
    """
    import asyncio

    # Step 1: Fast-path (src/entrypoints/cli.tsx ~L33-42)
    args = sys.argv[1:]
    if bootstrap_fast_path(args):
        return

    # Step 2: Heavy init (src/main.tsx -- simplified to just API client)
    print("[bootstrap] Initializing...")
    start = time.time()
    api_key = get_api_key()
    model = get_model()
    client = anthropic.AsyncAnthropic(api_key=api_key)
    print(f"[bootstrap] Ready in {time.time() - start:.0f}ms. Model: {model}")
    print(f"[bootstrap] Tool: {TOOL_NAME}")
    print()

    # Step 3: Create QueryEngine (src/QueryEngine.ts constructor)
    system_prompt = (
        "You are an interactive coding assistant. You have access to a bash tool "
        "to run commands on the user's machine. Be concise and helpful. "
        "When you need information, use the bash tool rather than guessing."
    )
    engine = QueryEngine(client=client, model=model, system_prompt=system_prompt)

    # Handle single-shot mode from CLI args
    if args:
        prompt = " ".join(args)
        await _run_turn(engine, prompt)
        return

    # Step 4: REPL loop
    print("Type your prompt (or 'quit' to exit):")
    while True:
        try:
            prompt = input("\n> ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nGoodbye!")
            break

        if not prompt:
            continue
        if prompt.lower() in ("quit", "exit", "/quit"):
            print("Goodbye!")
            break

        await _run_turn(engine, prompt)

    # Print session summary
    print(f"\n[session] Total usage: {engine.total_usage}")


async def _run_turn(engine: QueryEngine, prompt: str):
    """Run one turn and print yielded messages."""
    async for message in engine.submit_message(prompt):
        msg_type = message.get("type")

        if msg_type == "turn_start":
            turn = message["turn"]
            if turn > 1:
                print(f"\n--- turn {turn} ---")

        elif msg_type == "assistant":
            # Print text blocks from the assistant
            for block in message["content"]:
                if block.type == "text":
                    print(f"\n{block.text}")
                elif block.type == "tool_use":
                    print(f"\n[tool_use: {block.name}] {json.dumps(block.input)}")

        elif msg_type == "tool_result":
            print(f"[tool_result: {message['tool_name']}] {message['result'][:500]}")

        elif msg_type == "terminal":
            reason = message["reason"]
            if reason != "completed":
                print(f"\n[terminal: {reason}]")

        elif msg_type == "error":
            print(f"\n[error] {message['error']}")


if __name__ == "__main__":
    import asyncio
    asyncio.run(main())
