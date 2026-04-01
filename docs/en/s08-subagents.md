# s08: Subagents

`s01 > s02 > s03 > s04 > s05 | s06 > s07 > [ s08 ] s09 > s10 | s11 > s12 > s13 > s14`

> "Spawn cheap copies, not expensive clones."

## Problem

Complex tasks decompose into subtasks. "Implement login" might spawn an Explore agent to find auth code, a Plan agent to design the approach, and a worker agent to do the job. Each needs the same system prompt and context -- but re-processing 100K+ tokens from scratch per spawn is a massive waste.

## Solution

Claude Code uses CacheSafeParams to share the prompt cache prefix between parent and child agents. The API recognizes the byte-identical prefix and returns a cache hit.

```
  Parent Agent (cached prefix)
  +-----------------------------------------+
  | system_prompt + tools + model + messages |
  +-----+--------+--------+--------+--------+
        |        |        |        |
        v        v        v        v
  +--------+ +--------+ +--------+ +--------+
  | Explore| | Plan   | | Worker | | Verify |
  | (haiku)| |(inherit)| | (R/W) | | (bg)   |
  | RO     | | RO     | |        | | RO     |
  +--------+ +--------+ +--------+ +--------+
  Each child: isolated context, shared cache prefix
```

## How It Works

### Step 1: CacheSafeParams

Five components form the API cache key. If parent and child send identical values, the API returns a cache read. Source: `forkedAgent.ts`.

```python
# agents/s08_subagents.py (simplified)

@dataclass
class CacheSafeParams:
    system_prompt: str
    user_context: dict       # claudeMd, currentDate, etc.
    system_context: dict     # gitStatus, osInfo, etc.
    tools: list[str]         # order matters for cache key
    fork_context_messages: list[dict]  # parent message prefix
```

### Step 2: Context isolation

Every subagent gets its own execution context. File cache is cloned so one agent's reads don't pollute another's. Source: `forkedAgent.ts`.

```python
def create_subagent_context(parent_ctx, agent_type="general-purpose"):
    return SubagentContext(
        agent_id=uuid4(),
        agent_type=agent_type,
        read_file_cache=dict(parent_ctx.read_file_cache),  # clone
        query_depth=parent_ctx.query_depth + 1,
    )
```

### Step 3: Built-in agent types

Four built-in types, each tuned for a specific purpose. Source: `builtInAgents.ts`.

```python
EXPLORE  = AgentDef(model="haiku", disallow=["Agent","FileEdit","FileWrite"],
                    omit_claude_md=True)   # fast, read-only
PLAN     = AgentDef(model="inherit", disallow=["Agent","FileEdit","FileWrite"],
                    omit_claude_md=True)   # architect, read-only
GENERAL  = AgentDef(tools=["*"])           # full access
VERIFY   = AgentDef(model="inherit", disallow=["Agent","FileEdit","FileWrite"],
                    background=True)       # async, read-only
```

### Step 4: The agent runner

The runner orchestrates the full lifecycle: resolve model, build system prompt, assemble tools, create context, expose CacheSafeParams, run the query loop, record the transcript, clean up. Source: `runAgent.ts`.

```python
class AgentRunner:
    def run_agent(self, agent_def, prompt, fork_context_messages=None):
        resolved_model = agent_def.model or "claude-sonnet-4-20250514"
        system_prompt = agent_def.system_prompt
        if not agent_def.omit_claude_md:
            system_prompt += "\n\n" + claude_md_rules

        resolved_tools = [t for t in self.all_tools
                          if t not in agent_def.disallowed_tools]

        ctx = create_subagent_context(self.parent_context)
        # Expose CacheSafeParams for cache sharing
        # Run query loop, record transcript, cleanup
```

### Step 5: Sidechain transcript recording

Every subagent's messages are recorded with a UUID chain. This enables resume after crashes and post-hoc debugging. Source: `sessionStorage.ts`.

## What Changed

| Component | Before (s07) | After (s08) |
|-----------|-------------|-------------|
| Subagent prompt | N/A | CacheSafeParams enables API cache hit |
| Agent state | N/A | Isolated context with cloned caches |
| Agent types | N/A | 4 built-in: Explore, Plan, General, Verify |
| Transcript | N/A | Sidechain recording with UUID chain |
| Read-only enforcement | N/A | Write tools in disallowed list |
| Background work | N/A | Verification agent runs async |
| Custom agents | N/A | Loaded from `.claude/agents/*.md` |

## Try It

```bash
cd learn-the-actual-claude-code
python agents/s08_subagents.py
```

The demo spawns each agent type, captures CacheSafeParams, loads a custom agent from a markdown file, and inspects sidechain transcripts.

Try these prompts to see subagents in action:

- "Find all files related to authentication" (Explore agent)
- "Plan how to refactor the auth module" (Plan agent)
- "Implement the refactoring, then verify it works" (General + Verify agents)
