# Session 04 -- 系统提示词构建

`s01 > s02 > s03 > [ s04 ] s05 | s06 > s07 > s08 > s09 > s10 | s11 > s12 > s13 > s14`

> "Cached/uncached sections, CLAUDE.md hierarchy (managed/user/project/local), memory attachment, tool prompt deferral."
> "缓存/非缓存段落、CLAUDE.md 层级结构（managed/user/project/local）、记忆附加、工具提示词延迟加载。"
>
> *实践层：`agents/s04_system_prompt.py` 用约 560 行 Python 重新实现了完整的提示词装配流水线 —— 段落注册表、缓存、CLAUDE.md 加载、记忆截断和缓存分割边界。*

---

## 问题

一个简单的 Agent 只有一个单一的系统提示词字符串。Claude Code 的系统提示词需要解决更复杂的问题：

- **提示词缓存花费真金白银。** 缓存边界之前的每个 token 在缓存失效时都会被重新处理。修改系统提示词中的一个词就会使整个缓存失效。
- **指令来自 5 个来源** —— 管理策略、用户全局、项目根目录、本地覆盖和自动记忆 —— 每个来源有不同的信任级别和覆盖语义。
- **某些段落每轮都会变化**（MCP 服务器连接、当前日期），而其他段落在整个会话中保持稳定。
- **记忆文件可能很大** —— MEMORY.md 需要在 200 行 / 25 KB 处截断以避免上下文爆炸。
- **工具描述放在 API schema 中**，而不是系统提示词文本中 —— 但系统提示词会引用工具名称来提供行为指导。

---

## 解决方案

Claude Code 将系统提示词分为**静态**（全局可缓存）和**动态**（每会话）两半，由边界标记分隔：

```
  STATIC (cache_scope: "global")
  +------------------------------------+
  |  Intro section                     |
  |  System rules                      |
  |  Doing tasks guidance              |
  |  Using your tools section          |
  |  Tone and style                    |
  |  Output efficiency                 |
  +------------------------------------+
  === DYNAMIC BOUNDARY ===
  +------------------------------------+
  |  CLAUDE.md hierarchy (5 tiers)     |  <- cached per session
  |  Memory prompt (MEMORY.md)         |  <- cached per session
  |  Environment info (OS, CWD, git)   |  <- cached per session
  |  Language preference               |  <- cached per session
  |  MCP instructions                  |  <- UNCACHED (volatile!)
  +------------------------------------+
```

---

## 工作原理

### 1. 段落注册表：缓存 vs. 非缓存

每个段落都带有明确的缓存决策进行注册。`DANGEROUS_` 前缀强制作者为破坏缓存提供理由：

```python
@dataclass
class SystemPromptSection:
    """A named, optionally cached section of the system prompt."""
    name: str
    compute: Callable[[], Optional[str]]
    cache_break: bool = False  # True = recompute every turn (DANGEROUS)


def system_prompt_section(name, compute):
    """Cached section -- computed once, reused until clear/compact."""
    return SystemPromptSection(name=name, compute=compute, cache_break=False)


def dangerous_uncached_section(name, compute, reason):
    """Volatile section -- recomputed every turn, breaks prompt cache.
    The reason parameter forces the author to justify the cache break."""
    return SystemPromptSection(name=name, compute=compute, cache_break=True)
```

会话缓存解析段落，跳过已缓存段落的重新计算：

```python
class SectionCache:
    def resolve(self, sections: list[SystemPromptSection]) -> list[Optional[str]]:
        results = []
        for s in sections:
            if not s.cache_break and s.name in self._store:
                results.append(self._store[s.name])
            else:
                value = s.compute()
                self._store[s.name] = value
                results.append(value)
        return results
```

### 2. CLAUDE.md 层级结构（5 层）

指令从严格的层级结构中加载，每层都有标注的信任级别：

```python
TYPE_DESCRIPTIONS = {
    "Managed": "",
    "User":    " (user's private global instructions for all projects)",
    "Project": " (project instructions, checked into the codebase)",
    "Local":   " (user's private project instructions, not checked in)",
    "AutoMem": " (user's auto-memory, persists across conversations)",
}


def load_claude_md_hierarchy(cwd: str) -> list[MemoryFileInfo]:
    """Walk the four-tier hierarchy and load all instruction files.

    Order (real code, claudemd.ts:790-1074):
      1. Managed  -> /etc/claude-code/CLAUDE.md
      2. User     -> ~/.claude/CLAUDE.md
      3. Project  -> CLAUDE.md, .claude/CLAUDE.md, .claude/rules/*.md
      4. Local    -> CLAUDE.local.md (from root down to CWD)
      5. AutoMem  -> MEMORY.md from auto-memory directory
    """
    results = []
    seen = set()

    def try_load(filepath, mem_type):
        real = os.path.realpath(filepath)
        if real in seen:
            return
        seen.add(real)
        # ... load and append ...

    # 1. Managed (admin policy)
    try_load("/etc/claude-code/CLAUDE.md", "Managed")

    # 2. User (private global)
    try_load(str(home / ".claude" / "CLAUDE.md"), "User")

    # 3. Project + 4. Local -- walk from root toward CWD
    for directory in ancestors_to_cwd:
        try_load(os.path.join(d, "CLAUDE.md"), "Project")
        try_load(os.path.join(d, ".claude", "CLAUDE.md"), "Project")
        # .claude/rules/*.md files too
        try_load(os.path.join(d, "CLAUDE.local.md"), "Local")

    return results
```

### 3. 记忆截断（MEMORY.md）

大型记忆文件会在自然边界处截断以避免上下文爆炸：

```python
MAX_ENTRYPOINT_LINES = 200    # memdir.ts:35
MAX_ENTRYPOINT_BYTES = 25_000  # memdir.ts:38


def truncate_entrypoint(raw: str) -> str:
    """Enforce 200-line and 25 KB caps on MEMORY.md content."""
    lines = raw.strip().split("\n")

    was_line_truncated = len(lines) > MAX_ENTRYPOINT_LINES
    was_byte_truncated = len(raw.encode("utf-8")) > MAX_ENTRYPOINT_BYTES

    if not was_line_truncated and not was_byte_truncated:
        return raw.strip()

    truncated = "\n".join(lines[:MAX_ENTRYPOINT_LINES])

    if len(truncated.encode("utf-8")) > MAX_ENTRYPOINT_BYTES:
        cut_at = truncated.rfind("\n", 0, MAX_ENTRYPOINT_BYTES)
        truncated = truncated[:cut_at]

    return (
        truncated
        + "\n\n> WARNING: MEMORY.md is too large. Only part was loaded. "
        "Keep index entries to one line under ~200 chars."
    )
```

### 4. 动态环境注入

每个会话都会获得操作系统、工作目录、git 状态、模型标识和知识截止日期：

```python
def compute_env_info(model_id="claude-opus-4-6", cwd=None) -> str:
    """Real code: computeSimpleEnvInfo() in prompts.ts:651-710"""
    cwd = cwd or os.getcwd()
    is_git = os.path.isdir(os.path.join(cwd, ".git"))

    cutoffs = {
        "claude-opus-4-6": "May 2025",
        "claude-sonnet-4-6": "August 2025",
    }
    cutoff = cutoffs.get(model_id)

    items = [
        f"Primary working directory: {cwd}",
        f"Is a git repository: {is_git}",
        f"Platform: {platform.system().lower()}",
        f"Shell: {shell_name}",
        f"OS Version: {uname_sr}",
        f"You are powered by the model {model_id}.",
    ]
    if cutoff:
        items.append(f"Assistant knowledge cutoff is {cutoff}.")

    return "# Environment\n" + "\n".join(f" - {item}" for item in items)
```

### 5. 装配与缓存分割

构建器装配静态 + 动态两半，由边界标记分隔，告诉 API 缓存的作用范围：

```python
SYSTEM_PROMPT_DYNAMIC_BOUNDARY = "__SYSTEM_PROMPT_DYNAMIC_BOUNDARY__"

class SystemPromptBuilder:
    def build(self) -> list[str]:
        static = [
            self._intro_section(),
            self._system_section(),
            self._doing_tasks_section(),
            self._using_tools_section(),
            self._tone_section(),
            self._output_efficiency_section(),
        ]
        boundary = [SYSTEM_PROMPT_DYNAMIC_BOUNDARY]
        dynamic_defs = self._build_dynamic_sections()
        dynamic_values = self._cache.resolve(dynamic_defs)

        return [s for s in static + boundary + dynamic_values if s is not None]

    def split_for_caching(self, prompt_array):
        """Split at the boundary for API cache scoping.
        Real code: splitSysPromptPrefix() in api.ts:321-400"""
        idx = prompt_array.index(SYSTEM_PROMPT_DYNAMIC_BOUNDARY)
        static_text = "\n\n".join(prompt_array[:idx])
        dynamic_text = "\n\n".join(prompt_array[idx + 1:])

        return {
            "blocks": [
                {"text": static_text, "cache_scope": "global"},
                {"text": dynamic_text, "cache_scope": None},
            ]
        }
```

---

## 变化对比

| 组件 | 之前（教程风格） | 之后（Claude Code） |
|---|---|---|
| 系统提示词 | 单个硬编码字符串 | 带缓存边界的多段落数组 |
| 缓存 | 无感知 | 静态（全局缓存）/ 动态分割 |
| 指令 | 内联或单文件 | 带信任标签的 5 层 CLAUDE.md 层级结构 |
| 记忆 | 不适用 | MEMORY.md，200 行 / 25 KB 截断 |
| 易变性 | 全部重新计算 | 明确的缓存 vs. `DANGEROUS_uncached` 段落 |
| 工具提示词 | 在系统提示词文本中 | 延迟到 API 工具 schema（`.prompt()` 方法） |
| 环境 | 硬编码 | 动态：操作系统、工作目录、git 状态、模型、截止日期 |

---

## 试一试

```bash
cd agents
python s04_system_prompt.py
```

演示将会：
1. 构建完整的提示词数组，展示每个段落及其索引
2. 在动态边界处分割，展示缓存作用范围
3. 展示工具描述（API schema，而非系统提示词）
4. 从你的当前工作目录扫描真实的 CLAUDE.md 层级结构
5. 第二次构建以演示段落缓存命中

你将看到类似这样的输出：

```
Prompt array has 10 sections

  [0] You are an interactive agent that helps users with software engineering...
  [1] # System  - All text you output outside of tool use is displayed to ...
  ...
  [6] === DYNAMIC BOUNDARY ===
  [7] # Environment You have been invoked in the following environment: ...
  [8] # MCP Server Instructions ...

Cache split: 2 blocks
  scope=global, length=1423 chars
  scope=per-session, length=489 chars
```

**接下来可以探索的源文件：**
- `src/constants/prompts.ts` -- `getSystemPrompt()` 和所有段落构建器 (914 LOC)
- `src/constants/systemPromptSections.ts` -- 段落注册表和缓存
- `src/utils/claudemd.ts` -- CLAUDE.md 层级结构加载
- `src/memdir/memdir.ts` -- 记忆提示词加载和截断
- `src/utils/api.ts` -- `splitSysPromptPrefix()` 缓存作用范围分割
