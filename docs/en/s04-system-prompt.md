# s04: System Prompt

`s01 > s02 > s03 > [ s04 ] s05`

> *"Static half caches globally, dynamic half rebuilds per session"* -- one boundary marker splits the prompt for the API cache.

## Problem

In s01 the system prompt was a single hardcoded string. In production, Claude Code's prompt is assembled from multiple sources -- managed policy, user instructions, project rules, environment info, MCP server state -- and every token before the cache boundary costs money on a cache miss. A careless change to any section invalidates the entire cache.

## Solution

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

Everything above the boundary is identical across sessions and cached globally. Everything below is per-session. MCP instructions are explicitly marked uncached because servers connect and disconnect between turns. Real code: `src/constants/prompts.ts`, `src/utils/api.ts`.

## How It Works

**1. Each section is registered with an explicit caching decision.**

```python
def system_prompt_section(name, compute):
    """Cached -- computed once, reused until /clear."""
    return Section(name=name, compute=compute, cache_break=False)

def dangerous_uncached_section(name, compute, reason):
    """Recomputed every turn. The reason parameter forces the author
    to justify breaking the cache."""
    return Section(name=name, compute=compute, cache_break=True)
```

The `DANGEROUS_` prefix is real -- it exists in the source to make you think twice.

**2. Instructions load from a 5-tier CLAUDE.md hierarchy.**

```python
# 1. Managed  -> /etc/claude-code/CLAUDE.md       (admin policy)
# 2. User     -> ~/.claude/CLAUDE.md               (private global)
# 3. Project  -> CLAUDE.md, .claude/rules/*.md      (checked in)
# 4. Local    -> CLAUDE.local.md                    (not checked in)
# 5. AutoMem  -> MEMORY.md                          (persists across chats)
```

Each tier has a labeled trust level. The loader walks from root to CWD, deduplicating by real path. Real code: `src/utils/claudemd.ts` (~1,000 LOC).

**3. MEMORY.md gets truncated to avoid blowing context.**

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

Real code: `truncateEntrypointContent()` in `src/memdir/memdir.ts`.

**4. Assembly splits at the boundary for API cache scoping.**

```python
prompt_array = [*static_sections, BOUNDARY, *dynamic_sections]

# Split for the API:
idx = prompt_array.index(BOUNDARY)
static_text  = "\n\n".join(prompt_array[:idx])     # cache_scope: "global"
dynamic_text = "\n\n".join(prompt_array[idx + 1:])  # cache_scope: None
```

The static half gets `cache_scope: "global"` so it is reused across all sessions. The dynamic half is not cached (or cached per-session). Real code: `splitSysPromptPrefix()` in `src/utils/api.ts`.

## What Changed

| Component | Before (s03) | After (s04) |
|---|---|---|
| System prompt | Single hardcoded string | Multi-section array with cache boundary |
| Caching | No awareness | Static (global) / dynamic split |
| Instructions | Inline | 5-tier CLAUDE.md hierarchy with trust labels |
| Memory | None | MEMORY.md with 200-line / 25 KB truncation |
| Volatility | Everything recomputed | Explicit cached vs. `DANGEROUS_uncached` sections |
| Environment | Hardcoded | Dynamic: OS, CWD, git status, model, cutoff |

## Try It

```bash
cd learn-the-actual-claude-code
python agents/s04_system_prompt.py
```

Example things to watch for:

- Prompt array has ~10 sections with a `=== DYNAMIC BOUNDARY ===` in the middle
- Cache split produces 2 blocks: one with `scope=global`, one with `scope=per-session`
- Real CLAUDE.md files from your filesystem are discovered and loaded
- Second build hits the section cache (identical output, no recomputation)
