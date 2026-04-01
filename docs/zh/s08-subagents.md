# Session 08 -- 子 Agent：Prompt Cache 共享

s01 > s02 > s03 > s04 > s05 | s06 > s07 > **s08** > s09 > s10 | s11 > s12 > s13 > s14

---

> *"Spawn cheap copies, not expensive clones."*
> *"生成廉价的副本，而不是昂贵的克隆。"*
>
> **Harness 层**: 本节涵盖子 agent 系统——Claude Code 如何生成共享父 agent prompt
> cache 前缀的子 agent，避免对 100K+ token 的重复处理。该设计将一个潜在昂贵的操作
> 转变为近乎免费的缓存命中。

---

## 问题

复杂任务自然会分解为子任务。"实现登录功能"可能会生成一个 Explore agent 来查找现有
的认证代码，一个 Plan agent 来设计方案，以及一个通用 agent 来执行工作。每个 agent
都需要相同的 system prompt、tool 定义和对话上下文。

如果没有缓存共享，每个子 agent 都要从头重新处理完整的 prompt。当前缀超过 100K+
token（system prompt + tools + CLAUDE.md + 对话历史）时，这是对时间和金钱的巨大
浪费——每次生成都可能耗费数秒和数十万 input token。

你需要一个系统来：

- 以隔离的状态生成 agent（父子 agent 之间互不干扰）
- 共享 prompt cache 前缀，使 API 返回缓存命中而非重新计算
- 记录每个 agent 的 transcript 以便调试和恢复
- 支持不同的 agent 类型（只读、完全访问、后台运行）

## 解决方案

Claude Code 使用 **CacheSafeParams**——一个携带构成 API 缓存键的五个组件的结构
化对象。当子 agent 发送相同的值时，Anthropic API 识别出前缀是字节级相同的，
并返回缓存读取。

```
  Parent Agent
  +------------------------------------------+
  | system_prompt  (cached)                   |
  | tools          (cached)                   |
  | model          (cached)                   |
  | message prefix (cached)                   |
  | thinking config(cached)                   |
  +----+---------+---------+---------+--------+
       |         |         |         |
       v         v         v         v
  +--------+ +--------+ +--------+ +--------+
  | Explore| | Plan   | | General| | Verify |
  | (haiku)| |(inherit)| | Purpose| | (bg)  |
  | RO     | | RO     | | R/W    | | RO    |
  +--------+ +--------+ +--------+ +--------+
       |         |         |         |
       v         v         v         v
  Sidechain transcript recording (per agent)
```

每个子 agent 获得一个隔离的 `SubagentContext`——克隆的文件缓存、独立的中止信号、
递增的查询深度——但 API 请求前缀是共享的。

## 工作原理

### CacheSafeParams

父子 agent 之间必须完全相同才能实现 prompt cache 共享的五个组件。

```python
# agents/s08_subagents.py -- mirrors CacheSafeParams in forkedAgent.ts

@dataclass
class CacheSafeParams:
    """
    system prompt + tools + model + message prefix + thinking config
    => cache key

    CacheSafeParams carries the first four; thinking config is inherited
    via toolUseContext.options.thinkingConfig.
    """
    system_prompt: str
    user_context: dict[str, str]        # e.g. {"claudeMd": "...", "currentDate": "..."}
    system_context: dict[str, str]      # e.g. {"gitStatus": "...", "osInfo": "..."}
    tools: list[str]                    # tool names (order matters for cache key)
    fork_context_messages: list[dict]   # parent message prefix
```

### 子 Agent 上下文隔离

每个子 agent 获得自己的执行上下文。可变状态被克隆以防止跨 agent 干扰。文件缓存
被深拷贝，这样一个 agent 的读取不会污染另一个 agent 的缓存。

```python
# agents/s08_subagents.py -- mirrors createSubagentContext() in forkedAgent.ts

@dataclass
class SubagentContext:
    agent_id: str
    agent_type: str
    messages: list[dict] = field(default_factory=list)
    read_file_cache: dict[str, str] = field(default_factory=dict)
    abort_signal: bool = False
    query_depth: int = 0
    share_set_app_state: bool = False

def create_subagent_context(
    parent_ctx: SubagentContext,
    *,
    agent_id: Optional[str] = None,
    agent_type: str = "general-purpose",
    messages: Optional[list[dict]] = None,
    share_set_app_state: bool = False,
) -> SubagentContext:
    return SubagentContext(
        agent_id=agent_id or str(uuid.uuid4()),
        agent_type=agent_type,
        messages=messages if messages is not None else [],
        # Clone file cache to prevent cross-agent interference
        read_file_cache=dict(parent_ctx.read_file_cache),
        abort_signal=False,
        query_depth=parent_ctx.query_depth + 1,
        share_set_app_state=share_set_app_state,
    )
```

### 内置 Agent 类型

Claude Code 附带四种内置 agent 类型，每种都针对特定用途进行了调优。

```python
# agents/s08_subagents.py -- mirrors src/tools/AgentTool/built-in/*.ts

EXPLORE_AGENT = AgentDefinition(
    agent_type="Explore",
    when_to_use="Fast agent specialized for exploring codebases.",
    tools=None,  # All tools except disallowed
    disallowed_tools=["Agent", "FileEdit", "FileWrite", "NotebookEdit"],
    model="haiku",
    omit_claude_md=True,  # Skip CLAUDE.md for speed
)

PLAN_AGENT = AgentDefinition(
    agent_type="Plan",
    when_to_use="Software architect agent for designing implementation plans.",
    disallowed_tools=["Agent", "FileEdit", "FileWrite", "NotebookEdit"],
    model="inherit",  # Use parent's model
    omit_claude_md=True,
)

GENERAL_PURPOSE_AGENT = AgentDefinition(
    agent_type="general-purpose",
    when_to_use="General-purpose agent for complex questions and multi-step tasks.",
    tools=["*"],  # All tools
)

VERIFICATION_AGENT = AgentDefinition(
    agent_type="verification",
    when_to_use="Verify implementation correctness. Produces PASS/FAIL/PARTIAL.",
    disallowed_tools=["Agent", "FileEdit", "FileWrite", "NotebookEdit"],
    model="inherit",
    background=True,  # Always runs async
)
```

### Agent 运行器

运行器协调完整的子 agent 生命周期：解析模型、计算 system prompt、组装 tools、
创建上下文、暴露 CacheSafeParams、运行查询循环、记录 transcript 并清理资源。

```python
# agents/s08_subagents.py -- mirrors runAgent() in runAgent.ts

class AgentRunner:
    def run_agent(self, agent_def, prompt, *, is_async=False,
                  fork_context_messages=None, on_cache_safe_params=None):
        # 1. Model resolution
        resolved_model = agent_def.model or "claude-sonnet-4-20250514"

        # 2. Context message assembly
        context_msgs = list(fork_context_messages or [])
        initial_messages = context_msgs + [user_msg]

        # 3. System prompt (optionally skip CLAUDE.md)
        system_prompt = agent_def.system_prompt
        if not agent_def.omit_claude_md:
            system_prompt += "\n\n[CLAUDE.md rules appended here]"

        # 4. Tool resolution with wildcard expansion
        if agent_def.tools is None or agent_def.tools == ["*"]:
            resolved_tools = [t for t in self.all_tools
                              if t not in agent_def.disallowed_tools]

        # 5. Create isolated context
        agent_ctx = create_subagent_context(self.parent_context, ...)

        # 6. Expose CacheSafeParams for fork cache sharing
        if on_cache_safe_params is not None:
            params = CacheSafeParams(
                system_prompt=system_prompt,
                tools=resolved_tools,
                fork_context_messages=initial_messages,
                ...
            )
            on_cache_safe_params(params)

        # 7. Record initial transcript
        _recorder.record(agent_id, initial_messages)

        # 8. Run query loop
        output_messages = simulated_query(initial_messages, system_prompt, ...)

        # 9. Cleanup: clear caches, kill shells, remove hooks
        agent_ctx.read_file_cache.clear()
        return output_messages
```

### Sidechain Transcript 记录

每个子 agent 的消息通过 UUID 链进行增量记录。这使得崩溃后恢复和事后调试成为
可能。

```python
# agents/s08_subagents.py -- mirrors recordSidechainTranscript() in sessionStorage.ts

class SidechainRecorder:
    def __init__(self):
        self._transcripts: dict[str, list[dict]] = {}

    def record(self, agent_id: str, messages: list[dict],
               parent_uuid: Optional[str] = None) -> str:
        if agent_id not in self._transcripts:
            self._transcripts[agent_id] = []
        for msg in messages:
            entry = {
                "uuid": str(uuid.uuid4()),
                "parent_uuid": parent_uuid,
                **msg,
            }
            self._transcripts[agent_id].append(entry)
            parent_uuid = entry["uuid"]
        return parent_uuid or ""
```

## 变化对比

| 组件 | 之前 | 之后 |
|------|------|------|
| 子 agent prompt | 从头重新处理 100K+ token | CacheSafeParams 实现 API 缓存命中 |
| Agent 状态 | 共享可变状态（存在干扰风险） | 隔离的 SubagentContext，带克隆缓存 |
| Agent 类型 | 一刀切 | 4 种内置类型：Explore、Plan、General、Verify |
| Transcript | 会话结束后丢失 | Sidechain 记录，带 UUID 链用于恢复 |
| 只读 agent | 无强制约束 | Explore/Plan agent 在禁止列表中包含写工具 |
| 后台工作 | 阻塞主 agent | Verification agent 默认异步运行 |
| 自定义 agent | 仅硬编码 | 从 `.claude/agents/*.md` 加载，带 frontmatter |

## 试一试

```bash
# Run the subagent demo
python agents/s08_subagents.py
```

演示逐步展示：

1. **Explore agent** -- 使用 haiku 生成一个只读搜索 agent
2. **通用 agent** -- 使用 fork 上下文生成并捕获 CacheSafeParams
3. **Verification agent** -- 生成一个异步运行的后台 agent
4. **自定义 agent 加载** -- 从 markdown 文件读取 agent 定义
5. **Sidechain transcripts** -- 检查记录的消息链

试着修改演示：

- 添加一种具有特定工具限制的新内置 agent 类型
- 将 Explore agent 的模型从 haiku 改为 inherit
- 添加一个自定义 agent markdown 文件并观察它被自动发现
