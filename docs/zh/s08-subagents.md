# s08: Subagents

`s01 > s02 > s03 > s04 > s05 | s06 > s07 > [ s08 ] s09 > s10 | s11 > s12 > s13 > s14`

> "生成廉价的副本，不要生成昂贵的克隆。"

## 问题

复杂任务天然会拆成子任务。"实现登录功能"可能要一个 Explore agent 去找认证代码，一个 Plan agent 来设计方案，一个 worker agent 来干活。每个都需要同样的 system prompt 和上下文 -- 但从头重新处理 100K+ token 是巨大的浪费。

## 解决方案

Claude Code 用 CacheSafeParams 在父子 agent 之间共享 prompt cache 前缀。API 识别到字节级相同的前缀，直接返回缓存命中。

```
  Parent Agent (cached prefix)
  +-----------------------------------------+
  | system_prompt + tools + model + messages |
  +-----+--------+--------+--------+--------+
        |        |        |        |
        v        v        v        v
  +--------+ +--------+ +--------+ +--------+
  | Explore| | Plan   | | Worker | | Verify |
  | (haiku)| |(inherit)| | (R/W) | | (bg)   |
  | RO     | | RO     | |        | | RO     |
  +--------+ +--------+ +--------+ +--------+
  Each child: isolated context, shared cache prefix
```

## 工作原理

### 第 1 步：CacheSafeParams

五个组件构成 API cache key。父子 agent 发送相同的值，API 就返回缓存读取。源码参考：`forkedAgent.ts`。

```python
# agents/s08_subagents.py (simplified)

@dataclass
class CacheSafeParams:
    system_prompt: str
    user_context: dict       # claudeMd, currentDate, etc.
    system_context: dict     # gitStatus, osInfo, etc.
    tools: list[str]         # order matters for cache key
    fork_context_messages: list[dict]  # parent message prefix
```

### 第 2 步：上下文隔离

每个子 agent 拿到自己的执行上下文。文件缓存是克隆的，一个 agent 的读取不会污染另一个。源码参考：`forkedAgent.ts`。

```python
def create_subagent_context(parent_ctx, agent_type="general-purpose"):
    return SubagentContext(
        agent_id=uuid4(),
        agent_type=agent_type,
        read_file_cache=dict(parent_ctx.read_file_cache),  # clone
        query_depth=parent_ctx.query_depth + 1,
    )
```

### 第 3 步：内置 agent 类型

四种内置类型，各有各的用途。源码参考：`builtInAgents.ts`。

```python
EXPLORE  = AgentDef(model="haiku", disallow=["Agent","FileEdit","FileWrite"],
                    omit_claude_md=True)   # fast, read-only
PLAN     = AgentDef(model="inherit", disallow=["Agent","FileEdit","FileWrite"],
                    omit_claude_md=True)   # architect, read-only
GENERAL  = AgentDef(tools=["*"])           # full access
VERIFY   = AgentDef(model="inherit", disallow=["Agent","FileEdit","FileWrite"],
                    background=True)       # async, read-only
```

### 第 4 步：Agent runner

Runner 管整个生命周期：解析模型、构建 system prompt、组装 tools、创建上下文、暴露 CacheSafeParams、跑查询循环、记录 transcript、清理资源。源码参考：`runAgent.ts`。

```python
class AgentRunner:
    def run_agent(self, agent_def, prompt, fork_context_messages=None):
        resolved_model = agent_def.model or "claude-sonnet-4-20250514"
        system_prompt = agent_def.system_prompt
        if not agent_def.omit_claude_md:
            system_prompt += "\n\n" + claude_md_rules

        resolved_tools = [t for t in self.all_tools
                          if t not in agent_def.disallowed_tools]

        ctx = create_subagent_context(self.parent_context)
        # Expose CacheSafeParams for cache sharing
        # Run query loop, record transcript, cleanup
```

### 第 5 步：Sidechain transcript 记录

每个子 agent 的消息通过 UUID 链增量记录。崩溃后能恢复，事后能调试。源码参考：`sessionStorage.ts`。

## 变更内容

| 组件 | 之前 (s07) | 之后 (s08) |
|------|-----------|-----------|
| 子 agent prompt | 不存在 | CacheSafeParams 实现 API 缓存命中 |
| Agent 状态 | 不存在 | 隔离上下文，缓存是克隆的 |
| Agent 类型 | 不存在 | 4 种内置：Explore、Plan、General、Verify |
| Transcript | 不存在 | Sidechain 记录，UUID 链 |
| 只读限制 | 不存在 | 写 tools 放进 disallowed 列表 |
| 后台工作 | 不存在 | Verification agent 异步运行 |
| 自定义 agent | 不存在 | 从 `.claude/agents/*.md` 加载 |

## 试一试

```bash
cd learn-the-actual-claude-code
python agents/s08_subagents.py
```

演示会生成每种 agent 类型，捕获 CacheSafeParams，从 markdown 文件加载自定义 agent，检查 sidechain transcript。

试试这些 prompt 来看子 agent 的效果：

- "Find all files related to authentication"（Explore agent）
- "Plan how to refactor the auth module"（Plan agent）
- "Implement the refactoring, then verify it works"（General + Verify agents）
