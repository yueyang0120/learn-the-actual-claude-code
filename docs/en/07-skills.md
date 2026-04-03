# Chapter 7: Skills

Skills are reusable prompt templates that extend Claude Code's capabilities without modifying its core. Implemented as markdown files with YAML frontmatter, skills use a two-layer loading architecture that keeps system prompt overhead to approximately 100 tokens per skill while deferring the full body (often 2,000+ tokens) until the model actually invokes one. This chapter connects to Chapter 6 on context compaction: the token budget consciousness that drives compaction also motivates the skill system's two-layer design.

## The Problem

Users and organizations want to teach Claude Code domain-specific workflows -- deployment procedures, code review checklists, debugging protocols. Each workflow is substantial: instructions, steps, constraints, and examples can easily reach several thousand tokens.

Loading every skill's full body into the system prompt on every turn is wasteful. In a context window where every token matters (see Chapter 6), injecting 20 skills at 2,000 tokens each would consume 40,000 tokens -- roughly 20% of the effective window -- even if none of them are used in a given turn.

The opposite extreme, hiding skills entirely until the user explicitly invokes them, means the model cannot discover or suggest relevant skills on its own. The system needs a middle path: enough information for discovery, with full content loaded on demand.

## How Claude Code Solves It

### Skill Discovery from Multiple Sources

Skills are discovered from several directories in parallel, then deduplicated:

```typescript
// src/skills/loadSkillsDir.ts -- getSkillDirCommands (simplified)
const [managedSkills, userSkills, projectSkillsNested, ...] =
  await Promise.all([
    loadSkillsFromSkillsDir(managedSkillsDir, 'policySettings'),
    loadSkillsFromSkillsDir(userSkillsDir, 'userSettings'),
    Promise.all(projectSkillsDirs.map(
      dir => loadSkillsFromSkillsDir(dir, 'projectSettings')
    )),
    // ... additional dirs, legacy commands, bundled skills
  ])
```

| Source | Path Pattern | Source Value |
|--------|-------------|-------------|
| Managed (policy) | `<managed-path>/.claude/skills/` | `policySettings` |
| User global | `~/.claude/skills/` | `userSettings` |
| Project | `.claude/skills/` (walked up to home) | `projectSettings` |
| Bundled | Compiled into binary | `bundled` |
| MCP | MCP server prompt resources | `mcp` |

Each skill lives in a named directory containing a `SKILL.md` file:

```
.claude/skills/
  deploy-helper/
    SKILL.md          <-- required
    helper-script.sh  <-- optional reference file
```

The loader only recognizes `SKILL.md` inside a directory entry. Deduplication uses `realpath` to resolve symlinks, so the same file accessible through multiple paths is loaded only once.

### YAML Frontmatter

Every `SKILL.md` begins with YAML frontmatter that declares the skill's metadata:

```markdown
---
name: Deploy Helper
description: Assists with deployment workflows
when_to_use: When the user asks about deploying or releasing
allowed-tools: Bash, Read, Write
model: sonnet
context: fork
paths: "src/deploy/**, scripts/deploy*"
arguments: [environment, version]
---

You are a deployment assistant. Help the user deploy to
the specified environment.

## Steps
1. Check the current branch and status
2. Run pre-deploy checks
3. Execute deployment for $ARGUMENTS
```

The `FrontmatterData` type includes over 20 fields: `name`, `description`, `when_to_use`, `allowed-tools`, `model`, `context` (inline or fork), `paths` (conditional activation globs), `shell`, `hooks`, `effort`, `user-invocable`, and others. The `parseSkillFrontmatterFields()` function normalizes these into a structured object, handling string-to-array coercion, legacy field names, and model resolution.

### Layer 1: System Prompt Summaries

At system prompt build time, only the skill's name, description, and `when_to_use` field are injected. The budget is 1% of the context window:

```typescript
// src/tools/SkillTool/prompt.ts
export const SKILL_BUDGET_CONTEXT_PERCENT = 0.01
export const MAX_LISTING_DESC_CHARS = 250
```

The `formatCommandsWithinBudget()` function implements graceful degradation when the budget is tight:

```typescript
export function formatCommandsWithinBudget(commands, contextWindowTokens?) {
  const budget = getCharBudget(contextWindowTokens)

  // Try full descriptions first
  const fullEntries = commands.map(
    cmd => `- ${cmd.name}: ${getCommandDescription(cmd)}`
  )
  if (totalChars(fullEntries) <= budget) return fullEntries.join('\n')

  // Bundled skills are NEVER truncated
  // Calculate max description length per non-bundled skill
  const maxDescLen = Math.floor(availableForDescs / restCommands.length)

  if (maxDescLen < 20) {
    // Extreme: non-bundled go names-only, bundled keep descriptions
    return commands.map((cmd, i) =>
      isBundled(i) ? fullEntries[i] : `- ${cmd.name}`
    ).join('\n')
  }

  // Normal: truncate non-bundled descriptions to fit
  // ...
}
```

The result in the system prompt is a compact listing like:
```
- commit: Create a git commit with a well-crafted message
- review-pr: Review a pull request for code quality and correctness
- deploy-helper: Assists with deployment workflows
```

This costs roughly 100 tokens per skill -- enough for the model to recognize when a skill is relevant, without consuming meaningful context.

### Layer 2: On-Demand Full Body

When the model decides to use a skill, it calls the `Skill` tool. Only then is the full markdown body loaded:

```typescript
// src/skills/loadSkillsDir.ts -- getPromptForCommand method
async getPromptForCommand(args, toolUseContext) {
  let finalContent = markdownContent

  // Substitute $ARGUMENTS with actual args
  finalContent = substituteArguments(
    finalContent, args, true, argumentNames
  )

  // Replace ${CLAUDE_SKILL_DIR} with the skill's directory
  if (baseDir) {
    finalContent = finalContent.replace(
      /\$\{CLAUDE_SKILL_DIR\}/g, skillDir
    )
  }

  // Execute inline shell commands -- NOT for MCP skills
  if (loadedFrom !== 'mcp') {
    finalContent = await executeShellCommandsInPrompt(
      finalContent, toolUseContext, ...
    )
  }

  return [{ type: 'text', text: finalContent }]
}
```

The `$ARGUMENTS` substitution allows skills to accept parameters. The `${CLAUDE_SKILL_DIR}` variable lets skills reference files relative to their own directory. Inline shell commands (backtick-prefixed with `!`) are executed at load time, but this capability is disabled for MCP-sourced skills as a security boundary.

### SkillTool Invocation

The `Skill` tool (`src/tools/SkillTool/SkillTool.ts`) bridges the model and the skill system. Its input schema is minimal:

```typescript
z.object({
  skill: z.string().describe('The skill name'),
  args: z.string().optional().describe('Optional arguments'),
})
```

Validation resolves the skill name (stripping leading slashes), looks it up in the combined command list, and checks that it is a prompt-type skill that allows model invocation. Permission checking follows the standard deny/allow/ask pipeline, with one shortcut: skills that have only safe properties (no hooks, no shell commands, no fork context) are auto-allowed.

Execution branches on the `context` frontmatter field:

**Inline execution** (default): The skill's prompt expands into the current conversation as new user messages. A `contextModifier` adjusts the tool permission context and optionally overrides the model or effort level.

**Forked execution** (`context: fork`): The skill runs in an isolated sub-agent (see Chapter 8) with its own token budget. This is appropriate for long-running or tool-heavy workflows that should not pollute the main conversation.

### Conditional Skills

Skills with a `paths` frontmatter field are not loaded at startup. They are stored in a `conditionalSkills` map and activated only when the model touches a file matching the glob pattern:

```typescript
for (const skill of deduplicatedSkills) {
  if (skill.paths && skill.paths.length > 0
      && !activatedConditionalSkillNames.has(skill.name)) {
    conditionalSkills.set(skill.name, skill)  // stored, not returned
  } else {
    unconditionalSkills.push(skill)           // returned immediately
  }
}
```

This prevents domain-specific skills (e.g., a Kubernetes deployment skill triggered by `paths: "k8s/**"`) from consuming Layer 1 budget until they become relevant.

### MCP Prompts as Skills

When the `MCP_SKILLS` feature flag is enabled, MCP server prompt resources are converted into skill commands. A write-once registry (`mcpSkillBuilders.ts`) breaks the dependency cycle between the MCP client and the skill loader:

```typescript
// src/skills/mcpSkillBuilders.ts
let builders: MCPSkillBuilders | null = null

export function registerMCPSkillBuilders(b: MCPSkillBuilders): void {
  builders = b
}
```

MCP skills merge into the command list alongside local and bundled skills, deduplicated by name. The critical security boundary: MCP skills cannot execute inline shell commands, since their content originates from remote servers.

## Key Design Decisions

**1% budget for Layer 1.** This number is small enough to be negligible in most sessions but large enough to list dozens of skills with descriptions. Bundled skills are exempt from truncation, ensuring core functionality always has full descriptions.

**Bundled skills are never truncated.** The graceful degradation strategy sacrifices user-defined skill descriptions before touching bundled ones. In the extreme case, user skills appear as names only while bundled skills retain full descriptions.

**Fork vs. inline is a skill-level decision.** The skill author declares `context: fork` in frontmatter when the workflow is heavy enough to warrant isolation. The system does not guess. This keeps the common case (inline, zero overhead) fast.

**MCP skills cannot run shell.** Since MCP prompt content comes from potentially untrusted remote servers, the `executeShellCommandsInPrompt` function is gated on `loadedFrom !== 'mcp'`. This is a defense-in-depth measure that prevents prompt injection from escalating to code execution.

## In Practice

When Claude Code starts, it discovers skills from all configured directories, parses their frontmatter, and injects a one-line summary of each into the system prompt. The model sees these summaries on every turn and can decide to invoke a skill when relevant.

If a user types "deploy to staging," the model recognizes the `deploy-helper` skill from its Layer 1 listing and calls the `Skill` tool. The full markdown body loads, `$ARGUMENTS` is replaced with "staging," and the instructions expand into the conversation. If the skill specifies `context: fork`, a sub-agent handles the workflow in isolation and returns a summary.

Users can add custom skills by creating a directory under `.claude/skills/` with a `SKILL.md` file. Enterprise administrators can distribute managed skills through the policy path. The two-layer architecture ensures that even a large skill library does not degrade model performance through system prompt bloat.

## Summary

- Skills are markdown files with YAML frontmatter, discovered from managed, user, project, bundled, and MCP sources in parallel.
- A two-layer loading architecture keeps Layer 1 (system prompt) at roughly 100 tokens per skill while deferring the full body to Layer 2 (on-demand invocation).
- The `formatCommandsWithinBudget()` function enforces a 1% context-window budget with graceful degradation: truncated descriptions, then names-only, with bundled skills always exempt.
- Skills execute inline by default or in a forked sub-agent when `context: fork` is specified; MCP-sourced skills are prohibited from executing inline shell commands.
- Conditional skills with `paths` globs remain dormant until the model touches a matching file, avoiding unnecessary Layer 1 budget consumption.
