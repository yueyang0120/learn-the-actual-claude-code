# Session 06 -- 上下文压缩

s01 > s02 > s03 > s04 > s05 | **s06** > s07 > s08 > s09 > s10 | s11 > s12 > s13 > s14

---

> *"The context window is the only true constraint on an agent's memory."*
> *"上下文窗口是对 agent 记忆力的唯一真正限制。"*
>
> **Harness 层**: 本节涵盖位于 agent 循环和 API 之间的内存管理子系统。你发送的每个
> token 都会产生费用并占据有限空间。上下文压缩在不超出窗口限制的前提下保持对话的
> 有效性。

---

## 问题

LLM 拥有固定的上下文窗口。Claude Sonnet 提供 200K token，但一个繁忙的编程会话
可能在几分钟内就消耗殆尽——单个 tool result 就可能包含数千 token。如果你什么都
不做，对话就会撞到墙壁，API 拒绝请求，用户的工作流被打断。

你需要一个系统来：

- 检测上下文压力何时在上升
- 在不丢失关键信息的前提下回收空间
- 在摘要失败时优雅降级
- 自动运行，让用户完全不需要关心这件事

## 解决方案

Claude Code 使用 **四层压缩架构**。每一层都比前一层更激进，并且它们会自动级联触发。

```
                    200K context window
  +--------------------------------------------------+
  |                                                    |
  |  Layer 1: Micro-compact                           |
  |  Replace old tool results with stubs              |
  |  Cost: zero LLM calls                             |
  |                                                    |
  |  Layer 2: Auto-compact (session memory)           |
  |  Use extracted session notes as the summary       |
  |  Cost: zero LLM calls                             |
  |                                                    |
  |  Layer 3: Auto-compact (LLM summarization)        |
  |  Ask the model to summarize the conversation      |
  |  Cost: one LLM call                               |
  |                                                    |
  |  Layer 4: Manual compact                          |
  |  User triggers /compact explicitly                |
  |                                                    |
  +--------+-----------------------------------------+
           |
           v
  Threshold = effective_window - 13,000 buffer
  Circuit breaker trips after 3 consecutive failures
```

关键的算术公式：

```
effective_window   = context_window - min(max_output_tokens, 20000)
auto_compact_threshold = effective_window - 13,000
```

对于 200K 窗口配合 16K 输出 token，阈值为 **171,000 token**——超过这个值，压缩
就会自动启动。

## 工作原理

### 阈值计算

引擎计算你实际可用的空间，然后减去一个安全缓冲区。

```python
# agents/s06_context_compaction.py -- mirroring autoCompact.ts:33-91

MAX_OUTPUT_TOKENS_FOR_SUMMARY = 20_000
AUTOCOMPACT_BUFFER_TOKENS = 13_000

def get_effective_context_window_size(
    context_window: int = 200_000,
    max_output_tokens: int = 16_000,
) -> int:
    """autoCompact.ts:33-49 -- usable window after reserving output tokens."""
    reserved = min(max_output_tokens, MAX_OUTPUT_TOKENS_FOR_SUMMARY)
    return context_window - reserved

def get_auto_compact_threshold(
    context_window: int = 200_000,
    max_output_tokens: int = 16_000,
) -> int:
    """autoCompact.ts:72-91 -- token count that triggers auto-compact."""
    effective = get_effective_context_window_size(context_window, max_output_tokens)
    return effective - AUTOCOMPACT_BUFFER_TOKENS
```

### Micro-Compact（第 1 层）

在每次 API 请求之前，引擎用简短的存根替换旧的 tool result。只有最近 5 个 tool result 保持完整。

```python
# agents/s06_context_compaction.py -- mirroring microCompact.ts

COMPACTABLE_TOOLS = {
    "Read", "Bash", "Grep", "Glob",
    "WebSearch", "WebFetch", "Edit", "Write",
}

TIME_BASED_MC_CLEARED_MESSAGE = "[Old tool result content cleared]"

class MicroCompact:
    def __init__(self, keep_recent: int = 5):
        self.keep_recent = max(1, keep_recent)

    def compact_time_based(self, messages: list[Message]) -> tuple[list[Message], int]:
        """Replace old tool results with stubs. Zero LLM calls."""
        tool_ids: list[str] = []
        for msg in messages:
            for tr in msg.tool_results:
                if tr.tool_name in COMPACTABLE_TOOLS:
                    tool_ids.append(tr.tool_use_id)

        keep_set = set(tool_ids[-self.keep_recent:])
        clear_set = set(tid for tid in tool_ids if tid not in keep_set)
        # ... replace content with TIME_BASED_MC_CLEARED_MESSAGE
```

### Circuit Breaker

如果 LLM 摘要连续失败 3 次，引擎就会停止重试。这可以防止在模型表现不佳时
造成失控的 API 费用。

```python
# agents/s06_context_compaction.py -- mirroring autoCompact.ts:260-265

MAX_CONSECUTIVE_AUTOCOMPACT_FAILURES = 3

def auto_compact_if_needed(self, messages, force_failure=False):
    # Circuit breaker check
    if self.tracking.consecutive_failures >= MAX_CONSECUTIVE_AUTOCOMPACT_FAILURES:
        print("  [circuit breaker] Skipping -- too many failures")
        return messages, False

    # ... try session memory compaction first, then LLM summarization
    # On success: reset consecutive_failures to 0
    # On failure: increment consecutive_failures
```

### Session Memory 压缩（第 2 层）

当 session notes 存在时，引擎可以完全跳过 LLM 调用。它用 session memory 摘要
替换较旧的消息，只保留最近的消息原文。

```python
# agents/s06_context_compaction.py -- mirroring sessionMemoryCompact.ts

SM_COMPACT_MIN_TOKENS = 10_000
SM_COMPACT_MIN_TEXT_MESSAGES = 5
SM_COMPACT_MAX_TOKENS = 40_000

class SessionMemoryCompact:
    def try_compact(self, messages, auto_compact_threshold):
        if not self.session_memory:
            return None

        # Keep recent messages, replace the rest with session notes
        summary_msg = Message(
            role=MessageRole.SYSTEM,
            content=f"[Session Memory Summary]\n{self.session_memory}",
            is_compact_summary=True,
        )
        result = [summary_msg] + messages_to_keep

        # Safety check: bail if still over threshold
        if sum(m.token_estimate() for m in result) >= auto_compact_threshold:
            return None
        return result
```

### 渐进式警告状态

随着上下文逐渐填满，用户会收到逐步升级的紧急反馈。

```python
# agents/s06_context_compaction.py -- mirroring autoCompact.ts:93-145

def calculate_token_warning_state(token_usage, context_window, max_output_tokens):
    auto_threshold = get_auto_compact_threshold(context_window, max_output_tokens)
    effective = get_effective_context_window_size(context_window, max_output_tokens)

    return TokenWarningState(
        percent_left=max(0, round(((auto_threshold - token_usage) / auto_threshold) * 100)),
        is_above_warning_threshold=token_usage >= auto_threshold - 20_000,
        is_above_error_threshold=token_usage >= auto_threshold - 20_000,
        is_above_auto_compact_threshold=token_usage >= auto_threshold,
        is_at_blocking_limit=token_usage >= effective - 3_000,
    )
```

## 变化对比

| 组件 | 之前 | 之后 |
|------|------|------|
| 上下文管理 | 无——对话直接撞墙 | 四层自动压缩 |
| 旧的 tool result | 永久保留，浪费 token | 在 5 次更新的调用后替换为存根 |
| 摘要失败 | 无限重试循环 | circuit breaker 在 3 次失败后熔断 |
| Session notes | 不用于压缩 | 可以完全替代 LLM 摘要 |
| 用户反馈 | 二元状态：正常或崩溃 | 4 级阈值的渐进式警告 |
| Token 预算 | 整个上下文窗口 | `effective_window - 13,000` 安全缓冲区 |

## 试一试

```bash
# Run the compaction engine demo
python agents/s06_context_compaction.py
```

演示模拟一个多轮对话，展示：

1. **阈值算术** -- 真实常量如何转化为触发点
2. **Micro-compact** -- 旧的 tool result 被替换为存根
3. **Auto-compact** -- 超过阈值时 LLM 摘要自动启动
4. **Session memory** -- 当 session notes 可用时绕过 LLM
5. **Circuit breaker** -- 引擎在连续 3 次失败后停止重试

可以尝试修改不同的值：

- 修改 `CONTEXT_WINDOW` 观察较小模型的行为
- 将 `AUTOCOMPACT_BUFFER_TOKENS` 设为 0，观察压缩触发过晚的情况
- 强制触发失败来观察 circuit breaker 熔断
