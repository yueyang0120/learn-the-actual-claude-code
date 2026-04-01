"""
Session 08 -- Subagent Reimplementation
========================================
A runnable Python model of Claude Code's subagent architecture.

Maps to the real source files:
  - AgentDefinition     -> src/tools/AgentTool/loadAgentsDir.ts
  - CacheSafeParams     -> src/utils/forkedAgent.ts  (CacheSafeParams type)
  - createSubagentContext -> src/utils/forkedAgent.ts  (createSubagentContext())
  - AgentRunner.run_agent -> src/tools/AgentTool/runAgent.ts  (runAgent())
  - load_agents_from_dir -> src/tools/AgentTool/loadAgentsDir.ts  (parseAgentFromMarkdown)
  - SidechainRecorder    -> src/utils/sessionStorage.ts  (recordSidechainTranscript)
  - BUILT_IN_AGENTS      -> src/tools/AgentTool/builtInAgents.ts
"""

from __future__ import annotations

import copy
import json
import os
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional


# ---------------------------------------------------------------------------
# 1. Agent Definition
#    Real: src/tools/AgentTool/loadAgentsDir.ts  (AgentDefinition union type)
# ---------------------------------------------------------------------------

@dataclass
class AgentDefinition:
    """Mirrors the BaseAgentDefinition + source-specific subtypes."""

    agent_type: str                         # e.g. "Explore", "general-purpose"
    when_to_use: str                        # description shown to model
    system_prompt: str                      # body of the agent prompt
    source: str = "built-in"               # "built-in" | "user" | "project" | "plugin"
    tools: Optional[list[str]] = None       # None or ['*'] means all tools
    disallowed_tools: list[str] = field(default_factory=list)
    model: Optional[str] = None             # None -> default, "inherit" -> parent's
    permission_mode: str = "default"        # e.g. "acceptEdits", "bubble", "plan"
    max_turns: int = 50
    omit_claude_md: bool = False            # Explore/Plan skip CLAUDE.md
    background: bool = False                # always run async


# ---------------------------------------------------------------------------
# 2. CacheSafeParams
#    Real: src/utils/forkedAgent.ts  (CacheSafeParams type)
#
#    The five components that form the Anthropic API cache key.  Sharing these
#    between parent and child means the child's API request prefix is byte-
#    identical, so the API returns a cache read instead of reprocessing.
# ---------------------------------------------------------------------------

@dataclass
class CacheSafeParams:
    """
    Parameters that must be identical between parent and child to share the
    prompt cache.  In the real code:

        system prompt + tools + model + message prefix + thinking config
        => cache key

    CacheSafeParams carries the first four; thinking config is inherited via
    toolUseContext.options.thinkingConfig.
    """

    system_prompt: str
    user_context: dict[str, str]        # e.g. {"claudeMd": "...", "currentDate": "..."}
    system_context: dict[str, str]      # e.g. {"gitStatus": "...", "osInfo": "..."}
    tools: list[str]                    # tool names (order matters for cache key)
    fork_context_messages: list[dict]   # parent message prefix


# ---------------------------------------------------------------------------
# 3. Subagent Context (ToolUseContext isolation)
#    Real: src/utils/forkedAgent.ts  (createSubagentContext)
# ---------------------------------------------------------------------------

@dataclass
class SubagentContext:
    """
    Isolated execution context for a subagent.

    Real code clones: readFileState, contentReplacementState, abortController,
    and stubs out UI callbacks (setToolJSX, addNotification, etc.).
    """

    agent_id: str
    agent_type: str
    messages: list[dict] = field(default_factory=list)
    read_file_cache: dict[str, str] = field(default_factory=dict)
    abort_signal: bool = False
    query_depth: int = 0
    # In the real code, setAppState is a no-op for async agents.
    # Sync agents share the parent's setAppState.
    share_set_app_state: bool = False


def create_subagent_context(
    parent_ctx: SubagentContext,
    *,
    agent_id: Optional[str] = None,
    agent_type: str = "general-purpose",
    messages: Optional[list[dict]] = None,
    share_set_app_state: bool = False,
) -> SubagentContext:
    """
    Real: createSubagentContext() in src/utils/forkedAgent.ts

    By default all mutable state is cloned to prevent interference.
    Callers opt-in to sharing specific callbacks.
    """
    return SubagentContext(
        agent_id=agent_id or str(uuid.uuid4()),
        agent_type=agent_type,
        messages=messages if messages is not None else [],
        # Clone file cache to prevent cross-agent interference.
        # Real code: cloneFileStateCache(parentContext.readFileState)
        read_file_cache=dict(parent_ctx.read_file_cache),
        abort_signal=False,
        query_depth=parent_ctx.query_depth + 1,
        share_set_app_state=share_set_app_state,
    )


# ---------------------------------------------------------------------------
# 4. Sidechain Transcript Recorder
#    Real: src/utils/sessionStorage.ts  (recordSidechainTranscript)
# ---------------------------------------------------------------------------

class SidechainRecorder:
    """
    Records each subagent's messages for resume and debugging.

    Real code writes to disk under a per-session subagents/ directory.
    Each message is appended incrementally with a parent UUID chain.
    """

    def __init__(self) -> None:
        self._transcripts: dict[str, list[dict]] = {}

    def record(self, agent_id: str, messages: list[dict],
               parent_uuid: Optional[str] = None) -> str:
        """Record messages and return the last message UUID."""
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

    def get_transcript(self, agent_id: str) -> list[dict]:
        return list(self._transcripts.get(agent_id, []))


# Global recorder (real code uses file-backed session storage)
_recorder = SidechainRecorder()


# ---------------------------------------------------------------------------
# 5. Simulated query() loop
#    Real: src/query.ts  (the core agentic loop)
# ---------------------------------------------------------------------------

def simulated_query(
    messages: list[dict],
    system_prompt: str,
    tools: list[str],
    max_turns: int = 5,
) -> list[dict]:
    """
    Simulates Claude Code's query() loop.  In reality this calls the Anthropic
    API, handles tool execution, manages streaming, etc.  Here we produce a
    single assistant response for demonstration.
    """
    prompt_summary = messages[-1].get("content", "")[:80] if messages else ""
    return [
        {
            "type": "assistant",
            "content": (
                f"[Subagent response to: {prompt_summary}...]\n"
                f"System prompt length: {len(system_prompt)} chars\n"
                f"Available tools: {', '.join(tools[:5])}{'...' if len(tools) > 5 else ''}\n"
                f"Max turns: {max_turns}"
            ),
            "uuid": str(uuid.uuid4()),
        }
    ]


# ---------------------------------------------------------------------------
# 6. AgentRunner -- The orchestration engine
#    Real: src/tools/AgentTool/runAgent.ts  (runAgent generator)
# ---------------------------------------------------------------------------

class AgentRunner:
    """
    Orchestrates the full subagent lifecycle:
      1. Resolve model
      2. Compute system prompt
      3. Assemble tool list
      4. Create isolated context
      5. Optionally expose CacheSafeParams for cache sharing
      6. Run query loop
      7. Record sidechain transcript
      8. Clean up
    """

    def __init__(self, parent_context: SubagentContext,
                 all_tools: list[str]) -> None:
        self.parent_context = parent_context
        self.all_tools = all_tools

    def run_agent(
        self,
        agent_def: AgentDefinition,
        prompt: str,
        *,
        is_async: bool = False,
        fork_context_messages: Optional[list[dict]] = None,
        on_cache_safe_params: Optional[Callable[[CacheSafeParams], None]] = None,
    ) -> list[dict]:
        """
        Real: runAgent() async generator in runAgent.ts

        Returns the list of messages produced by the subagent.
        """
        start_time = time.time()
        agent_id = str(uuid.uuid4())

        # --- 2a. Model resolution ---
        # Real: getAgentModel(agentDef.model, parentModel, overrideModel, permMode)
        resolved_model = agent_def.model or "claude-sonnet-4-20250514"
        if resolved_model == "inherit":
            resolved_model = "parent-model"

        # --- 2b. Context message assembly ---
        # Real: filterIncompleteToolCalls(forkContextMessages) then concat
        context_msgs = list(fork_context_messages or [])
        user_msg = {"type": "user", "content": prompt, "uuid": str(uuid.uuid4())}
        initial_messages = context_msgs + [user_msg]

        # --- 2c. System prompt ---
        # Real: getAgentSystemPrompt -> enhanceSystemPromptWithEnvDetails
        system_prompt = agent_def.system_prompt
        if not agent_def.omit_claude_md:
            system_prompt += "\n\n[CLAUDE.md rules would be appended here]"

        # --- 2d. Tool resolution ---
        # Real: resolveAgentTools() with wildcard expansion and filtering
        if agent_def.tools is None or agent_def.tools == ["*"]:
            resolved_tools = [t for t in self.all_tools
                              if t not in agent_def.disallowed_tools]
        else:
            resolved_tools = [t for t in agent_def.tools
                              if t in self.all_tools
                              and t not in agent_def.disallowed_tools]

        # --- 2e. Create isolated context ---
        # Real: createSubagentContext(toolUseContext, { options, agentId, ... })
        agent_ctx = create_subagent_context(
            self.parent_context,
            agent_id=agent_id,
            agent_type=agent_def.agent_type,
            messages=initial_messages,
            share_set_app_state=not is_async,
        )

        # --- 2f. Expose CacheSafeParams for fork cache sharing ---
        # Real: onCacheSafeParams callback in runAgent.ts
        if on_cache_safe_params is not None:
            params = CacheSafeParams(
                system_prompt=system_prompt,
                user_context={},
                system_context={},
                tools=resolved_tools,
                fork_context_messages=initial_messages,
            )
            on_cache_safe_params(params)

        # --- 2g. Record initial transcript ---
        # Real: void recordSidechainTranscript(initialMessages, agentId)
        last_uuid = _recorder.record(agent_id, initial_messages)

        # --- 2h. Run query loop ---
        # Real: for await (const message of query({...})) { yield message }
        output_messages = simulated_query(
            messages=initial_messages,
            system_prompt=system_prompt,
            tools=resolved_tools,
            max_turns=agent_def.max_turns,
        )

        # Record each output message incrementally
        for msg in output_messages:
            last_uuid = _recorder.record(agent_id, [msg], last_uuid)

        # --- 2i. Cleanup ---
        # Real: finally block in runAgent.ts
        #   - mcpCleanup()
        #   - clearSessionHooks()
        #   - readFileState.clear()
        #   - killShellTasksForAgent()
        agent_ctx.read_file_cache.clear()

        elapsed = time.time() - start_time
        print(f"  [{agent_def.agent_type}] completed in {elapsed:.3f}s, "
              f"{len(output_messages)} messages, model={resolved_model}")

        return output_messages


# ---------------------------------------------------------------------------
# 7. Agent Loading from Directory
#    Real: loadAgentsDir.ts  (loadMarkdownFilesForSubdir -> parseAgentFromMarkdown)
# ---------------------------------------------------------------------------

def load_agents_from_dir(agents_dir: str) -> list[AgentDefinition]:
    """
    Load agent definitions from a directory of markdown files.

    Real code uses loadMarkdownFilesForSubdir('agents', cwd) which reads
    .claude/agents/*.md, parses YAML frontmatter, and constructs
    CustomAgentDefinition objects with closure-based getSystemPrompt().
    """
    agents = []
    agents_path = Path(agents_dir)
    if not agents_path.is_dir():
        return agents

    for md_file in sorted(agents_path.glob("*.md")):
        content = md_file.read_text()
        # Minimal frontmatter parser (real code uses a YAML library)
        if not content.startswith("---"):
            continue
        end = content.index("---", 3)
        frontmatter_text = content[3:end].strip()
        body = content[end + 3:].strip()

        # Parse frontmatter key-value pairs
        fm: dict[str, Any] = {}
        for line in frontmatter_text.split("\n"):
            if ":" in line:
                key, val = line.split(":", 1)
                fm[key.strip()] = val.strip().strip('"').strip("'")

        name = fm.get("name")
        description = fm.get("description")
        if not name or not description:
            continue

        # Parse tools list (simplified)
        tools_raw = fm.get("tools")
        tools = None
        if tools_raw and tools_raw.startswith("["):
            tools = [t.strip().strip('"') for t in tools_raw[1:-1].split(",")]

        agents.append(AgentDefinition(
            agent_type=name,
            when_to_use=description,
            system_prompt=body,
            source="project",
            tools=tools,
            model=fm.get("model"),
            max_turns=int(fm.get("maxTurns", 50)),
            background=fm.get("background", "").lower() == "true",
        ))

    return agents


# ---------------------------------------------------------------------------
# 8. Built-in Agent Types
#    Real: src/tools/AgentTool/built-in/*.ts
# ---------------------------------------------------------------------------

EXPLORE_AGENT = AgentDefinition(
    agent_type="Explore",
    when_to_use=(
        "Fast agent specialized for exploring codebases. Use for file search, "
        "keyword search, or answering questions about the codebase."
    ),
    system_prompt=(
        "You are a file search specialist for Claude Code.\n"
        "=== CRITICAL: READ-ONLY MODE ===\n"
        "Use Glob for file patterns, Grep for content search, Read for specific files.\n"
        "Spawn multiple parallel tool calls for efficiency."
    ),
    tools=None,  # All tools except disallowed
    disallowed_tools=["Agent", "FileEdit", "FileWrite", "NotebookEdit"],
    model="haiku",
    omit_claude_md=True,
)

PLAN_AGENT = AgentDefinition(
    agent_type="Plan",
    when_to_use=(
        "Software architect agent for designing implementation plans."
    ),
    system_prompt=(
        "You are a software architect and planning specialist.\n"
        "=== CRITICAL: READ-ONLY MODE ===\n"
        "Explore the codebase and design step-by-step implementation plans.\n"
        "End with Critical Files for Implementation."
    ),
    tools=None,
    disallowed_tools=["Agent", "FileEdit", "FileWrite", "NotebookEdit"],
    model="inherit",
    omit_claude_md=True,
)

GENERAL_PURPOSE_AGENT = AgentDefinition(
    agent_type="general-purpose",
    when_to_use=(
        "General-purpose agent for complex questions, code search, multi-step tasks."
    ),
    system_prompt=(
        "You are an agent for Claude Code. Complete the task fully -- "
        "don't gold-plate, but don't leave it half-done.\n"
        "Respond with a concise report covering what was done."
    ),
    tools=["*"],
)

VERIFICATION_AGENT = AgentDefinition(
    agent_type="verification",
    when_to_use=(
        "Verify implementation correctness after non-trivial tasks. "
        "Produces PASS/FAIL/PARTIAL verdict with evidence."
    ),
    system_prompt=(
        "You are a verification specialist. Your job is to try to break it.\n"
        "=== CRITICAL: DO NOT MODIFY THE PROJECT ===\n"
        "Run builds, tests, linters. Try adversarial probes.\n"
        "End with VERDICT: PASS | FAIL | PARTIAL"
    ),
    disallowed_tools=["Agent", "FileEdit", "FileWrite", "NotebookEdit"],
    model="inherit",
    background=True,
)

BUILT_IN_AGENTS = [EXPLORE_AGENT, PLAN_AGENT, GENERAL_PURPOSE_AGENT, VERIFICATION_AGENT]


# ---------------------------------------------------------------------------
# 9. Demo
# ---------------------------------------------------------------------------

def main() -> None:
    print("=" * 70)
    print("Session 08 -- Subagent Reimplementation Demo")
    print("=" * 70)

    # Simulate available tools (real code assembles via assembleToolPool)
    all_tools = [
        "Bash", "Read", "Write", "Edit", "Glob", "Grep",
        "Agent", "FileEdit", "FileWrite", "NotebookEdit", "WebSearch",
    ]

    # Create parent context
    parent_ctx = SubagentContext(
        agent_id="parent-session",
        agent_type="main",
        messages=[{"type": "user", "content": "Implement the login feature"}],
        read_file_cache={"src/auth.ts": "export function login() { ... }"},
    )

    runner = AgentRunner(parent_ctx, all_tools)

    # --- Demo 1: Spawn Explore agent (read-only search) ---
    print("\n--- 1. Spawning Explore agent ---")
    explore_msgs = runner.run_agent(
        EXPLORE_AGENT,
        "Find all authentication-related files in the codebase",
    )
    for msg in explore_msgs:
        print(f"  Response: {msg['content'][:120]}...")

    # --- Demo 2: Spawn general-purpose agent with fork context ---
    print("\n--- 2. Spawning general-purpose agent with fork context ---")
    fork_messages = [
        {"type": "user", "content": "Build a login page", "uuid": str(uuid.uuid4())},
        {"type": "assistant", "content": "I'll build that.", "uuid": str(uuid.uuid4())},
    ]

    captured_params: list[CacheSafeParams] = []
    gp_msgs = runner.run_agent(
        GENERAL_PURPOSE_AGENT,
        "Implement the form validation logic",
        fork_context_messages=fork_messages,
        on_cache_safe_params=lambda p: captured_params.append(p),
    )
    for msg in gp_msgs:
        print(f"  Response: {msg['content'][:120]}...")

    if captured_params:
        p = captured_params[0]
        print(f"\n  CacheSafeParams captured:")
        print(f"    system_prompt length: {len(p.system_prompt)}")
        print(f"    tools: {p.tools[:5]}...")
        print(f"    fork_context_messages: {len(p.fork_context_messages)} messages")

    # --- Demo 3: Spawn async background agent (verification) ---
    print("\n--- 3. Spawning verification agent (background) ---")
    verify_msgs = runner.run_agent(
        VERIFICATION_AGENT,
        "Verify the login implementation: files changed auth.ts, login.tsx",
        is_async=True,
    )
    for msg in verify_msgs:
        print(f"  Response: {msg['content'][:120]}...")

    # --- Demo 4: Load custom agents from directory (if exists) ---
    print("\n--- 4. Agent loading from directory ---")
    demo_dir = "/tmp/demo-agents"
    os.makedirs(demo_dir, exist_ok=True)
    # Write a sample agent definition
    sample_agent_md = """\
---
name: code-reviewer
description: "Reviews code for style, bugs, and security issues"
model: inherit
maxTurns: 20
---

You are a code review specialist. Examine the provided code for:
1. Style violations
2. Potential bugs
3. Security issues
Report findings with file paths and line numbers.
"""
    Path(f"{demo_dir}/code-reviewer.md").write_text(sample_agent_md)

    loaded = load_agents_from_dir(demo_dir)
    for agent in loaded:
        print(f"  Loaded: {agent.agent_type} -- {agent.when_to_use}")
        print(f"    model={agent.model}, max_turns={agent.max_turns}")
        runner.run_agent(agent, "Review src/auth.ts for issues")

    # --- Demo 5: Show sidechain transcript ---
    print("\n--- 5. Sidechain transcript records ---")
    for agent_id, transcript in _recorder._transcripts.items():
        print(f"  Agent {agent_id[:8]}...: {len(transcript)} messages recorded")

    print("\n" + "=" * 70)
    print("Done. See SOURCE_ANALYSIS.md for annotated source walkthrough.")
    print("=" * 70)


if __name__ == "__main__":
    main()
