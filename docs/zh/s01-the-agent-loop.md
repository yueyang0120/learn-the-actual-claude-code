# s01: The Agent Loop

`[ s01 ] s02 > s03 > s04 > s05`

> *"A streaming generator, not a while loop"* -- 每个事件实时 yield 出去, 永远不返回一个最终字符串。

## 问题

语言模型能推理代码, 但碰不到真实世界 -- 不能读文件、跑测试、看报错。没有循环, 每次工具调用你都得手动把结果粘回去。你自己就是那个循环。大多数教程用一个简洁的 `while True` 来解决, 但 Claude Code 的真实循环是一个 streaming generator -- 每个事件发生的瞬间就 yield 给 UI。

## 解决方案

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

循环持续运行, 直到模型不再调用工具。一个退出条件控制整个流程。

## 工作原理

**1. 用户 prompt 作为第一条消息。**

```python
messages = [{"role": "user", "content": prompt}]
```

**2. 将消息和工具定义一起发给 LLM。**

```python
response = await client.messages.create(
    model=model, max_tokens=8192,
    system=system_prompt,
    messages=messages, tools=tools,
)
```

**3. yield assistant 响应, 检查 stop_reason。**

```python
yield {"type": "assistant", "content": response.content}

tool_use_blocks = [b for b in response.content if b.type == "tool_use"]
if not tool_use_blocks:
    yield {"type": "terminal", "reason": "completed"}
    return
```

**4. 执行每个工具调用, 收集结果, 回到第 2 步。**

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

**组装成一个函数：**

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

不到 30 行, 这就是整个 Agent。`QueryEngine` 类包装这个 generator, 在多轮对话之间持久化会话状态（真实代码：`src/QueryEngine.ts`）。后面的章节都在这个循环上叠加机制 -- 循环本身始终不变。

## 变更内容

| 组件 | 之前 | 之后 (s01) |
|---|---|---|
| -- | (无 -- 这是第一个章节) | -- |

## 试一试

```bash
cd learn-the-actual-claude-code
python agents/s01_agent_loop.py
```

可以试这些 prompt：

- `what files are in the current directory?`
- `count the lines of code in agents/s01_agent_loop.py`
- `create a file called /tmp/hello.txt with "hello world" and then cat it`
