# s06: Context Compaction

`s01 > s02 > s03 > s04 > s05 | [ s06 ] s07 > s08 > s09 > s10 | s11 > s12 > s13 > s14`

> "留下的每个 token 都是要花钱的。压缩帮你把对话撑得更久。"

## 问题

LLM 的上下文窗口是固定的。一个繁忙的编程会话很快就能烧掉 200K token -- 光 tool result 就可能每个几千 token。如果不管它，对话撞墙，用户的节奏直接被打断。

## 解决方案

Claude Code 用四层压缩级联来应对。每一层比上一层更激进。

```
  Context Window (200K)
  +--------------------------------------------------+
  | Layer 1: Micro-compact                           |
  |   Replace old tool results with stubs (free)     |
  |                                                  |
  | Layer 2: Session memory compact                  |
  |   Use session notes as summary (free)            |
  |                                                  |
  | Layer 3: LLM summarization                       |
  |   Ask the model to summarize (1 API call)        |
  |                                                  |
  | Layer 4: Manual /compact                         |
  |   User triggers explicitly                       |
  +--------------------------------------------------+
       Threshold = effective_window - 13,000 buffer
       Circuit breaker after 3 consecutive failures
```

超过阈值，压缩自动启动。

## 工作原理

### 第 1 步：阈值计算

引擎先算出可用空间，再减去一个安全缓冲区。源码参考：`autoCompact.ts`。

```python
# agents/s06_context_compaction.py (simplified)

def get_effective_window(context_window=200_000, max_output=16_000):
    reserved = min(max_output, 20_000)
    return context_window - reserved

def get_auto_compact_threshold(context_window=200_000, max_output=16_000):
    effective = get_effective_window(context_window, max_output)
    return effective - 13_000  # buffer
```

对于 200K 窗口加 16K 输出 token，阈值是 **171,000 token**。

### 第 2 步：Micro-compact（第 1 层）

每次 API 请求前，旧的 tool result 会被替换成 stub。只保留最近 5 个完整。零 LLM 调用。源码参考：`microCompact.ts`。

```python
COMPACTABLE_TOOLS = {"Read", "Bash", "Grep", "Glob", "WebSearch", "WebFetch", "Edit", "Write"}
CLEARED_MSG = "[Old tool result content cleared]"

class MicroCompact:
    def __init__(self, keep_recent=5):
        self.keep_recent = keep_recent

    def compact(self, messages):
        tool_ids = [tr.id for msg in messages for tr in msg.tool_results
                    if tr.tool_name in COMPACTABLE_TOOLS]
        keep = set(tool_ids[-self.keep_recent:])
        # Replace everything else with CLEARED_MSG
```

### 第 3 步：Session memory compact（第 2 层）

有 session notes 的时候，引擎直接拿它替换旧消息，不需要 LLM 调用。源码参考：`sessionMemoryCompact.ts`。

```python
class SessionMemoryCompact:
    def try_compact(self, messages, threshold):
        if not self.session_memory:
            return None  # fall through to Layer 3
        summary = Message(content=f"[Session Memory]\n{self.session_memory}")
        result = [summary] + recent_messages
        if token_count(result) >= threshold:
            return None  # still too big
        return result
```

### 第 4 步：Circuit breaker

LLM 摘要连续失败 3 次，引擎就不再尝试了。防止 API 费用失控。源码参考：`autoCompact.ts:260-265`。

```python
MAX_FAILURES = 3

def auto_compact_if_needed(self, messages):
    if self.consecutive_failures >= MAX_FAILURES:
        return messages, False  # circuit breaker tripped
    # ... try session memory, then LLM summarization
    # on success: reset failures to 0
    # on failure: increment failures
```

### 第 5 步：渐进式警告

上下文越来越满的时候，用户会收到逐步升级的反馈 -- 四个阈值级别：warning、error、auto-compact、blocking limit。源码参考：`autoCompact.ts:93-145`。

## 变更内容

| 组件 | 之前 (s05) | 之后 (s06) |
|------|-----------|-----------|
| 上下文管理 | 没有 -- 直接撞墙 | 四层自动压缩 |
| 旧 tool result | 永远留着 | 5 次新调用后替换成 stub |
| 摘要失败 | 无限重试 | circuit breaker，3 次失败后熔断 |
| Session notes | 没有用上 | 可以完全替代 LLM 摘要 |
| 用户反馈 | 二元的：正常或崩溃 | 4 级阈值的渐进式警告 |
| Token 预算 | 整个窗口 | `effective_window - 13,000` 安全缓冲区 |

## 试一试

```bash
cd learn-the-actual-claude-code
python agents/s06_context_compaction.py
```

演示模拟一个多轮对话，展示每层压缩的实际效果。

用 Claude Code 试试这些操作，亲眼看看压缩过程：

- 开一个长会话，盯着状态栏里的上下文百分比
- 输入 `/compact` 手动触发压缩
- 让 Claude 连续读一堆大文件，然后看看哪些 tool result 被替换成了 stub
