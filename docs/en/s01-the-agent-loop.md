# s01: The Agent Loop

`[ s01 ] s02 > s03 > s04 > s05`

> *"A streaming generator, not a while loop"* -- yield every event as it happens, never return a final string.

## Problem

A language model can reason about code, but it cannot touch the real world -- it cannot read files, run tests, or check errors. Without a loop, every tool call requires you to copy-paste results back. You become the loop. Most tutorials solve this with a tidy `while True`, but Claude Code's real loop is a streaming generator that yields every event to the UI the instant it happens.

## Solution

```
  User prompt
      |
      v
  QueryEngine            owns conversation state, tracks tokens
      |
      v
  query_loop()           async generator -- yields every event
      |
      +---> call model
      |        |
      |        v
      |     stop_reason == "end_turn"?  --yes-->  yield terminal("completed"), return
      |        |
      |       no (tool_use)
      |        |
      |        v
      |     execute tools, yield each result
      |        |
      +--------+ rebuild state, loop back
```

The loop runs until the model stops calling tools. One exit condition controls the entire flow.

## How It Works

**1. User prompt becomes the first message.**

```python
messages = [{"role": "user", "content": prompt}]
```

**2. Send messages + tool definitions to the LLM.**

```python
response = await client.messages.create(
    model=model, max_tokens=8192,
    system=system_prompt,
    messages=messages, tools=tools,
)
```

**3. Yield the assistant response. Check stop_reason.**

```python
yield {"type": "assistant", "content": response.content}

tool_use_blocks = [b for b in response.content if b.type == "tool_use"]
if not tool_use_blocks:
    yield {"type": "terminal", "reason": "completed"}
    return
```

**4. Execute each tool call, collect results, loop back to step 2.**

```python
tool_results = []
for block in tool_use_blocks:
    result = execute_bash_tool(block.input["command"])
    yield {"type": "tool_result", "tool_use_id": block.id, "result": result}
    tool_results.append({"type": "tool_result", "tool_use_id": block.id, "content": result})

messages = [*messages,
    {"role": "assistant", "content": serialize(response.content)},
    {"role": "user", "content": tool_results},
]
# loop back to step 2
```

**Assembled into one function:**

```python
async def query_loop(*, client, model, system_prompt, messages, tools):
    """Async generator -- yields every event, never returns a final string.
    Real code: src/query.ts queryLoop() (~1,700 LOC)"""
    state_messages = list(messages)

    while True:
        yield {"type": "turn_start"}
        response = await client.messages.create(
            model=model, max_tokens=8192,
            system=system_prompt, messages=state_messages, tools=tools,
        )
        yield {"type": "assistant", "content": response.content}

        tool_blocks = [b for b in response.content if b.type == "tool_use"]
        if not tool_blocks:
            yield {"type": "terminal", "reason": "completed"}
            return

        tool_results = []
        for block in tool_blocks:
            result = execute_bash_tool(block.input["command"])
            yield {"type": "tool_result", "tool_use_id": block.id, "result": result}
            tool_results.append({"type": "tool_result", "tool_use_id": block.id, "content": result})

        state_messages = [*state_messages,
            {"role": "assistant", "content": serialize(response.content)},
            {"role": "user", "content": tool_results},
        ]
```

That is the entire agent in under 30 lines. A `QueryEngine` class wraps this generator to persist conversation state across turns (real code: `src/QueryEngine.ts`). Everything else in this course layers on top.

## What Changed

| Component | Before | After (s01) |
|---|---|---|
| -- | (none -- this is the first session) | -- |

## Try It

```bash
cd learn-the-actual-claude-code
python agents/s01_agent_loop.py
```

Example prompts to try:

- `what files are in the current directory?`
- `count the lines of code in agents/s01_agent_loop.py`
- `create a file called /tmp/hello.txt with "hello world" and then cat it`
