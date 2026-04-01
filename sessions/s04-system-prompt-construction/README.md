# Session 04 -- System Prompt Construction

## What You Will Learn

Claude Code does not send a single hardcoded string as its system prompt. The
prompt is a multi-layered, dynamically assembled document that changes based on
session state, enabled tools, model identity, OS environment, user instructions
(CLAUDE.md files), and persistent memory (MEMORY.md). This session dissects
exactly how that assembly works.

### Learning Objectives

1. **Two-layer section architecture** -- understand the difference between
   `systemPromptSection()` (cached, computed once) and
   `DANGEROUS_uncachedSystemPromptSection()` (recomputed every turn, breaks
   prompt cache).
2. **The dynamic boundary** -- how `SYSTEM_PROMPT_DYNAMIC_BOUNDARY` splits the
   prompt into a globally cacheable static prefix and a per-session dynamic
   suffix.
3. **CLAUDE.md hierarchy** -- how instructions are loaded from four tiers:
   Managed -> User -> Project -> Local, with directory traversal from root
   toward CWD and `.claude/rules/*.md` conditional rules.
4. **Memory attachment** -- how MEMORY.md is loaded from the auto-memory
   directory, truncated at 200 lines / 25 KB, and injected into the system
   prompt via a `systemPromptSection('memory', ...)` entry.
5. **Tool prompt injection** -- every tool implements a `.prompt()` method whose
   return value becomes the `description` field in the API tool schema, not
   part of the system prompt text.
6. **Dynamic sections** -- OS info, CWD, git status, timezone, model identity,
   knowledge cutoff, and shell type are all computed and included as an
   `# Environment` section.
7. **Override mechanisms** -- custom system prompts via SDK, append system
   prompts, language preferences, output style configs, and MCP server
   instructions.

---

## Key Source Files

| File | Purpose |
|------|---------|
| `src/constants/prompts.ts` | Master orchestrator -- `getSystemPrompt()` assembles static + dynamic sections |
| `src/constants/systemPromptSections.ts` | `systemPromptSection()`, `DANGEROUS_uncachedSystemPromptSection()`, `resolveSystemPromptSections()` |
| `src/utils/claudemd.ts` | CLAUDE.md loader -- hierarchy walk, frontmatter parsing, `getClaudeMds()` formatter |
| `src/memdir/memdir.ts` | Memory system -- `loadMemoryPrompt()`, `buildMemoryLines()`, truncation logic |
| `src/memdir/paths.ts` | Auto-memory path resolution, `isAutoMemoryEnabled()` |
| `src/utils/api.ts` | `splitSysPromptPrefix()` -- splits prompt at the dynamic boundary for cache scoping |
| `src/services/api/claude.ts` | `buildSystemPromptBlocks()` -- final API payload assembly |

---

## What shareAI-lab and Similar Clones Get Wrong

Most open-source reimplementations of "Claude Code" use a single hardcoded
system prompt string. Here is what that misses:

| Aspect | Real Claude Code | Typical Clone |
|--------|-----------------|---------------|
| **Caching** | Two-tier: static prefix gets `cacheScope: 'global'`; dynamic suffix recomputed per turn | No caching strategy at all |
| **CLAUDE.md** | Four-tier hierarchy (managed/user/project/local) with directory walk, `.claude/rules/*.md` conditional rules, `@include` directives, frontmatter path globs | Reads a single file or ignores CLAUDE.md entirely |
| **Memory** | Persistent `MEMORY.md` in `~/.claude/projects/<slug>/memory/` with 200-line / 25 KB truncation, typed memory taxonomy | No memory system |
| **Tool prompts** | Each tool's `.prompt()` method generates its own API description dynamically | Static tool descriptions or none |
| **Environment** | OS, shell, CWD, git status, model identity, knowledge cutoff injected per session | Maybe CWD |
| **Section registry** | Named sections with compute-once memoization and cache invalidation on `/clear` or `/compact` | N/A |
| **MCP instructions** | Server-provided instructions injected via uncached section or delta attachments | Not supported |
| **Output styles** | Configurable output style prompt appended to identity section | Not supported |

---

## Session Files

- `SOURCE_ANALYSIS.md` -- deep annotated walkthrough of the source code
- `reimplementation.py` -- runnable Python that demonstrates the architecture

## Prerequisites

- Session 01 (bootstrap/agent loop) for understanding the query cycle
- Session 02 (tool interface) for understanding how `.prompt()` works on tools
