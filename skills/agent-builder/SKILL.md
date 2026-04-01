---
name: agent-builder
description: Scaffold a new Python agent with tool definitions and an agent loop
whenToUse: When the user wants to create a new agent or add tools to an existing one
allowed-tools:
  - bash
  - read_file
  - write_file
---

# Agent Builder

Help the user build a Python agent using the patterns from this course.

## Steps

1. Ask what the agent should do (or infer from context)
2. Determine which tools it needs:
   - `bash` — run shell commands
   - `read_file` / `write_file` — file I/O
   - Custom tools — define based on the task
3. Scaffold the agent:
   - Tool definitions with `name`, `description`, `input_schema`
   - Permission rules (allow/deny/ask per tool)
   - System prompt with environment context
   - Agent loop with tool execution
4. Test it with a sample query

## Template

```python
#!/usr/bin/env python3
"""
{agent_name} — {description}
"""

import asyncio
import json
import os
from anthropic import Anthropic

client = Anthropic()
MODEL = os.environ.get("MODEL_ID", "claude-sonnet-4-6")

TOOLS = [
    # Define tools here
]

SYSTEM = """You are {agent_description}."""


def agent_loop(query: str):
    messages = [{"role": "user", "content": query}]
    while True:
        response = client.messages.create(
            model=MODEL, system=SYSTEM, messages=messages,
            tools=TOOLS, max_tokens=8000,
        )
        messages.append({"role": "assistant", "content": response.content})
        if response.stop_reason != "tool_use":
            return messages
        # Execute tools and append results
        results = []
        for block in response.content:
            if block.type == "tool_use":
                output = execute_tool(block.name, block.input)
                results.append({
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": output,
                })
        messages.append({"role": "user", "content": results})


if __name__ == "__main__":
    query = input("> ")
    agent_loop(query)
```
