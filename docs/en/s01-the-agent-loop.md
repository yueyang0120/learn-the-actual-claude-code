# Session 01 -- The Agent Loop

`[ s01 ] s02 > s03 > s04 > s05 | s06 > s07 > s08 > s09 > s10 | s11 > s12 > s13 > s14`

> "The loop is a streaming generator, not a simple while loop."
>
> *Harness layer: `agents/s01_agent_loop.py` reimplements the core loop in ~450 lines of Python so you can step through it, add breakpoints, and watch every yield happen in real time.*

---

## Problem

Most agent tutorials show the loop as a tidy `while True` that calls the model, checks for tool use, executes tools, and appends results. That works for demos, but Claude Code's actual loop has to handle:

- **Streaming**: every content block, tool result, and state transition is yielded to the UI the instant it happens.
- **Conversation ownership**: a `QueryEngine` persists across user turns, tracking message history and cumulative token usage.
- **Typed transitions**: the loop doesn't just break or continue -- it emits discriminated `Terminal` / `Continue` states so the caller knows *why* the loop moved.
- **Recovery paths**: hitting `max_output_tokens` doesn't crash -- the loop retries up to 3 times before giving up.

---

## Solution

Claude Code structures the loop as a **three-layer pipeline**:

```
 CLI (cli.tsx)              -- fast-path exits, then hand off
   |
   v
 QueryEngine (QueryEngine.ts) -- owns conversation state, calls queryLoop
   |
   v
 queryLoop (query.ts)       -- async generator that yields every event
   |
   v
 StreamingToolExecutor      -- dispatches tools during the stream
```

```
  +-----------------+
  |  cli.tsx        |  fast-path: --version, --help
  |  (entrypoint)   |  then heavy init -> main.tsx
  +--------+--------+
           |
           v
  +--------+--------+
  |  QueryEngine    |  one per conversation
  |  .submitMessage |  adds user msg, calls queryLoop
  |  .totalUsage    |  tracks input/output tokens
  +--------+--------+
           |
           v
  +--------+--------+
  |  queryLoop()    |  while (true) {
  |  generator      |    yield turn_start
  |                 |    call model
  |                 |    yield assistant blocks
  |                 |    if no tool_use -> yield terminal("completed")
  |                 |    dispatch tools -> yield tool_results
  |                 |    atomic state rebuild -> next iteration
  |                 |  }
  +-----------------+
```

---

## How It Works

### 1. Bootstrap fast-path (cli.tsx)

Before loading any heavy dependencies, the entrypoint checks for instant exits:

```python
def bootstrap_fast_path(args: list[str]) -> bool:
    """Check for fast-path exits before loading heavy dependencies.
    Real code: src/entrypoints/cli.tsx ~L33-42"""
    if len(args) == 1 and args[0] in ("--version", "-v"):
        print("s01-reimplementation 0.1.0 (learn-the-actual-claude-code)")
        return True
    if len(args) == 1 and args[0] == "--help":
        print("Usage: python reimplementation.py [--version] [prompt]")
        return True
    return False
```

### 2. QueryEngine -- conversation state owner

One `QueryEngine` per conversation. Each call to `submit_message()` starts a new turn within the same conversation:

```python
class QueryEngine:
    """
    Owns the query lifecycle and session state for a conversation.
    Real code: src/QueryEngine.ts
    """

    def __init__(self, *, client, model, system_prompt):
        self.client = client
        self.model = model
        self.system_prompt = system_prompt
        self.messages: list[dict] = []
        self.total_usage = {"input_tokens": 0, "output_tokens": 0}

    async def submit_message(self, prompt: str) -> AsyncGenerator[dict, None]:
        """
        Generator that yields messages for one turn of conversation.
        Real code: src/QueryEngine.ts ~L209 (async *submitMessage)
        """
        self.messages.append({"role": "user", "content": prompt})

        async for message in query_loop(
            client=self.client,
            model=self.model,
            system_prompt=self.system_prompt,
            messages=self.messages,
            tools=TOOLS,
        ):
            # Track usage
            if message.get("type") == "assistant" and "usage" in message:
                usage = message["usage"]
                self.total_usage["input_tokens"] += usage.get("input_tokens", 0)
                self.total_usage["output_tokens"] += usage.get("output_tokens", 0)

            yield message
```

### 3. The query loop -- an async generator

This is the heart of the system. Notice it is an `async def` that uses `yield`, making it a **generator** -- not a function that returns a final answer:

```python
async def query_loop(
    *,
    client, model, system_prompt, messages, tools,
    max_turns=30,
) -> AsyncGenerator[dict, None]:
    """
    The agent loop as an async generator.
    Real code: src/query.ts ~L241-1728 (queryLoop)
    """
    state = LoopState(messages=list(messages))

    while True:
        turn_messages = list(state.messages)

        yield {"type": "turn_start", "turn": state.turn_count}

        # API call
        response = await client.messages.create(
            model=model, max_tokens=8192,
            system=system_prompt,
            messages=turn_messages, tools=tools,
        )

        yield {
            "type": "assistant",
            "content": response.content,
            "stop_reason": response.stop_reason,
            "usage": { ... },
        }

        # Extract tool_use blocks
        tool_use_blocks = [
            block for block in response.content
            if block.type == "tool_use"
        ]

        # No tools -> done
        if not tool_use_blocks:
            yield {"type": "terminal", "reason": "completed"}
            return

        # Dispatch each tool, yielding results as they happen
        tool_results = []
        for tool_block in tool_use_blocks:
            result_text = execute_bash_tool(tool_block.input.get("command", ""))

            yield {
                "type": "tool_result",
                "tool_use_id": tool_block.id,
                "tool_name": tool_block.name,
                "result": result_text,
            }
            tool_results.append(...)

        # Atomic state rebuild at continue site
        state = LoopState(
            messages=[
                *turn_messages,
                {"role": "assistant", "content": ...},
                {"role": "user", "content": tool_results},
            ],
            turn_count=state.turn_count + 1,
        )
        # falls to top of while(True) -- same as real code
```

### 4. Typed transitions

The loop uses discriminated types so the caller always knows what happened:

```python
TransitionReason = Literal[
    "next_turn",                    # tool results appended, keep going
    "max_output_tokens_recovery",   # retrying after truncated output
    "completed",                    # model stopped without tool calls
    "max_turns",                    # safety limit reached
    "aborted",                      # user cancelled
    "model_error",                  # API error
]

@dataclass
class LoopState:
    """Mutable cross-iteration state. Mirrors the State type in src/query.ts."""
    messages: list[dict]
    turn_count: int = 1
    max_output_tokens_recovery_count: int = 0
    transition_reason: TransitionReason | None = None
```

---

## What Changed

| Component | Before (tutorial style) | After (Claude Code) |
|---|---|---|
| Loop structure | `while True` returning final string | Async generator yielding every event |
| State management | Mutable list in outer scope | Atomic `LoopState` rebuild at continue sites |
| Conversation | Single-shot function | `QueryEngine` persists across turns |
| Termination | `break` | Discriminated `Terminal` type with reason |
| Token tracking | None | Cumulative usage in `QueryEngine.total_usage` |
| Recovery | Crash on max_output_tokens | Retry up to 3 times before terminal |

---

## Try It

```bash
cd agents
python s01_agent_loop.py
```

You will get an interactive REPL. Type a prompt, watch the generator yield `turn_start`, `assistant`, `tool_result`, and `terminal` events in sequence. Try asking it to run a multi-step task to see the loop iterate across turns.

Single-shot mode:

```bash
python s01_agent_loop.py "list the files in the current directory"
```

**Source files to explore next:**
- `src/entrypoints/cli.tsx` -- the real bootstrap fast-path
- `src/main.tsx` -- heavy initialization
- `src/QueryEngine.ts` -- conversation state owner (~800 LOC)
- `src/query.ts` -- the full query loop (~1,700 LOC)
