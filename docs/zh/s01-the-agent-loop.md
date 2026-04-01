# Session 01 -- Agent 循环

`[ s01 ] s02 > s03 > s04 > s05 | s06 > s07 > s08 > s09 > s10 | s11 > s12 > s13 > s14`

> "The loop is a streaming generator, not a simple while loop."
> "循环是一个流式生成器，而不是简单的 while 循环。"
>
> *实践层：`agents/s01_agent_loop.py` 用约 450 行 Python 重新实现了核心循环，你可以逐步调试、设置断点，实时观察每一次 yield 的发生。*

---

## 问题

大多数 Agent 教程把循环展示为一个简洁的 `while True`：调用模型、检查是否有工具调用、执行工具、追加结果。这对演示来说没问题，但 Claude Code 的实际循环需要处理：

- **流式输出**：每个 content block、工具结果和状态转换在发生的瞬间就被 yield 给 UI。
- **会话所有权**：一个 `QueryEngine` 在用户多轮对话中持久存在，跟踪消息历史和累计 token 用量。
- **类型化状态转换**：循环不是简单地 break 或 continue —— 它发出经过区分的 `Terminal` / `Continue` 状态，让调用方知道循环为什么移动了。
- **恢复路径**：命中 `max_output_tokens` 不会崩溃 —— 循环会最多重试 3 次后才放弃。

---

## 解决方案

Claude Code 将循环组织为一个**三层流水线**：

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

## 工作原理

### 1. 启动快速路径 (cli.tsx)

在加载任何重量级依赖之前，入口点会先检查是否可以立即退出：

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

### 2. QueryEngine —— 会话状态的拥有者

每个会话对应一个 `QueryEngine`。每次调用 `submit_message()` 都会在同一会话中开启新的一轮：

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

### 3. 查询循环 —— 一个异步生成器

这是整个系统的核心。注意它是一个使用 `yield` 的 `async def`，使其成为一个**生成器** —— 而不是返回最终结果的函数：

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

### 4. 类型化状态转换

循环使用可区分类型，使调用方始终清楚发生了什么：

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

## 变化对比

| 组件 | 之前（教程风格） | 之后（Claude Code） |
|---|---|---|
| 循环结构 | `while True` 返回最终字符串 | 异步生成器，yield 每个事件 |
| 状态管理 | 外层作用域中的可变列表 | 在 continue 点原子化重建 `LoopState` |
| 会话 | 单次调用函数 | `QueryEngine` 跨轮次持久化 |
| 终止方式 | `break` | 带原因的可区分 `Terminal` 类型 |
| Token 跟踪 | 无 | `QueryEngine.total_usage` 中累计统计 |
| 恢复机制 | max_output_tokens 时崩溃 | 最多重试 3 次后终止 |

---

## 试一试

```bash
cd agents
python s01_agent_loop.py
```

你将进入一个交互式 REPL。输入提示词，观察生成器依次 yield `turn_start`、`assistant`、`tool_result` 和 `terminal` 事件。试着让它执行一个多步骤任务，观察循环在不同轮次间的迭代。

单次执行模式：

```bash
python s01_agent_loop.py "list the files in the current directory"
```

**接下来可以探索的源文件：**
- `src/entrypoints/cli.tsx` -- 真实的启动快速路径
- `src/main.tsx` -- 重量级初始化
- `src/QueryEngine.ts` -- 会话状态拥有者 (~800 LOC)
- `src/query.ts` -- 完整的查询循环 (~1,700 LOC)
