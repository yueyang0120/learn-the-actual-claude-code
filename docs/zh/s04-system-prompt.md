# s04: System Prompt

`s01 > s02 > s03 > [ s04 ] s05`

> *"Static half caches globally, dynamic half rebuilds per session"* -- 一个边界标记把 prompt 一分为二, 给 API cache 用。

## 问题

s01 里系统 prompt 就是一个硬编码字符串。生产环境中, Claude Code 的 prompt 从多个来源组装 -- managed policy, 用户指令, 项目规则, 环境信息, MCP 服务器状态 -- cache 边界之前的每个 token 在 cache miss 时都要花钱。随便改一个段落就会让整个 cache 失效。

## 解决方案

```
  STATIC (cache_scope: "global")
  +--------------------------------------+
  |  Intro / System rules / Task guide   |
  |  Tool usage / Tone / Efficiency      |
  +--------------------------------------+
       === DYNAMIC BOUNDARY ===
  +--------------------------------------+
  |  CLAUDE.md hierarchy (5 tiers)       |  cached per session
  |  MEMORY.md (truncated at 200 lines)  |  cached per session
  |  Environment (OS, CWD, git, model)   |  cached per session
  |  MCP instructions                    |  UNCACHED (volatile)
  +--------------------------------------+
```

边界以上的部分跨 session 全局缓存, 边界以下的部分按 session 缓存。MCP 指令显式标记为 uncached, 因为服务器会在 turn 之间连接和断开。真实代码：`src/constants/prompts.ts`, `src/utils/api.ts`。

## 工作原理

**1. 每个段落注册时带上明确的缓存决策。**

```python
def system_prompt_section(name, compute):
    """Cached -- computed once, reused until /clear."""
    return Section(name=name, compute=compute, cache_break=False)

def dangerous_uncached_section(name, compute, reason):
    """Recomputed every turn. The reason parameter forces the author
    to justify breaking the cache."""
    return Section(name=name, compute=compute, cache_break=True)
```

`DANGEROUS_` 前缀是真实存在的 -- 就是为了让你三思。

**2. 指令从 5 层 CLAUDE.md 层级加载。**

```python
# 1. Managed  -> /etc/claude-code/CLAUDE.md       (admin policy)
# 2. User     -> ~/.claude/CLAUDE.md               (private global)
# 3. Project  -> CLAUDE.md, .claude/rules/*.md      (checked in)
# 4. Local    -> CLAUDE.local.md                    (not checked in)
# 5. AutoMem  -> MEMORY.md                          (persists across chats)
```

每层有标注的信任级别。加载器从根目录走到 CWD, 按真实路径去重。真实代码：`src/utils/claudemd.ts`（~1,000 LOC）。

**3. MEMORY.md 会被截断, 防止撑爆上下文。**

```python
MAX_LINES = 200
MAX_BYTES = 25_000

def truncate_entrypoint(raw: str) -> str:
    lines = raw.split("\n")
    if len(lines) <= MAX_LINES and len(raw.encode()) <= MAX_BYTES:
        return raw
    truncated = "\n".join(lines[:MAX_LINES])
    if len(truncated.encode()) > MAX_BYTES:
        truncated = truncated[:truncated.rfind("\n", 0, MAX_BYTES)]
    return truncated + "\n\n> WARNING: MEMORY.md truncated."
```

真实代码：`truncateEntrypointContent()`, 在 `src/memdir/memdir.ts`。

**4. 组装时在边界处分割, 给 API cache 定作用域。**

```python
prompt_array = [*static_sections, BOUNDARY, *dynamic_sections]

# Split for the API:
idx = prompt_array.index(BOUNDARY)
static_text  = "\n\n".join(prompt_array[:idx])     # cache_scope: "global"
dynamic_text = "\n\n".join(prompt_array[idx + 1:])  # cache_scope: None
```

静态半边拿到 `cache_scope: "global"`, 所有 session 共享。动态半边不缓存（或按 session 缓存）。真实代码：`splitSysPromptPrefix()`, 在 `src/utils/api.ts`。

## 变更内容

| 组件 | 之前 (s03) | 之后 (s04) |
|---|---|---|
| 系统 prompt | 单个硬编码字符串 | 多段落数组 + cache 边界 |
| 缓存 | 没有意识 | 静态（全局）/ 动态分割 |
| 指令 | 内联 | 5 层 CLAUDE.md 层级, 带信任标签 |
| 记忆 | 无 | MEMORY.md, 200 行 / 25 KB 截断 |
| 易变性 | 全部重算 | 显式区分 cached vs. `DANGEROUS_uncached` 段落 |
| 环境 | 硬编码 | 动态：OS, CWD, git status, model, cutoff |

## 试一试

```bash
cd learn-the-actual-claude-code
python agents/s04_system_prompt.py
```

注意观察：

- prompt 数组有 ~10 个段落, 中间有一个 `=== DYNAMIC BOUNDARY ===`
- cache split 产出 2 个 block：一个 `scope=global`, 一个 `scope=per-session`
- 你文件系统上真实的 CLAUDE.md 文件被发现并加载了
- 第二次构建命中了段落缓存（输出一样, 不重新计算）
