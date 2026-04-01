# Session 04 -- System Prompt Construction

`s01 > s02 > s03 > [ s04 ] s05 | s06 > s07 > s08 > s09 > s10 | s11 > s12 > s13 > s14`

> "Cached/uncached sections, CLAUDE.md hierarchy (managed/user/project/local), memory attachment, tool prompt deferral."
>
> *Harness layer: `agents/s04_system_prompt.py` reimplements the full prompt assembly pipeline in ~560 lines of Python -- section registry, caching, CLAUDE.md loading, memory truncation, and cache-split boundary.*

---

## Problem

A simple agent has a single system prompt string. Claude Code's system prompt has to solve much harder problems:

- **Prompt caching costs real money.** Every token before the cache boundary is reprocessed on cache miss. Changing one word in the system prompt invalidates the entire cache.
- **Instructions come from 5 sources** -- managed policy, user global, project root, local overrides, and auto-memory -- each with different trust levels and override semantics.
- **Some sections change every turn** (MCP server connections, current date) while others are stable for the entire session.
- **Memory files can grow large** -- MEMORY.md needs truncation at 200 lines / 25 KB to avoid blowing context.
- **Tool descriptions go in the API schema**, not the system prompt text -- but the system prompt references tool names for behavioral guidance.

---

## Solution

Claude Code splits the system prompt into **static** (globally cacheable) and **dynamic** (per-session) halves, separated by a boundary marker:

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

## How It Works

### 1. Section registry: cached vs. uncached

Every section is registered with an explicit caching decision. The `DANGEROUS_` prefix forces the author to justify breaking the cache:

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

The session cache resolves sections, skipping recomputation for cached ones:

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

### 2. CLAUDE.md hierarchy (5 tiers)

Instructions are loaded from a strict hierarchy, each with a labeled trust level:

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

### 3. Memory truncation (MEMORY.md)

Large memory files get truncated at natural boundaries to avoid blowing context:

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

### 4. Dynamic environment injection

Each session gets OS, CWD, git status, model identity, and knowledge cutoff:

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

### 5. Assembly and cache-split

The builder assembles static + dynamic halves, separated by a boundary that tells the API where to scope caching:

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

## What Changed

| Component | Before (tutorial style) | After (Claude Code) |
|---|---|---|
| System prompt | Single hardcoded string | Multi-section array with cache boundary |
| Caching | No awareness | Static (global cache) / dynamic split |
| Instructions | Inline or single file | 5-tier CLAUDE.md hierarchy with trust labels |
| Memory | N/A | MEMORY.md with 200-line / 25 KB truncation |
| Volatility | Everything recomputed | Explicit cached vs. `DANGEROUS_uncached` sections |
| Tool prompts | In system prompt text | Deferred to API tool schema (`.prompt()` method) |
| Environment | Hardcoded | Dynamic: OS, CWD, git status, model, cutoff |

---

## Try It

```bash
cd agents
python s04_system_prompt.py
```

The demo will:
1. Build the full prompt array and show each section with its index
2. Split at the dynamic boundary and show cache scoping
3. Show tool descriptions (API schema, not system prompt)
4. Scan the real CLAUDE.md hierarchy from your CWD
5. Build a second time to demonstrate the section cache hitting

You will see output like:

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

**Source files to explore next:**
- `src/constants/prompts.ts` -- `getSystemPrompt()` and all section builders (914 LOC)
- `src/constants/systemPromptSections.ts` -- section registry and cache
- `src/utils/claudemd.ts` -- CLAUDE.md hierarchy loading
- `src/memdir/memdir.ts` -- memory prompt loading and truncation
- `src/utils/api.ts` -- `splitSysPromptPrefix()` for cache scoping
