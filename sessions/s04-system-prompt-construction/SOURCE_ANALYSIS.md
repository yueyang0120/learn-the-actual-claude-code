# Source Analysis -- System Prompt Construction

## 1. The Two-Layer Section Architecture

The system prompt is not a single string. It is an **array of strings**
(`string[]`) returned by `getSystemPrompt()`. Each element is either a static
section or a registry-managed dynamic section. The dynamic sections use a
purpose-built memoization system defined in `src/constants/systemPromptSections.ts`.

### 1.1 `systemPromptSection()` -- Cached Sections

```typescript
// src/constants/systemPromptSections.ts:20-25
export function systemPromptSection(
  name: string,
  compute: ComputeFn,
): SystemPromptSection {
  return { name, compute, cacheBreak: false }
}
```

A cached section is computed **once** and then memoized for the lifetime of the
session (until `/clear` or `/compact` resets the cache). This is critical for
prompt caching: if the value does not change between turns, the API can reuse
the cached prefix and avoid re-processing thousands of tokens.

Examples of cached sections:
- `'memory'` -- the memory prompt from `loadMemoryPrompt()`
- `'env_info_simple'` -- OS, CWD, git status, model identity
- `'session_guidance'` -- agent tool usage, skill discovery
- `'language'` -- user's preferred language
- `'output_style'` -- configurable output style

### 1.2 `DANGEROUS_uncachedSystemPromptSection()` -- Volatile Sections

```typescript
// src/constants/systemPromptSections.ts:32-38
export function DANGEROUS_uncachedSystemPromptSection(
  name: string,
  compute: ComputeFn,
  _reason: string, // documentation-only: why cache-breaking is needed
): SystemPromptSection {
  return { name, compute, cacheBreak: true }
}
```

The `DANGEROUS_` prefix is a naming convention that signals: **this will break
the prompt cache when the value changes**. The `_reason` parameter is purely
documentary, enforcing that every cache-busting section has a written
justification.

Currently the only uncached section in the main prompt is `'mcp_instructions'`:

```typescript
// src/constants/prompts.ts:513-520
DANGEROUS_uncachedSystemPromptSection(
  'mcp_instructions',
  () =>
    isMcpInstructionsDeltaEnabled()
      ? null
      : getMcpInstructionsSection(mcpClients),
  'MCP servers connect/disconnect between turns',
),
```

MCP servers can connect or disconnect between turns, so their instructions must
be recomputed. When the delta feature flag is on, instructions are delivered via
per-turn attachments instead, avoiding the cache break entirely.

### 1.3 Resolution

```typescript
// src/constants/systemPromptSections.ts:43-58
export async function resolveSystemPromptSections(
  sections: SystemPromptSection[],
): Promise<(string | null)[]> {
  const cache = getSystemPromptSectionCache()

  return Promise.all(
    sections.map(async s => {
      if (!s.cacheBreak && cache.has(s.name)) {
        return cache.get(s.name) ?? null
      }
      const value = await s.compute()
      setSystemPromptSectionCacheEntry(s.name, value)
      return value
    }),
  )
}
```

All sections are resolved in parallel via `Promise.all`. Cached sections skip
their compute function on subsequent turns. The cache is a session-scoped Map
stored in bootstrap state.

---

## 2. How the Final Prompt Array is Assembled

`getSystemPrompt()` in `src/constants/prompts.ts:444-577` returns a `string[]`
with this structure:

```
[ STATIC SECTIONS ]
  1. Identity intro          -- getSimpleIntroSection()
  2. System rules            -- getSimpleSystemSection()
  3. Doing tasks guidance     -- getSimpleDoingTasksSection()
  4. Executing with care     -- getActionsSection()
  5. Using your tools        -- getUsingYourToolsSection(enabledTools)
  6. Tone and style          -- getSimpleToneAndStyleSection()
  7. Output efficiency       -- getOutputEfficiencySection()

[ BOUNDARY MARKER ]
  8. '__SYSTEM_PROMPT_DYNAMIC_BOUNDARY__'

[ DYNAMIC SECTIONS ]  -- via resolveSystemPromptSections()
  9.  session_guidance       -- tool/agent-specific guidance
  10. memory                 -- MEMORY.md + behavioral instructions
  11. ant_model_override     -- internal override (null for external users)
  12. env_info_simple        -- OS, CWD, git, model, cutoff
  13. language               -- language preference
  14. output_style           -- configurable style
  15. mcp_instructions       -- MCP server instructions (UNCACHED)
  16. scratchpad             -- scratchpad directory path
  17. frc                    -- function result clearing
  18. summarize_tool_results -- reminder to save important data
  19. (optional) numeric_length_anchors, token_budget, brief
```

### 2.1 The Dynamic Boundary

```typescript
// src/constants/prompts.ts:106-115
export const SYSTEM_PROMPT_DYNAMIC_BOUNDARY =
  '__SYSTEM_PROMPT_DYNAMIC_BOUNDARY__'
```

This sentinel string splits the array into two halves:

- **Before the boundary**: static content that is identical across all users and
  sessions using the same build. Gets `cacheScope: 'global'` -- the API can
  reuse a single cached representation across all organizations.
- **After the boundary**: per-user/per-session content (memory, environment,
  MCP instructions). Gets `cacheScope: null` or `'org'`.

The split happens in `splitSysPromptPrefix()` in `src/utils/api.ts:321`:

```typescript
// src/utils/api.ts:362-396
if (useGlobalCacheFeature) {
  const boundaryIndex = systemPrompt.findIndex(
    s => s === SYSTEM_PROMPT_DYNAMIC_BOUNDARY,
  )
  if (boundaryIndex !== -1) {
    // ... static blocks get cacheScope: 'global'
    // ... dynamic blocks get cacheScope: null
  }
}
```

This architecture means that the ~8-10 KB of static instructions are cached
once globally, and only the ~2-5 KB of dynamic content is transmitted fresh
each turn.

---

## 3. CLAUDE.md Hierarchy: Managed -> User -> Project -> Local

The CLAUDE.md loading system is defined in `src/utils/claudemd.ts`. The file
header documents the full hierarchy:

```typescript
// src/utils/claudemd.ts:1-26
// Files are loaded in the following order:
//
// 1. Managed memory (/etc/claude-code/CLAUDE.md)     -- admin policy
// 2. User memory (~/.claude/CLAUDE.md)                -- private global
// 3. Project memory (CLAUDE.md, .claude/CLAUDE.md,    -- checked into repo
//    .claude/rules/*.md in project roots)
// 4. Local memory (CLAUDE.local.md in project roots)  -- private per-project
//
// Files are loaded in reverse order of priority, i.e. the latest files
// are highest priority with the model paying more attention to them.
```

### 3.1 Discovery Walk

The `getMemoryFiles()` function in `src/utils/claudemd.ts:790` performs:

1. **Managed**: reads `/etc/claude-code/CLAUDE.md` and `/etc/claude-code/.claude/rules/*.md`
2. **User**: reads `~/.claude/CLAUDE.md` and `~/.claude/rules/*.md`
3. **Project walk**: starting from the filesystem root, walks **downward** toward
   CWD. At each directory level, reads:
   - `<dir>/CLAUDE.md` (Project type)
   - `<dir>/.claude/CLAUDE.md` (Project type)
   - `<dir>/.claude/rules/*.md` (Project type, recursive into subdirs)
   - `<dir>/CLAUDE.local.md` (Local type)
4. **AutoMem entrypoint**: if auto-memory is enabled, reads `MEMORY.md` from the
   auto-memory directory
5. **TeamMem entrypoint**: if team memory is enabled, reads the shared team
   `MEMORY.md`

### 3.2 Formatting for the API

The `getClaudeMds()` function formats all loaded files into a single string
block with type annotations:

```typescript
// src/utils/claudemd.ts:1153-1194
export const getClaudeMds = (memoryFiles, filter) => {
  // Each file gets a header like:
  // Contents of /path/to/CLAUDE.md (project instructions, checked into the codebase):
  // or
  // Contents of ~/.claude/CLAUDE.md (user's private global instructions):
  //
  // Prefixed with MEMORY_INSTRUCTION_PROMPT:
  // "Codebase and user instructions are shown below.
  //  Be sure to adhere to these instructions.
  //  IMPORTANT: These instructions OVERRIDE any default behavior..."
}
```

### 3.3 Conditional Rules via Frontmatter

Files in `.claude/rules/` can have frontmatter with `paths:` globs:

```markdown
---
paths:
  - src/components/**
  - src/pages/**
---
Always use React functional components with hooks.
```

These conditional rules are only injected when the model is working on a file
that matches the glob pattern. The matching uses the `ignore` library (same
as `.gitignore` pattern matching).

### 3.4 @include Directives

CLAUDE.md files can include other files with `@path` syntax:

```markdown
See coding standards: @./standards/code-style.md
Common patterns: @~/shared/patterns.md
```

Includes are resolved recursively up to depth 5, with cycle detection via a
`processedPaths` Set. Only text file extensions are allowed (binary files like
images and PDFs are skipped).

---

## 4. Memory Attachment from MEMORY.md

### 4.1 The Memory Prompt

The memory system is loaded via `loadMemoryPrompt()` in `src/memdir/memdir.ts:419`.
It returns a string containing behavioral instructions plus the `MEMORY.md`
content:

```typescript
// src/memdir/memdir.ts:419-507
export async function loadMemoryPrompt(): Promise<string | null> {
  const autoEnabled = isAutoMemoryEnabled()
  // ...
  if (autoEnabled) {
    const autoDir = getAutoMemPath()
    await ensureMemoryDirExists(autoDir)
    return buildMemoryLines('auto memory', autoDir, ...).join('\n')
  }
  return null
}
```

### 4.2 Truncation Caps

MEMORY.md is subject to strict size limits:

```typescript
// src/memdir/memdir.ts:35-38
export const MAX_ENTRYPOINT_LINES = 200
export const MAX_ENTRYPOINT_BYTES = 25_000  // ~25 KB
```

The `truncateEntrypointContent()` function enforces both caps:

```typescript
// src/memdir/memdir.ts:57-103
export function truncateEntrypointContent(raw: string): EntrypointTruncation {
  // 1. Check line count against MAX_ENTRYPOINT_LINES (200)
  // 2. Check byte count against MAX_ENTRYPOINT_BYTES (25,000)
  // 3. Line-truncate first (natural boundary)
  // 4. Then byte-truncate at the last newline before the cap
  // 5. Append WARNING with the reason for truncation
}
```

When truncated, a warning is appended:
```
> WARNING: MEMORY.md is 250 lines (limit: 200). Only part of it was loaded.
> Keep index entries to one line under ~200 chars; move detail into topic files.
```

### 4.3 Memory Directory Path

The auto-memory directory defaults to:
```
~/.claude/projects/<sanitized-project-root>/memory/
```

This can be overridden via:
1. `CLAUDE_COWORK_MEMORY_PATH_OVERRIDE` env var (used by Cowork SDK)
2. `autoMemoryDirectory` in settings.json (user/local/policy settings only --
   project settings are excluded for security)

### 4.4 How Memory Reaches the System Prompt

Memory is registered as a cached dynamic section:

```typescript
// src/constants/prompts.ts:495-496
systemPromptSection('memory', () => loadMemoryPrompt()),
```

Because it uses `systemPromptSection` (not `DANGEROUS_uncached`), the memory
prompt is computed once at session start and reused on every subsequent turn
without breaking the prompt cache.

---

## 5. Tool Prompt Injection

Tool prompts do **not** appear in the system prompt text array. Instead, each
tool's `.prompt()` method generates a description that becomes the `description`
field in the Anthropic API's tool schema:

```typescript
// src/Tool.ts:518-523
prompt(options: {
  getToolPermissionContext: () => Promise<ToolPermissionContext>
  tools: Tools
  agents: AgentDefinition[]
  allowedAgentTypes?: string[]
}): Promise<string>
```

When building the API request, each enabled tool is converted:

```typescript
// src/utils/api.ts:169-178
base = {
  name: tool.name,
  description: await tool.prompt({
    getToolPermissionContext: options.getToolPermissionContext,
    tools: options.tools,
    agents: options.agents,
    allowedAgentTypes: options.allowedAgentTypes,
  }),
  input_schema,
}
```

The tool description is then cached per-session via `toolSchemaCache` to prevent
recalculation drift.

### 5.1 System Prompt References to Tools

While tool descriptions are not in the system prompt, the system prompt **does**
reference tool names in its behavioral guidance. For example:

```typescript
// src/constants/prompts.ts:301
`Reserve using the ${BASH_TOOL_NAME} exclusively for system commands...`
```

This means the system prompt and tool descriptions work as a coordinated pair --
the system prompt tells the model *when* to use each tool, and the tool's
`.prompt()` tells it *how*.

---

## 6. Dynamic Sections: Environment Information

### 6.1 The `# Environment` Section

`computeSimpleEnvInfo()` in `src/constants/prompts.ts:651-710` builds a
structured environment block:

```typescript
const envItems = [
  `Primary working directory: ${cwd}`,
  // worktree notice if applicable
  [`Is a git repository: ${isGit}`],
  // additional working directories if --add-dir
  `Platform: ${env.platform}`,
  getShellInfoLine(),           // "Shell: zsh" or "Shell: bash"
  `OS Version: ${unameSR}`,     // "Darwin 25.3.0" or "Linux 6.6.4"
  modelDescription,             // "You are powered by Claude Opus 4.6..."
  knowledgeCutoffMessage,       // "Assistant knowledge cutoff is May 2025."
  // latest model family IDs
  // Claude Code availability info
  // Fast mode explanation
]
```

### 6.2 Knowledge Cutoff

Each model has a hardcoded knowledge cutoff date:

```typescript
// src/constants/prompts.ts:713-730
function getKnowledgeCutoff(modelId: string): string | null {
  if (canonical.includes('claude-sonnet-4-6')) return 'August 2025'
  if (canonical.includes('claude-opus-4-6'))   return 'May 2025'
  if (canonical.includes('claude-opus-4-5'))   return 'May 2025'
  if (canonical.includes('claude-haiku-4'))    return 'February 2025'
  // ...
}
```

### 6.3 Shell Detection

```typescript
// src/constants/prompts.ts:732-743
function getShellInfoLine(): string {
  const shell = process.env.SHELL || 'unknown'
  const shellName = shell.includes('zsh') ? 'zsh'
    : shell.includes('bash') ? 'bash' : shell
  // On Windows: adds "(use Unix shell syntax, not Windows)"
  return `Shell: ${shellName}`
}
```

---

## 7. Custom / Append System Prompt Overrides

### 7.1 Language Preference

```typescript
// src/constants/prompts.ts:142-149
function getLanguageSection(languagePreference: string | undefined): string | null {
  if (!languagePreference) return null
  return `# Language
Always respond in ${languagePreference}. Use ${languagePreference} for all
explanations, comments, and communications with the user.`
}
```

### 7.2 Output Style Configuration

```typescript
// src/constants/prompts.ts:151-158
function getOutputStyleSection(outputStyleConfig: OutputStyleConfig | null): string | null {
  if (outputStyleConfig === null) return null
  return `# Output Style: ${outputStyleConfig.name}
${outputStyleConfig.prompt}`
}
```

### 7.3 MCP Server Instructions

Connected MCP servers can provide instructions that are injected:

```typescript
// src/constants/prompts.ts:579-604
function getMcpInstructions(mcpClients: MCPServerConnection[]): string | null {
  // Filters to connected clients with instructions
  // Formats as:
  // # MCP Server Instructions
  // ## ServerName
  // <server's instructions text>
}
```

### 7.4 The SIMPLE Mode Escape Hatch

When `CLAUDE_CODE_SIMPLE` is set (the `--bare` flag), the entire prompt
collapses to a single line:

```typescript
// src/constants/prompts.ts:450-453
if (isEnvTruthy(process.env.CLAUDE_CODE_SIMPLE)) {
  return [
    `You are Claude Code, Anthropic's official CLI for Claude.\n\nCWD: ${getCwd()}\nDate: ${getSessionStartDate()}`,
  ]
}
```

This is used for testing, SDK integrations, and minimal deployments.

---

## Architecture Diagram

```
getSystemPrompt(tools, model, additionalDirs, mcpClients)
  |
  |-- [Static Sections] ---------------------------------> cacheScope: 'global'
  |     |-- getSimpleIntroSection()
  |     |-- getSimpleSystemSection()
  |     |-- getSimpleDoingTasksSection()
  |     |-- getActionsSection()
  |     |-- getUsingYourToolsSection(enabledTools)
  |     |-- getSimpleToneAndStyleSection()
  |     |-- getOutputEfficiencySection()
  |
  |-- SYSTEM_PROMPT_DYNAMIC_BOUNDARY
  |
  |-- [Dynamic Sections] --------------------------------> cacheScope: null
  |     |-- systemPromptSection('session_guidance', ...)
  |     |-- systemPromptSection('memory', loadMemoryPrompt)
  |     |     |-- buildMemoryLines()
  |     |     |     |-- memory behavioral instructions
  |     |     |     |-- MEMORY.md content (truncated at 200 lines / 25 KB)
  |     |
  |     |-- systemPromptSection('env_info_simple', ...)
  |     |     |-- CWD, git status, platform, shell
  |     |     |-- model name, knowledge cutoff
  |     |
  |     |-- systemPromptSection('language', ...)
  |     |-- systemPromptSection('output_style', ...)
  |     |-- DANGEROUS_uncachedSystemPromptSection('mcp_instructions', ...)
  |     |-- systemPromptSection('scratchpad', ...)
  |     |-- systemPromptSection('frc', ...)
  |     |-- systemPromptSection('summarize_tool_results', ...)
  |
  v
splitSysPromptPrefix(systemPrompt)
  |
  |-- SystemPromptBlock { text, cacheScope: 'global' }  -- static prefix
  |-- SystemPromptBlock { text, cacheScope: null }       -- dynamic suffix
  |
  v
Anthropic API messages.create({ system: [...blocks], tools: [...] })
  |
  |-- Tool descriptions come from tool.prompt(), NOT from system prompt
```

---

## Key Design Decisions

1. **Why a string array instead of one string?** The array allows the boundary
   marker to split static from dynamic content without string parsing. It also
   lets `null` entries be filtered out cleanly.

2. **Why name sections?** Named sections enable targeted cache invalidation.
   If only the memory changes, only the `'memory'` cache entry needs clearing.

3. **Why is CLAUDE.md in user context, not system prompt?** CLAUDE.md content is
   injected as a user-context message (via `getClaudeMds()`), not as part of the
   system prompt array. This keeps the system prompt stable for caching while
   allowing CLAUDE.md to change between turns (e.g., when the user edits it).

4. **Why the DANGEROUS_ prefix?** It is a code-review signal. Any section that
   breaks the prompt cache must justify itself with a `_reason` string and
   survive the scrutiny of the prefix.

5. **Why truncate MEMORY.md?** Without caps, a large memory index would consume
   context window space that should be available for the actual conversation.
   The 200-line / 25 KB caps are calibrated to the p97 observed usage.
