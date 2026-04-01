# Source Analysis -- The Skill System: Two-Layer Loading

## Table of Contents

1. [Skill Discovery from Multiple Directories](#1-skill-discovery-from-multiple-directories)
2. [YAML Frontmatter Parsing](#2-yaml-frontmatter-parsing)
3. [Two-Layer Token Optimization](#3-two-layer-token-optimization)
4. [SkillTool Invocation](#4-skilltool-invocation)
5. [Bundled Skills](#5-bundled-skills)
6. [MCP Prompt Resources Becoming Skills](#6-mcp-prompt-resources-becoming-skills)

---

## 1. Skill Discovery from Multiple Directories

Skills are discovered from several sources, loaded in parallel, then
deduplicated.  The entry point is `getSkillDirCommands()` in
`src/skills/loadSkillsDir.ts`:

```typescript
// src/skills/loadSkillsDir.ts -- getSkillDirCommands (simplified)
export const getSkillDirCommands = memoize(
  async (cwd: string): Promise<Command[]> => {
    const userSkillsDir = join(getClaudeConfigHomeDir(), 'skills')    // ~/.claude/skills/
    const managedSkillsDir = join(getManagedFilePath(), '.claude', 'skills') // policy-managed
    const projectSkillsDirs = getProjectDirsUpToHome('skills', cwd)   // .claude/skills/ up to ~

    // Load ALL sources in parallel -- they read different directories, no contention
    const [managedSkills, userSkills, projectSkillsNested, additionalSkillsNested, legacyCommands] =
      await Promise.all([
        loadSkillsFromSkillsDir(managedSkillsDir, 'policySettings'),
        loadSkillsFromSkillsDir(userSkillsDir, 'userSettings'),
        Promise.all(projectSkillsDirs.map(dir => loadSkillsFromSkillsDir(dir, 'projectSettings'))),
        Promise.all(additionalDirs.map(dir => loadSkillsFromSkillsDir(...))),
        loadSkillsFromCommandsDir(cwd),  // legacy /commands/ format
      ])

    // Flatten, combine, deduplicate by resolved file identity (handles symlinks)
    // ...
  },
)
```

### Directory Priority and Sources

| Source | Path Pattern | `source` value | `loadedFrom` |
|--------|-------------|----------------|--------------|
| Managed (policy) | `<managed-path>/.claude/skills/` | `'policySettings'` | `'skills'` |
| User global | `~/.claude/skills/` | `'userSettings'` | `'skills'` |
| Project | `.claude/skills/` (walked up to home) | `'projectSettings'` | `'skills'` |
| Additional dirs | `--add-dir <path>/.claude/skills/` | `'projectSettings'` | `'skills'` |
| Legacy commands | `.claude/commands/` | varies | `'commands_DEPRECATED'` |
| Bundled | Compiled into binary | `'bundled'` | `'bundled'` |
| MCP | MCP server prompt resources | varies | `'mcp'` |

### Skill Directory Format

Skills live in a specific directory structure:

```
.claude/skills/
  my-skill/
    SKILL.md          <-- required file name
    helper-script.sh  <-- optional reference files
```

The loader only recognizes `SKILL.md` inside a named directory:

```typescript
// src/skills/loadSkillsDir.ts -- loadSkillsFromSkillsDir (simplified)
async function loadSkillsFromSkillsDir(basePath, source) {
  const entries = await fs.readdir(basePath)
  for (const entry of entries) {
    // ONLY directory format is supported in /skills/
    if (!entry.isDirectory() && !entry.isSymbolicLink()) continue

    const skillFilePath = join(basePath, entry.name, 'SKILL.md')
    const content = await fs.readFile(skillFilePath, { encoding: 'utf-8' })

    const { frontmatter, content: markdownContent } = parseFrontmatter(content, skillFilePath)
    const skillName = entry.name  // directory name IS the skill name
    // ...
  }
}
```

### Deduplication by File Identity

When the same file is accessible via multiple paths (symlinks, overlapping
parent directories), Claude Code deduplicates using `realpath`:

```typescript
// src/skills/loadSkillsDir.ts
async function getFileIdentity(filePath: string): Promise<string | null> {
  try {
    return await realpath(filePath)  // resolve symlinks to canonical path
  } catch {
    return null
  }
}
```

Pre-computed in parallel, then synchronous first-wins dedup:

```typescript
const fileIds = await Promise.all(
  allSkillsWithPaths.map(({ skill, filePath }) =>
    skill.type === 'prompt' ? getFileIdentity(filePath) : Promise.resolve(null)
  )
)

const seenFileIds = new Map()
for (let i = 0; i < allSkillsWithPaths.length; i++) {
  const fileId = fileIds[i]
  if (seenFileIds.has(fileId)) continue  // duplicate -- skip
  seenFileIds.set(fileId, skill.source)
  deduplicatedSkills.push(skill)
}
```

### Conditional Skills (Path-Filtered)

Skills with a `paths` frontmatter field are not loaded at startup. Instead,
they are stored in a `conditionalSkills` map and only activated when the model
touches a file matching the glob pattern:

```typescript
// Skills with paths frontmatter are held back
for (const skill of deduplicatedSkills) {
  if (skill.paths && skill.paths.length > 0 && !activatedConditionalSkillNames.has(skill.name)) {
    conditionalSkills.set(skill.name, skill)  // stored, not returned
  } else {
    unconditionalSkills.push(skill)           // returned immediately
  }
}
```

---

## 2. YAML Frontmatter Parsing

Every `SKILL.md` file starts with YAML frontmatter between `---` delimiters.
The parser (`src/utils/frontmatterParser.ts`) extracts this into a typed object:

### Example SKILL.md

```markdown
---
name: Deploy Helper
description: Assists with deployment workflows
when_to_use: When the user asks about deploying or releasing
allowed-tools: Bash, Read, Write
model: sonnet
context: fork
shell: bash
paths: "src/deploy/**, scripts/deploy*"
arguments: [environment, version]
argument-hint: "<environment> [version]"
user-invocable: true
effort: high
---

# Deploy Helper

You are a deployment assistant. Help the user deploy to the
specified environment.

## Steps
1. Check the current branch and status
2. Run pre-deploy checks
3. Execute deployment for $ARGUMENTS
```

### Frontmatter Field Types

From `src/utils/frontmatterParser.ts`:

```typescript
export type FrontmatterData = {
  'allowed-tools'?: string | string[] | null
  description?: string | null
  when_to_use?: string | null
  version?: string | null
  model?: string | null
  'user-invocable'?: string | null
  hooks?: HooksSettings | null
  effort?: string | null
  context?: 'inline' | 'fork' | null
  agent?: string | null
  paths?: string | string[] | null
  shell?: string | null
  'argument-hint'?: string | null
  [key: string]: unknown
}
```

### Parsing Shared Fields

`parseSkillFrontmatterFields()` in `loadSkillsDir.ts` processes all fields
into a normalized structure:

```typescript
// src/skills/loadSkillsDir.ts -- parseSkillFrontmatterFields (simplified)
export function parseSkillFrontmatterFields(frontmatter, markdownContent, resolvedName) {
  const description = coerceDescriptionToString(frontmatter.description, resolvedName)
    ?? extractDescriptionFromMarkdown(markdownContent, 'Skill')

  return {
    displayName: frontmatter.name != null ? String(frontmatter.name) : undefined,
    description,
    allowedTools: parseSlashCommandToolsFromFrontmatter(frontmatter['allowed-tools']),
    argumentNames: parseArgumentNames(frontmatter.arguments),
    whenToUse: frontmatter.when_to_use,
    model: frontmatter.model === 'inherit' ? undefined
         : frontmatter.model ? parseUserSpecifiedModel(frontmatter.model) : undefined,
    executionContext: frontmatter.context === 'fork' ? 'fork' : undefined,
    effort: parseEffortValue(frontmatter.effort),
    shell: parseShellFrontmatter(frontmatter.shell, resolvedName),
    hooks: parseHooksFromFrontmatter(frontmatter, resolvedName),
    userInvocable: frontmatter['user-invocable'] === undefined
      ? true : parseBooleanFrontmatter(frontmatter['user-invocable']),
    // ...
  }
}
```

---

## 3. Two-Layer Token Optimization

This is the core architectural insight of the skill system.

### Layer 1: System Prompt Injection (~100 tokens per skill)

At system prompt build time, only **name + description + whenToUse** are
injected.  The budget is 1% of the context window:

```typescript
// src/tools/SkillTool/prompt.ts
export const SKILL_BUDGET_CONTEXT_PERCENT = 0.01
export const CHARS_PER_TOKEN = 4
export const DEFAULT_CHAR_BUDGET = 8_000  // Fallback: 1% of 200k * 4

// Per-entry hard cap -- listing is for discovery only
export const MAX_LISTING_DESC_CHARS = 250
```

The `formatCommandsWithinBudget()` function implements a graceful degradation
strategy:

```typescript
// src/tools/SkillTool/prompt.ts -- formatCommandsWithinBudget (simplified)
export function formatCommandsWithinBudget(commands, contextWindowTokens?) {
  const budget = getCharBudget(contextWindowTokens)

  // Try full descriptions first
  const fullEntries = commands.map(cmd => `- ${cmd.name}: ${getCommandDescription(cmd)}`)
  if (totalChars(fullEntries) <= budget) return fullEntries.join('\n')

  // Bundled skills are NEVER truncated
  // Calculate remaining budget for non-bundled skills
  const remainingBudget = budget - bundledCharsTotal

  // Calculate max description length per non-bundled skill
  const maxDescLen = Math.floor(availableForDescs / restCommands.length)

  if (maxDescLen < 20) {
    // Extreme case: non-bundled go names-only, bundled keep descriptions
    return commands.map((cmd, i) =>
      isBundled(i) ? fullEntries[i] : `- ${cmd.name}`
    ).join('\n')
  }

  // Normal case: truncate non-bundled descriptions to fit
  return commands.map((cmd, i) => {
    if (isBundled(i)) return fullEntries[i]   // full description
    return `- ${cmd.name}: ${truncate(description, maxDescLen)}`
  }).join('\n')
}
```

The resulting listing looks like this in the system prompt:

```
- commit: Create a git commit with a well-crafted message
- review-pr: Review a pull request for code quality and correctness
- my-deploy: Assists with deployment workflows - When the user asks about deploying
```

### Layer 2: On-Demand Full Body Loading

When the model decides to use a skill, it calls the `Skill` tool.  Only then
is the full markdown body loaded:

```typescript
// src/skills/loadSkillsDir.ts -- createSkillCommand (the getPromptForCommand method)
async getPromptForCommand(args, toolUseContext) {
  // THIS is where the full markdown body is injected -- only on invocation
  let finalContent = baseDir
    ? `Base directory for this skill: ${baseDir}\n\n${markdownContent}`
    : markdownContent

  // Substitute $ARGUMENTS with the actual args
  finalContent = substituteArguments(finalContent, args, true, argumentNames)

  // Replace ${CLAUDE_SKILL_DIR} with the skill's own directory
  if (baseDir) {
    finalContent = finalContent.replace(/\$\{CLAUDE_SKILL_DIR\}/g, skillDir)
  }

  // Execute inline shell commands (!`cmd`) -- NOT for MCP skills (security)
  if (loadedFrom !== 'mcp') {
    finalContent = await executeShellCommandsInPrompt(finalContent, toolUseContext, ...)
  }

  return [{ type: 'text', text: finalContent }]
}
```

### Token Estimation for Discovery Layer

The token count for each skill's discovery footprint is estimated from
frontmatter only:

```typescript
// src/skills/loadSkillsDir.ts
export function estimateSkillFrontmatterTokens(skill: Command): number {
  const frontmatterText = [skill.name, skill.description, skill.whenToUse]
    .filter(Boolean)
    .join(' ')
  return roughTokenCountEstimation(frontmatterText)
}
```

---

## 4. SkillTool Invocation

The `Skill` tool (`src/tools/SkillTool/SkillTool.ts`) is the bridge between
the model and the skill system.

### Input Schema

```typescript
z.object({
  skill: z.string().describe('The skill name. E.g., "commit", "review-pr", or "pdf"'),
  args: z.string().optional().describe('Optional arguments for the skill'),
})
```

### Validation Flow

```typescript
async validateInput({ skill }, context) {
  const commandName = skill.trim().replace(/^\//, '')  // strip leading slash

  const commands = await getAllCommands(context)
  const foundCommand = findCommand(commandName, commands)

  if (!foundCommand) return { result: false, message: `Unknown skill: ${commandName}` }
  if (foundCommand.disableModelInvocation) return { result: false, ... }
  if (foundCommand.type !== 'prompt') return { result: false, ... }

  return { result: true }
}
```

### Two Execution Paths

The `call()` method branches based on the skill's `context` field:

#### Path A: Forked Execution (`context: 'fork'`)

Runs the skill in an isolated sub-agent with its own token budget:

```typescript
// src/tools/SkillTool/SkillTool.ts
if (command?.type === 'prompt' && command.context === 'fork') {
  return executeForkedSkill(command, commandName, args, context, canUseTool, parentMessage, onProgress)
}
```

Inside `executeForkedSkill()`:

```typescript
async function executeForkedSkill(command, commandName, args, context, ...) {
  const agentId = createAgentId()
  const { modifiedGetAppState, baseAgent, promptMessages, skillContent } =
    await prepareForkedCommandContext(command, args || '', context)

  // Run a full sub-agent loop
  for await (const message of runAgent({
    agentDefinition: command.effort ? { ...baseAgent, effort: command.effort } : baseAgent,
    promptMessages,
    toolUseContext: { ...context, getAppState: modifiedGetAppState },
    canUseTool,
    model: command.model,
    availableTools: context.options.tools,
    override: { agentId },
  })) {
    agentMessages.push(message)
    // Report progress for tool uses
  }

  return { data: { success: true, commandName, status: 'forked', agentId, result: resultText } }
}
```

#### Path B: Inline Execution (default)

Expands the skill's prompt into the current conversation as new user messages:

```typescript
// src/tools/SkillTool/SkillTool.ts -- call() inline path
const processedCommand = await processPromptSlashCommand(commandName, args || '', commands, context)

// The skill content becomes new user messages in the conversation
const newMessages = tagMessagesWithToolUseID(processedCommand.messages, toolUseID)

return {
  data: { success: true, commandName, allowedTools, model },
  newMessages,           // injected into conversation
  contextModifier(ctx) { // modifies tool permissions, model, effort
    // Add skill's allowed-tools to permission context
    // Override model if skill specifies one
    // Override effort if skill specifies one
  },
}
```

### Permission Checking

Skills go through a permission system with deny/allow rules:

```typescript
async checkPermissions({ skill, args }, context) {
  // 1. Check deny rules first
  for (const [ruleContent, rule] of denyRules.entries()) {
    if (ruleMatches(ruleContent)) return { behavior: 'deny', ... }
  }

  // 2. Check allow rules
  for (const [ruleContent, rule] of allowRules.entries()) {
    if (ruleMatches(ruleContent)) return { behavior: 'allow', ... }
  }

  // 3. Auto-allow skills with only safe properties (no hooks, no shell, etc.)
  if (skillHasOnlySafeProperties(commandObj)) return { behavior: 'allow', ... }

  // 4. Default: ask user
  return { behavior: 'ask', message: `Execute skill: ${commandName}`, ... }
}
```

---

## 5. Bundled Skills

Bundled skills are compiled into the CLI binary and registered at startup.

### Registration Pattern

```typescript
// src/skills/bundledSkills.ts
export function registerBundledSkill(definition: BundledSkillDefinition): void {
  const command: Command = {
    type: 'prompt',
    name: definition.name,
    description: definition.description,
    whenToUse: definition.whenToUse,
    source: 'bundled',
    loadedFrom: 'bundled',
    contentLength: 0,  // not applicable -- body is generated dynamically
    getPromptForCommand: definition.getPromptForCommand,
    // ...
  }
  bundledSkills.push(command)
}
```

### List of Bundled Skills (as of source snapshot)

From `src/skills/bundled/index.ts`:

| Skill | Description |
|-------|-------------|
| `update-config` | Update Claude Code configuration |
| `keybindings` | Show/manage keyboard bindings |
| `verify` | Verify code changes are correct |
| `debug` | Debug issues with context |
| `lorem-ipsum` | Generate placeholder text |
| `skillify` | Convert a prompt into a reusable skill |
| `remember` | Review and organize auto-memory entries |
| `simplify` | Simplify complex code |
| `batch` | Run batch operations |
| `stuck` | Help when stuck on a problem |
| `dream` | (experimental, feature-flagged) |
| `hunter` | (experimental, feature-flagged) |
| `loop` | (experimental, feature-flagged) |
| `claude-api` | (experimental, feature-flagged) |
| `claude-in-chrome` | (conditional on Chrome setup) |

### Example: The `/remember` Bundled Skill

```typescript
// src/skills/bundled/remember.ts (simplified)
registerBundledSkill({
  name: 'remember',
  description: 'Review auto-memory entries and propose promotions to CLAUDE.md...',
  whenToUse: 'Use when the user wants to review, organize, or promote their auto-memory entries.',
  userInvocable: true,
  isEnabled: () => isAutoMemoryEnabled(),
  async getPromptForCommand(args) {
    let prompt = SKILL_PROMPT  // large markdown string with instructions
    if (args) {
      prompt += `\n## Additional context from user\n\n${args}`
    }
    return [{ type: 'text', text: prompt }]
  },
})
```

### Bundled Skill File Extraction

Bundled skills can include reference files that are lazily extracted to disk:

```typescript
// src/skills/bundledSkills.ts
if (files && Object.keys(files).length > 0) {
  skillRoot = getBundledSkillExtractDir(definition.name)
  let extractionPromise  // memoized -- extract once per process
  getPromptForCommand = async (args, ctx) => {
    extractionPromise ??= extractBundledSkillFiles(definition.name, files)
    const extractedDir = await extractionPromise
    const blocks = await inner(args, ctx)
    if (extractedDir === null) return blocks
    return prependBaseDir(blocks, extractedDir)  // "Base directory for this skill: /path"
  }
}
```

---

## 6. MCP Prompt Resources Becoming Skills

MCP (Model Context Protocol) servers can expose prompt resources.  When the
`MCP_SKILLS` feature flag is enabled, these prompts are converted into skill
commands.

### The Cycle-Breaking Registry

The MCP client needs functions from `loadSkillsDir.ts`, but importing directly
would create a dependency cycle (`client.ts -> mcpSkills.ts -> loadSkillsDir.ts
-> ... -> client.ts`).  The solution is a write-once registry:

```typescript
// src/skills/mcpSkillBuilders.ts
export type MCPSkillBuilders = {
  createSkillCommand: typeof createSkillCommand
  parseSkillFrontmatterFields: typeof parseSkillFrontmatterFields
}

let builders: MCPSkillBuilders | null = null

export function registerMCPSkillBuilders(b: MCPSkillBuilders): void {
  builders = b
}

export function getMCPSkillBuilders(): MCPSkillBuilders {
  if (!builders) {
    throw new Error('MCP skill builders not registered -- loadSkillsDir.ts has not been evaluated yet')
  }
  return builders
}
```

Registration happens at module init in `loadSkillsDir.ts`:

```typescript
// src/skills/loadSkillsDir.ts (bottom of file)
registerMCPSkillBuilders({
  createSkillCommand,
  parseSkillFrontmatterFields,
})
```

### MCP Skill Discovery Flow

When an MCP server connects and lists its prompts:

```typescript
// src/services/mcp/client.ts (simplified)
const [tools, mcpCommands, mcpSkills, resources] = await Promise.all([
  fetchToolsForClient(client),
  fetchPromptsForClient(client),
  fetchMcpSkillsForClient!(client),  // converts prompt resources to skills
  fetchResourcesForClient(client),
])
const commands = [...mcpCommands, ...mcpSkills]  // merged into command list
```

MCP skills are stored in `AppState.mcp.commands` and merged with local
commands when the `Skill` tool looks up available skills:

```typescript
// src/tools/SkillTool/SkillTool.ts
async function getAllCommands(context) {
  const mcpSkills = context.getAppState().mcp.commands
    .filter(cmd => cmd.type === 'prompt' && cmd.loadedFrom === 'mcp')
  const localCommands = await getCommands(getProjectRoot())
  return uniqBy([...localCommands, ...mcpSkills], 'name')
}
```

### Security: MCP Skills Cannot Execute Shell

A critical security boundary: MCP skills are remote/untrusted, so inline
shell commands (`!`cmd``) are never executed from their markdown body:

```typescript
// src/skills/loadSkillsDir.ts -- inside getPromptForCommand
if (loadedFrom !== 'mcp') {
  finalContent = await executeShellCommandsInPrompt(finalContent, ...)
}
```

---

## Architecture Diagram

```
Startup
  |
  +--> loadSkillsDir.ts
  |     |
  |     +--> ~/.claude/skills/          \
  |     +--> .claude/skills/             |-- discover SKILL.md files
  |     +--> managed policy skills/      |   parse YAML frontmatter
  |     +--> legacy /commands/          /    extract name+description+whenToUse
  |
  +--> bundledSkills.ts
  |     +--> initBundledSkills()        --> register /verify, /remember, etc.
  |
  +--> MCP clients
        +--> fetchMcpSkillsForClient()  --> convert prompt resources to skills

System Prompt Build (every turn)
  |
  +--> formatCommandsWithinBudget()
        |
        +--> Budget = 1% of context window
        +--> For each skill: "- name: description" (max 250 chars)
        +--> Bundled skills: never truncated
        +--> User skills: truncated to fit budget, or names-only in extreme cases
        |
        Result: ~100 tokens per skill in system prompt

Skill Invocation (on demand)
  |
  +--> Model calls Skill tool with { skill: "deploy", args: "prod" }
  |
  +--> SkillTool.validateInput()  --> find command, check type & permissions
  +--> SkillTool.checkPermissions() --> deny/allow rules, safe-property auto-allow
  +--> SkillTool.call()
        |
        +--> context === 'fork'?
        |     YES --> executeForkedSkill() --> runAgent() sub-agent with full body
        |     NO  --> processPromptSlashCommand()
        |              |
        |              +--> getPromptForCommand(args)
        |              |     |
        |              |     +--> Load full markdown body  <-- LAYER 2
        |              |     +--> Substitute $ARGUMENTS
        |              |     +--> Replace ${CLAUDE_SKILL_DIR}
        |              |     +--> Execute !`shell` commands (not for MCP)
        |              |
        |              +--> Return as newMessages injected into conversation
        |
        +--> Return contextModifier (allowed-tools, model override, effort)
```
