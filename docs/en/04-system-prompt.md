# Chapter 4: The System Prompt

The agent loop (Chapter 1) sends messages to the API; the tool system (Chapters 2-3) defines what the model can do. The system prompt defines what the model _knows_ — its identity, rules, environment, available tools, and user-provided instructions. This chapter describes how Claude Code assembles a system prompt from dozens of sources into a single, cache-optimized string.

## The Problem

A system prompt for an AI coding assistant must serve multiple purposes simultaneously. It must establish the model's identity and behavioral boundaries. It must describe every available tool, including dynamically registered MCP tools that vary per session. It must convey the user's project-specific instructions (coding style, forbidden patterns, preferred libraries). It must communicate environment facts (operating system, current directory, git branch). And it must do all of this within a finite token budget while maximizing cache hit rates across requests.

These requirements conflict. Tool descriptions are static across a session, but environment facts change with every command. User instructions are stable within a project but vary across projects. If static and dynamic content are interleaved, every change to a dynamic section invalidates the entire prompt cache, forcing the API to re-process thousands of tokens that have not changed.

The Anthropic API's prompt caching is prefix-based: the cache key is computed from the beginning of the prompt, and a cache hit occurs only if the prefix matches exactly. This means that even a single changed character in the middle of the prompt invalidates the cache for everything that follows it. The prompt assembly system must therefore separate content by volatility: stable content first (cached aggressively), volatile content last (re-processed cheaply because it is short and at the end).

## How Claude Code Solves It

### The Assembly Function

System prompt assembly lives in `prompts.ts` (914 lines). The top-level function collects sections from multiple sources and concatenates them into a single string. Despite the file's length, the logic is straightforward: gather sections, sort them by cacheability, join them with a boundary marker.

### Two-Layer Section Architecture

Every section of the system prompt is created through one of two constructors:

```typescript
// src/prompts.ts — section constructors (conceptual)
systemPromptSection(title: string, content: string)
DANGEROUS_uncachedSystemPromptSection(title: string, content: string)
```

The naming is deliberate. `systemPromptSection()` produces cached sections — content that is identical across requests within a session. `DANGEROUS_uncachedSystemPromptSection()` produces volatile sections that may change between requests. The "DANGEROUS" prefix is a code-smell signal: adding uncached sections hurts cache hit rates, so every such addition should be scrutinized during code review.

The assembled prompt has this structure:

```
[cached section 1]
[cached section 2]
...
[cached section N]
SYSTEM_PROMPT_DYNAMIC_BOUNDARY
[uncached section 1]
[uncached section 2]
...
```

The `SYSTEM_PROMPT_DYNAMIC_BOUNDARY` is a literal string marker inserted into the prompt text. It tells the API client where to set the cache breakpoint: everything above the boundary is sent with a cache-control header indicating it should be cached; everything below is sent without caching. This single architectural decision determines the cache economics of the entire system.

In practice, the cached prefix typically constitutes 80-90% of the system prompt's tokens. The dynamic tail is small — usually a few hundred tokens of environment info and CLAUDE.md content. This means that on the vast majority of requests, 80-90% of the system prompt processing cost is eliminated by caching.

### Static (Cached) Sections

The sections above the cache boundary are stable across an entire session. They change only when Claude Code is updated or when the set of enabled tools changes.

**1. Identity.** The model's name, version, and core behavioral framing. A few sentences establishing that this is Claude Code, an AI coding assistant running in a terminal. This section is deliberately minimal — it exists to anchor the model's self-concept, not to provide detailed instructions. Keeping it short means it changes rarely, even across Claude Code versions.

**2. System rules.** Hard constraints on what the model must and must not do. These include safety boundaries (never execute commands that could damage the host system without explicit permission), output formatting rules (use markdown for code, keep responses concise), and interaction protocols (ask for clarification when requirements are ambiguous rather than guessing). The rules section is one of the longest static sections, typically several hundred tokens. Rules are phrased as directives rather than suggestions, because the model follows explicit instructions more reliably than implicit norms.

**3. Tool usage guidance.** General instructions for how to use tools effectively, distinct from individual tool documentation. Examples include: prefer reading a file before editing it (to avoid clobbering content the model has not seen), use absolute paths rather than relative paths (to avoid ambiguity when the working directory changes), prefer targeted searches over reading entire files (to conserve context tokens), and check file existence before attempting writes (to avoid creating files in the wrong location). This section encodes best practices that apply across all tools, reducing the need for each tool's individual prompt to repeat common patterns.

**4. Tone guidance.** Instructions for communication style. Be concise. Be technical. Do not add unnecessary preamble ("Sure, I can help with that!"). Do not repeat the user's question back to them. Provide code directly rather than describing what code to write. Do not apologize for errors — fix them. These instructions shape the model's output to match developer expectations: developers want results, not pleasantries.

**5. Individual tool prompts.** Each tool in the assembled pool contributes its own prompt text via the `.prompt()` method described in Chapter 2. A tool like Bash might contribute a paragraph explaining its timeout behavior, how to use it for background processes, and how exit codes are reported. A tool like FileRead might explain its line-number format and how to request specific line ranges. A tool like Grep might describe its regex syntax and available flags.

Because the tool pool is sorted alphabetically (Chapter 2), this section's content is deterministic for a given set of enabled tools. If Bash is always first and WebSearch is always last, the token sequence is identical across requests, which is essential for prefix-based caching. Adding or removing a tool (e.g., connecting an MCP server) changes this section and invalidates the cache — but tool set changes are infrequent within a session, typically happening at most once (at startup).

### Dynamic (Per-Request) Sections

Below the cache boundary, sections that may change between requests:

**1. CLAUDE.md hierarchy.** User-provided instruction files, loaded from up to four tiers:

- **Managed**: `~/.claude/CLAUDE.md` sections managed by Claude Code itself — auto-generated summaries of project structure, common patterns, or learned preferences. The model can write to this tier during a session.
- **User**: `~/.claude/CLAUDE.md` — per-user instructions that apply to all projects. A developer might put personal preferences here: "use vim keybindings in examples," "prefer functional style," or "respond in Japanese."
- **Project**: `<project-root>/CLAUDE.md` — project-wide instructions checked into version control. The entire team sees these. Typical contents: coding standards, test conventions, architecture notes, CI pipeline requirements.
- **Local**: `<project-root>/.claude/CLAUDE.md` — local overrides not checked into version control (the `.claude/` directory is typically in `.gitignore`). Useful for personal preferences that should not be imposed on the team, or for experimental instructions.

Files are loaded in this order. When instructions conflict, later tiers take precedence over earlier ones — local overrides project, project overrides user, user overrides managed. This precedence order matches the specificity of each tier: the most specific context (local) wins.

The content from all four tiers is concatenated into a single section of the system prompt, with clear headers indicating which tier each instruction came from. This transparency lets the model (and the user, if they inspect the prompt) understand the provenance of each instruction.

**2. MEMORY.md.** The persistent memory file (`~/.claude/MEMORY.md`) stores facts the model has learned across sessions. Examples: "this project uses pnpm, not npm," "the user prefers const over let," "the API server runs on port 3001 in development." The model can write to this file during a session (via a dedicated tool or as part of its response processing), and the contents are loaded into the system prompt at assembly time.

MEMORY.md is subject to hard truncation limits: 200 lines or 25 KB, whichever limit is hit first. The truncation is applied from the end of the file, preserving the oldest entries. The rationale: older entries have survived multiple sessions and are likely more important than recent additions that may be session-specific or redundant.

The truncation prevents a growing memory file from consuming an ever-larger share of the context window, which would gradually degrade performance on actual coding tasks. A memory file that grows to 500 lines over months of use would consume thousands of tokens — context that could instead be used for code, tool results, or conversation history. The hard cap forces the memory system to stay concise and high-signal.

Unlike CLAUDE.md files, MEMORY.md is not organized by topic or tier. It is a flat list of facts, accumulated chronologically. Users who need more extensive persistent context are better served by CLAUDE.md files, which can be edited manually, organized by topic, and distributed across the four-tier hierarchy.

**3. Environment information.** A structured block containing:

- Current working directory (absolute path).
- Git status: current branch, whether the working tree is dirty, number of staged/unstaged changes.
- Platform: operating system name and version (e.g., "macOS 14.2", "Ubuntu 22.04").
- Shell: the user's default shell (bash, zsh, fish, etc.).
- Active model name (e.g., "claude-sonnet-4-20250514").
- Knowledge cutoff date: the date through which the model's training data extends.

This section changes whenever the user changes directories, switches git branches, makes commits, or stages files. It is relatively small (typically 50-100 tokens) and re-processed on every request, which is inexpensive given its size.

The environment information is crucial for tool usage: the model needs to know the current directory to construct correct file paths, needs the git branch to avoid accidental commits to main, and needs the platform to generate platform-appropriate shell commands. Without this section, the model would need to run diagnostic tools (like `pwd` and `git status`) at the start of every turn, wasting tool calls and latency on information the runtime already possesses.

**4. MCP instructions.** If any MCP (Model Context Protocol) servers are connected, their server-provided instructions are included here. An MCP server can declare instructions that describe its tools' intended use, limitations, and conventions — for example, a GitHub MCP server might instruct the model to prefer pull request drafts over direct pushes, or a database MCP server might warn against running DDL statements.

Because MCP servers can connect and disconnect during a session (e.g., a database server that restarts, or a user adding a new server via `mcp add`), this content is inherently volatile and must be in the uncached section. The MCP instructions section is typically small (100-300 tokens per server), but it can grow significantly if many servers are connected simultaneously.

### CLAUDE.md Include Directives

CLAUDE.md files support an `@include` directive that pulls in content from other files:

```markdown
# Project Instructions

@include ./docs/coding-standards.md
@include ./docs/api-conventions.md
```

This allows project instructions to be modular. A large monorepo might have separate instruction files for the frontend, backend, and infrastructure subsystems, with the root CLAUDE.md including only the subset relevant to the current working directory. A microservices project might have a shared instruction file for common conventions, included by each service's own CLAUDE.md.

The include resolution happens at prompt assembly time — the included files are read from disk and their content is spliced into the CLAUDE.md content before it enters the prompt. Paths are resolved relative to the directory containing the CLAUDE.md file, not relative to the current working directory. This ensures that includes work correctly regardless of where Claude Code is launched from within the project.

Included files are subject to the same size limits as the parent CLAUDE.md. If the total content after include resolution exceeds the per-tier caps, it is truncated. Circular includes (A includes B, B includes A) are detected by tracking the include chain during resolution, and the cycle is broken with a warning rather than causing infinite recursion. Nested includes are supported: if an included file itself contains `@include` directives, those are resolved recursively (subject to the circular-include guard).

### The SIMPLE Mode Escape Hatch

For contexts where the full system prompt is unnecessary or counterproductive, a `SIMPLE` mode strips most guidance sections. In SIMPLE mode, the system prompt contains only the identity section and basic safety rules, omitting tool guidance, tone instructions, and most CLAUDE.md content.

SIMPLE mode is useful in several scenarios:

- One-shot questions where the overhead of a large system prompt is not justified by the task complexity.
- Automated pipelines where Claude Code is used as a simple command executor and does not need behavioral guidance.
- Debugging, where a minimal prompt helps isolate whether unexpected model behavior is caused by the system prompt or by other factors.

The token savings are significant: a full system prompt might be 5,000-8,000 tokens, while SIMPLE mode might use 500-1,000. For high-volume automated use cases, this reduction translates directly into lower API costs.

### Tools Inject Their Own Prompts

As noted in Chapter 2, each tool provides its own prompt text via the `.prompt()` method. During system prompt assembly, the assembler iterates over the tool pool and concatenates each tool's prompt contribution:

```typescript
// src/prompts.ts — tool prompt injection (conceptual)
for (const tool of toolPool) {
  const toolPrompt = tool.prompt()
  if (toolPrompt) {
    sections.push(systemPromptSection(tool.name, toolPrompt))
  }
}
```

Because tool prompts come from the tools themselves (not from a central registry), adding a new tool automatically adds its documentation to the system prompt. No separate registration step is needed. Removing a tool removes its documentation. Modifying a tool's behavior and updating its prompt happen in the same code change. This co-location prevents the documentation drift that is common in systems where tool descriptions are maintained separately from tool implementations.

Note that tool prompts are placed in the _cached_ section of the system prompt. This is correct because the tool set does not change between requests within a session (MCP tool changes trigger a prompt reassembly, which effectively starts a new cache epoch). The alphabetical ordering of tools in the pool (Chapter 2) ensures that the cached section is byte-identical across requests as long as the tool set is unchanged.

## Key Design Decisions

**Cache boundary as an explicit marker.** Rather than relying on the API to infer which parts of the prompt are cacheable, Claude Code places an explicit boundary string. This gives the system precise control over cache behavior and makes it visible in the code exactly where the boundary falls. Moving a section from above to below the boundary (or vice versa) is a one-line change with obvious cache implications. The explicitness also makes it easy to measure the cache ratio (cached tokens / total tokens) during development.

**Four-tier CLAUDE.md hierarchy.** The managed/user/project/local layering mirrors configuration systems in tools like git (`system` > `global` > `local`) and editors like VS Code (default > user > workspace). It provides a natural escalation path: organization-wide conventions in the managed tier, personal preferences in the user tier, project standards in the project tier, and local experimentation in the local tier. The layering is ordered so that more-specific instructions override more-general ones, which matches developer intuition about configuration precedence.

**MEMORY.md truncation.** The 200-line / 25 KB cap is a hard limit, not a suggestion. Without it, a model that aggressively writes to MEMORY.md over many sessions could accumulate a file that consumes a significant fraction of the context window, degrading performance on actual tasks. The cap forces the memory system to stay concise and high-signal. Users who need more extensive persistent context are better served by CLAUDE.md files, which can be edited manually and organized hierarchically. The truncation boundary is also a useful forcing function for memory quality: when the file nears its limit, older low-value entries are naturally displaced by newer, more relevant ones.

**Tool prompt ordering by name.** Sorting tool prompts alphabetically is a cache optimization that has no semantic significance — the model does not care what order tool descriptions appear in. But the API's prompt caching is prefix-based: identical prefixes share cache entries. If tool ordering varied between requests (due to, say, non-deterministic iteration over a set or hash map), every request would miss the cache. Alphabetical sorting eliminates this source of cache variance at zero cost. The same principle applies to any list embedded in the cached section of the prompt: deterministic ordering is a prerequisite for effective prefix caching.

**DANGEROUS_ prefix convention.** Naming the uncached section constructor with a DANGEROUS_ prefix is a social-technical mechanism. It does not enforce anything at the type level, but it ensures that every code review of a new uncached section triggers a conversation about whether the content truly needs to be volatile. The naming convention embeds the cost model into the API surface: adding an uncached section is easy, but the name forces the author to acknowledge that they are degrading cache performance. Most content does not need to be volatile, and the naming pressure helps keep it in the cached section.

## In Practice

When a developer starts Claude Code in a project directory, the system prompt is assembled once. The static portion — identity, rules, tool descriptions — is identical for every user running the same version of Claude Code with the same tools enabled, so the API can serve it from cache across all users globally. The dynamic portion — CLAUDE.md content, environment info, memory — is small (typically a few hundred tokens) and re-processed cheaply on each request.

If the project has a `CLAUDE.md` file with instructions like "always use single quotes in TypeScript" or "run `npm test` after editing files in src/", the model sees these instructions on every turn and follows them. If the user has personal preferences in `~/.claude/CLAUDE.md` like "respond in British English" or "prefer explicit type annotations," those appear too, but project-level instructions take precedence if they conflict.

When the user connects an MCP server mid-session (e.g., `mcp add github`), the prompt is reassembled: the new tool's prompt text is added to the cached section, the MCP instructions are added to the dynamic section, and a new cache epoch begins. Subsequent requests cache the updated prefix.

The effect is a system prompt that feels personalized and context-aware, yet is cheap to transmit because most of it is cached. A typical session might process 6,000 cached tokens and 400 uncached tokens per request — a 94% cache hit rate that directly reduces API latency and cost.

The system prompt assembly is also deterministic: given the same Claude Code version, tool set, CLAUDE.md files, and environment state, the same prompt is produced. This determinism is important for debugging — if a user reports unexpected model behavior, the prompt can be reconstructed exactly from the known inputs.

## Summary

- The system prompt is split into cached (static) and uncached (volatile) sections, separated by an explicit `SYSTEM_PROMPT_DYNAMIC_BOUNDARY` marker that controls prefix-based prompt caching. Typically 80-90% of prompt tokens are in the cached prefix.
- Static sections include identity, system rules, tone guidance, tool usage guidance, and individual tool prompts (sorted alphabetically for cache determinism). Dynamic sections include CLAUDE.md files, MEMORY.md, environment info, and MCP instructions.
- CLAUDE.md files follow a four-tier hierarchy (managed, user, project, local) with later tiers overriding earlier ones. The `@include` directive enables modular instructions with recursive resolution and circular-include detection.
- MEMORY.md is hard-capped at 200 lines / 25 KB to prevent unbounded context consumption across sessions. Truncation preserves the oldest (most durable) entries.
- Tool prompts are injected by the tools themselves via `.prompt()`, ensuring co-location of documentation with implementation. The `DANGEROUS_uncachedSystemPromptSection` naming convention pressures contributors to keep content in the cached section unless volatility is genuinely required.
