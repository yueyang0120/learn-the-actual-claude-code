# Session 07 -- The Skill System: Two-Layer Loading

## Overview

Claude Code's **skill system** is the mechanism by which slash commands (like
`/commit`, `/review-pr`, custom user skills) are discovered, registered, and
executed.  The architecture is carefully optimized around a single constraint:
**system prompt tokens are expensive**.  Every token injected into the system
prompt is paid on *every* API call for the rest of the conversation, so the
skill system splits its work into two layers:

| Layer | When | What is loaded | Cost |
|-------|------|----------------|------|
| **Layer 1 -- Discovery** | Startup / system-prompt build | Name + description + whenToUse (~100 tokens per skill) | Paid on every turn |
| **Layer 2 -- Invocation** | Only when the model calls `Skill` tool | Full markdown body of the skill | Paid once, in the turn it runs |

This "pay only for what you use" design means a project can have dozens of
skills installed without bloating the context window.

## Learning Objectives

After completing this session you will understand:

1. **Multi-directory skill discovery** -- how Claude Code searches `.claude/skills/`,
   `~/.claude/skills/`, managed policy paths, legacy `/commands/`, and bundled skills.
2. **YAML frontmatter parsing** -- the metadata format (`name`, `description`,
   `whenToUse`, `allowed-tools`, `model`, `context`, `shell`, `paths`, etc.).
3. **Two-layer token optimization** -- Layer 1 injects a budget-aware summary
   list into the system prompt; Layer 2 loads the full skill body on demand.
4. **SkillTool invocation** -- how the model triggers a skill via the `Skill`
   tool, and the inline vs. forked execution paths.
5. **Bundled skills** -- the built-in skills that ship with the CLI binary
   (`/remember`, `/verify`, `/debug`, `/simplify`, etc.).
6. **MCP prompt resources becoming skills** -- how MCP servers expose prompt
   resources that are registered as skills via `mcpSkillBuilders.ts`.

## Key Source Files

| File | Purpose |
|------|---------|
| `src/skills/loadSkillsDir.ts` | Discovery, frontmatter parsing, `createSkillCommand()`, deduplication |
| `src/skills/bundledSkills.ts` | `registerBundledSkill()` / `getBundledSkills()` |
| `src/skills/bundled/index.ts` | `initBundledSkills()` -- registers all built-in skills |
| `src/skills/mcpSkillBuilders.ts` | Write-once registry to break import cycles for MCP skills |
| `src/tools/SkillTool/SkillTool.ts` | The `Skill` tool definition -- validate, permissions, call |
| `src/tools/SkillTool/prompt.ts` | Budget-aware skill listing for the system prompt |
| `src/utils/frontmatterParser.ts` | Generic YAML frontmatter extractor |
| `src/commands.ts` | `getSkillToolCommands()` / `getSlashCommandToolSkills()` |

## Comparison: shareAI-lab vs. Real Claude Code

Many open-source reimplementations (e.g., shareAI-lab forks) load the **full
markdown body of every skill** into the system prompt at startup.  This is
the simplest approach, but it scales poorly:

| Aspect | shareAI-lab approach | Real Claude Code |
|--------|---------------------|-----------------|
| System prompt size | Grows linearly with skill count | ~100 tokens per skill (name + description) |
| Token cost | Full body paid on every turn | Full body paid only on invocation turn |
| Budget awareness | None | 1% of context window allocated; descriptions truncated to fit |
| Bundled skills | Typically hardcoded strings | `registerBundledSkill()` with lazy file extraction |
| MCP integration | Not present | MCP prompt resources become skills via registry pattern |
| Conditional skills | Not present | `paths` frontmatter delays activation until matching files are touched |

The two-layer approach is the key architectural insight: **discovery is cheap,
invocation is pay-per-use**.

## Session Files

| File | Description |
|------|-------------|
| `README.md` | This file -- session overview |
| `SOURCE_ANALYSIS.md` | Deep annotated walkthrough of the real source code |
| `reimplementation.py` | Runnable Python reimplementation (~250 LOC) |

## How to Use This Session

1. Read `SOURCE_ANALYSIS.md` to understand the real implementation.
2. Run `reimplementation.py` to see the two-layer system in action.
3. Modify the example skills in the `skills/` directory the script creates and
   re-run to see how discovery and invocation interact.
