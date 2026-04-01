# Session 08 -- Subagents: Prompt Cache Sharing

s01 > s02 > s03 > s04 > s05 | s06 > s07 > **s08** > s09 > s10 | s11 > s12 > s13 > s14

---

> *"Spawn cheap copies, not expensive clones."*
>
> **Harness layer**: This session covers the subagent system -- how Claude Code
> spawns child agents that share the parent's prompt cache prefix, avoiding
> redundant processing of 100K+ tokens. The design turns a potentially expensive
> operation into a near-free cache hit.

---

## Problem

Complex tasks naturally decompose into subtasks. "Implement login" might spawn
an Explore agent to find existing auth code, a Plan agent to design the
approach, and a general-purpose agent to do the work. Each agent needs the same
system prompt, tool definitions, and conversation context.

Without cache sharing, every subagent re-processes the full prompt from scratch.
With a 100K+ token prefix (system prompt + tools + CLAUDE.md + conversation
history), that is a massive waste of time and money -- each spawn could cost
seconds and hundreds of thousands of input tokens.

You need a system that:

- Spawns agents with isolated state (no interference between parent and child)
- Shares the prompt cache prefix so the API returns a cache hit, not a recompute
- Records every agent's transcript for debugging and resume
- Supports different agent types (read-only, full-access, background)

## Solution

Claude Code uses **CacheSafeParams** -- a structured object carrying the five
components that form the API cache key. When a child agent sends these same
values, the Anthropic API recognizes the prefix as byte-identical and returns a
cache read.

```
  Parent Agent
  +------------------------------------------+
  | system_prompt  (cached)                   |
  | tools          (cached)                   |
  | model          (cached)                   |
  | message prefix (cached)                   |
  | thinking config(cached)                   |
  +----+---------+---------+---------+--------+
       |         |         |         |
       v         v         v         v
  +--------+ +--------+ +--------+ +--------+
  | Explore| | Plan   | | General| | Verify |
  | (haiku)| |(inherit)| | Purpose| | (bg)  |
  | RO     | | RO     | | R/W    | | RO    |
  +--------+ +--------+ +--------+ +--------+
       |         |         |         |
       v         v         v         v
  Sidechain transcript recording (per agent)
```

Each child gets an isolated `SubagentContext` -- cloned file cache, independent
abort signal, incremented query depth -- but the API request prefix is shared.

## How It Works

### CacheSafeParams

The five components that must be identical between parent and child for prompt
cache sharing to work.

```python
# agents/s08_subagents.py -- mirrors CacheSafeParams in forkedAgent.ts

@dataclass
class CacheSafeParams:
    """
    system prompt + tools + model + message prefix + thinking config
    => cache key

    CacheSafeParams carries the first four; thinking config is inherited
    via toolUseContext.options.thinkingConfig.
    """
    system_prompt: str
    user_context: dict[str, str]        # e.g. {"claudeMd": "...", "currentDate": "..."}
    system_context: dict[str, str]      # e.g. {"gitStatus": "...", "osInfo": "..."}
    tools: list[str]                    # tool names (order matters for cache key)
    fork_context_messages: list[dict]   # parent message prefix
```

### Subagent Context Isolation

Every subagent gets its own execution context. Mutable state is cloned to
prevent cross-agent interference. The file cache is deep-copied so one agent's
reads do not pollute another's.

```python
# agents/s08_subagents.py -- mirrors createSubagentContext() in forkedAgent.ts

@dataclass
class SubagentContext:
    agent_id: str
    agent_type: str
    messages: list[dict] = field(default_factory=list)
    read_file_cache: dict[str, str] = field(default_factory=dict)
    abort_signal: bool = False
    query_depth: int = 0
    share_set_app_state: bool = False

def create_subagent_context(
    parent_ctx: SubagentContext,
    *,
    agent_id: Optional[str] = None,
    agent_type: str = "general-purpose",
    messages: Optional[list[dict]] = None,
    share_set_app_state: bool = False,
) -> SubagentContext:
    return SubagentContext(
        agent_id=agent_id or str(uuid.uuid4()),
        agent_type=agent_type,
        messages=messages if messages is not None else [],
        # Clone file cache to prevent cross-agent interference
        read_file_cache=dict(parent_ctx.read_file_cache),
        abort_signal=False,
        query_depth=parent_ctx.query_depth + 1,
        share_set_app_state=share_set_app_state,
    )
```

### Built-In Agent Types

Claude Code ships with four built-in agent types, each tuned for a specific
purpose.

```python
# agents/s08_subagents.py -- mirrors src/tools/AgentTool/built-in/*.ts

EXPLORE_AGENT = AgentDefinition(
    agent_type="Explore",
    when_to_use="Fast agent specialized for exploring codebases.",
    tools=None,  # All tools except disallowed
    disallowed_tools=["Agent", "FileEdit", "FileWrite", "NotebookEdit"],
    model="haiku",
    omit_claude_md=True,  # Skip CLAUDE.md for speed
)

PLAN_AGENT = AgentDefinition(
    agent_type="Plan",
    when_to_use="Software architect agent for designing implementation plans.",
    disallowed_tools=["Agent", "FileEdit", "FileWrite", "NotebookEdit"],
    model="inherit",  # Use parent's model
    omit_claude_md=True,
)

GENERAL_PURPOSE_AGENT = AgentDefinition(
    agent_type="general-purpose",
    when_to_use="General-purpose agent for complex questions and multi-step tasks.",
    tools=["*"],  # All tools
)

VERIFICATION_AGENT = AgentDefinition(
    agent_type="verification",
    when_to_use="Verify implementation correctness. Produces PASS/FAIL/PARTIAL.",
    disallowed_tools=["Agent", "FileEdit", "FileWrite", "NotebookEdit"],
    model="inherit",
    background=True,  # Always runs async
)
```

### The Agent Runner

The runner orchestrates the full subagent lifecycle: resolve model, compute
system prompt, assemble tools, create context, expose CacheSafeParams, run the
query loop, record the transcript, and clean up.

```python
# agents/s08_subagents.py -- mirrors runAgent() in runAgent.ts

class AgentRunner:
    def run_agent(self, agent_def, prompt, *, is_async=False,
                  fork_context_messages=None, on_cache_safe_params=None):
        # 1. Model resolution
        resolved_model = agent_def.model or "claude-sonnet-4-20250514"

        # 2. Context message assembly
        context_msgs = list(fork_context_messages or [])
        initial_messages = context_msgs + [user_msg]

        # 3. System prompt (optionally skip CLAUDE.md)
        system_prompt = agent_def.system_prompt
        if not agent_def.omit_claude_md:
            system_prompt += "\n\n[CLAUDE.md rules appended here]"

        # 4. Tool resolution with wildcard expansion
        if agent_def.tools is None or agent_def.tools == ["*"]:
            resolved_tools = [t for t in self.all_tools
                              if t not in agent_def.disallowed_tools]

        # 5. Create isolated context
        agent_ctx = create_subagent_context(self.parent_context, ...)

        # 6. Expose CacheSafeParams for fork cache sharing
        if on_cache_safe_params is not None:
            params = CacheSafeParams(
                system_prompt=system_prompt,
                tools=resolved_tools,
                fork_context_messages=initial_messages,
                ...
            )
            on_cache_safe_params(params)

        # 7. Record initial transcript
        _recorder.record(agent_id, initial_messages)

        # 8. Run query loop
        output_messages = simulated_query(initial_messages, system_prompt, ...)

        # 9. Cleanup: clear caches, kill shells, remove hooks
        agent_ctx.read_file_cache.clear()
        return output_messages
```

### Sidechain Transcript Recording

Every subagent's messages are recorded incrementally with a UUID chain. This
enables resume after crashes and post-hoc debugging.

```python
# agents/s08_subagents.py -- mirrors recordSidechainTranscript() in sessionStorage.ts

class SidechainRecorder:
    def __init__(self):
        self._transcripts: dict[str, list[dict]] = {}

    def record(self, agent_id: str, messages: list[dict],
               parent_uuid: Optional[str] = None) -> str:
        if agent_id not in self._transcripts:
            self._transcripts[agent_id] = []
        for msg in messages:
            entry = {
                "uuid": str(uuid.uuid4()),
                "parent_uuid": parent_uuid,
                **msg,
            }
            self._transcripts[agent_id].append(entry)
            parent_uuid = entry["uuid"]
        return parent_uuid or ""
```

## What Changed

| Component | Before | After |
|-----------|--------|-------|
| Subagent prompt | Re-processes 100K+ tokens from scratch | CacheSafeParams enables API cache hit |
| Agent state | Shared mutable state (interference risk) | Isolated SubagentContext with cloned caches |
| Agent types | One-size-fits-all | 4 built-in types: Explore, Plan, General, Verify |
| Transcript | Lost after session ends | Sidechain recording with UUID chain for resume |
| Read-only agents | No enforcement | Explore/Plan agents have write tools in disallowed list |
| Background work | Blocks the main agent | Verification agent runs async by default |
| Custom agents | Hard-coded only | Loaded from `.claude/agents/*.md` with frontmatter |

## Try It

```bash
# Run the subagent demo
python agents/s08_subagents.py
```

The demo walks through:

1. **Explore agent** -- spawning a read-only search agent with haiku
2. **General-purpose agent** -- spawning with fork context and CacheSafeParams capture
3. **Verification agent** -- spawning a background agent that runs async
4. **Custom agent loading** -- reading agent definitions from markdown files
5. **Sidechain transcripts** -- inspecting the recorded message chains

Try modifying the demo:

- Add a new built-in agent type with specific tool restrictions
- Change the Explore agent's model from haiku to inherit
- Add a custom agent markdown file and watch it get discovered
