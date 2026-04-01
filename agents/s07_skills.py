#!/usr/bin/env python3
"""
Session 07 -- Skill System: Two-Layer Loading (Reimplementation)

Demonstrates how Claude Code discovers skills from multiple directories,
injects only lightweight summaries into the system prompt (Layer 1), and
loads the full skill body on demand when invoked (Layer 2).

Reference source files:
  - src/skills/loadSkillsDir.ts          (discovery, frontmatter, createSkillCommand)
  - src/tools/SkillTool/SkillTool.ts     (Skill tool: validate, permissions, call)
  - src/tools/SkillTool/prompt.ts        (budget-aware listing for system prompt)
  - src/skills/bundledSkills.ts          (registerBundledSkill / getBundledSkills)
  - src/skills/mcpSkillBuilders.ts       (cycle-breaking registry for MCP skills)
"""

from __future__ import annotations

import os
import textwrap
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional


# ---------------------------------------------------------------------------
# 1. Skill Definition (mirrors Command type in src/types/command.ts)
# ---------------------------------------------------------------------------

@dataclass
class SkillDefinition:
    """
    Represents a discovered skill.  At discovery time only the frontmatter
    fields are populated -- the full markdown body stays on disk until
    invocation.

    Real code: createSkillCommand() in src/skills/loadSkillsDir.ts
    """

    name: str
    description: str
    when_to_use: Optional[str] = None
    allowed_tools: list[str] = field(default_factory=list)
    model: Optional[str] = None
    execution_context: str = "inline"  # 'inline' or 'fork'
    user_invocable: bool = True
    argument_hint: Optional[str] = None
    shell: Optional[str] = None
    source: str = "skills"       # 'skills', 'bundled', 'mcp', 'commands_DEPRECATED'
    loaded_from: str = "skills"
    skill_dir: Optional[str] = None  # base directory for ${CLAUDE_SKILL_DIR}
    file_path: Optional[str] = None  # absolute path to SKILL.md

    # The body is NOT loaded at discovery time -- this is the Layer 2 secret.
    _body_cache: Optional[str] = field(default=None, repr=False)


# ---------------------------------------------------------------------------
# 2. YAML Frontmatter Parser (mirrors src/utils/frontmatterParser.ts)
# ---------------------------------------------------------------------------

def parse_frontmatter(content: str) -> tuple[dict, str]:
    """
    Extract YAML frontmatter from a markdown file.

    Real code: parseFrontmatter() in src/utils/frontmatterParser.ts
    """
    if not content.startswith("---"):
        return {}, content

    end_idx = content.find("\n---", 3)
    if end_idx == -1:
        return {}, content

    yaml_block = content[3:end_idx].strip()
    body = content[end_idx + 4:].strip()

    # Simple YAML key-value parser (real code uses a proper YAML library)
    frontmatter: dict = {}
    for line in yaml_block.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if ":" in line:
            key, _, value = line.partition(":")
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            # Handle comma-separated lists (e.g., allowed-tools: Bash, Read, Write)
            if "," in value and key in ("allowed-tools", "paths"):
                frontmatter[key] = [v.strip() for v in value.split(",")]
            else:
                frontmatter[key] = value
    return frontmatter, body


def extract_description_from_markdown(body: str, fallback_label: str = "Skill") -> str:
    """
    If no description in frontmatter, use the first non-heading line.

    Real code: extractDescriptionFromMarkdown() in src/utils/markdownConfigLoader.ts
    """
    for line in body.splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            return line[:120]
    return f"{fallback_label} (no description)"


# ---------------------------------------------------------------------------
# 3. Skill Loader (mirrors getSkillDirCommands in src/skills/loadSkillsDir.ts)
# ---------------------------------------------------------------------------

class SkillLoader:
    """
    Discovers skills from multiple directories and deduplicates them.

    Real code: getSkillDirCommands() + loadSkillsFromSkillsDir() in
    src/skills/loadSkillsDir.ts
    """

    def __init__(self, search_dirs: list[str]):
        """
        search_dirs: list of directory paths to scan for skills.
        Each should contain subdirectories with SKILL.md files, e.g.:
            search_dirs = ["~/.claude/skills", ".claude/skills"]
        """
        self.search_dirs = search_dirs
        self._skills: dict[str, SkillDefinition] = {}
        self._bundled: list[SkillDefinition] = []

    # -- Discovery (Layer 1) -----------------------------------------------

    def discover_all(self) -> list[SkillDefinition]:
        """
        Scan all directories, parse frontmatter, build SkillDefinitions.
        Does NOT read the full body -- only frontmatter metadata.

        Real code: getSkillDirCommands() loads from managed, user, project,
        additional, and legacy dirs in parallel (Promise.all).
        """
        seen_paths: set[str] = set()  # dedup by resolved path (like realpath)

        for search_dir in self.search_dirs:
            search_path = Path(search_dir).expanduser()
            if not search_path.is_dir():
                continue

            for entry in sorted(search_path.iterdir()):
                if not entry.is_dir():
                    continue  # /skills/ only supports directory format

                skill_file = entry / "SKILL.md"
                if not skill_file.exists():
                    continue

                # Dedup by resolved path (handles symlinks)
                # Real code: getFileIdentity() uses realpath()
                resolved = str(skill_file.resolve())
                if resolved in seen_paths:
                    continue
                seen_paths.add(resolved)

                # Parse frontmatter ONLY -- do not store full body yet
                raw = skill_file.read_text(encoding="utf-8")
                frontmatter, _body = parse_frontmatter(raw)

                skill_name = entry.name
                description = frontmatter.get("description") or \
                    extract_description_from_markdown(_body)

                allowed_tools_raw = frontmatter.get("allowed-tools", "")
                if isinstance(allowed_tools_raw, list):
                    allowed_tools = allowed_tools_raw
                elif allowed_tools_raw:
                    allowed_tools = [t.strip() for t in allowed_tools_raw.split(",")]
                else:
                    allowed_tools = []

                skill = SkillDefinition(
                    name=skill_name,
                    description=description,
                    when_to_use=frontmatter.get("when_to_use"),
                    allowed_tools=allowed_tools,
                    model=frontmatter.get("model"),
                    execution_context=frontmatter.get("context", "inline"),
                    user_invocable=frontmatter.get("user-invocable", "true") != "false",
                    argument_hint=frontmatter.get("argument-hint"),
                    shell=frontmatter.get("shell"),
                    source="skills",
                    loaded_from="skills",
                    skill_dir=str(entry),
                    file_path=str(skill_file),
                )
                self._skills[skill_name] = skill

        # Also include bundled skills
        for bundled in self._bundled:
            if bundled.name not in self._skills:
                self._skills[bundled.name] = bundled

        return list(self._skills.values())

    def register_bundled_skill(self, skill: SkillDefinition) -> None:
        """
        Register a bundled skill (compiled into the binary).

        Real code: registerBundledSkill() in src/skills/bundledSkills.ts
        """
        skill.source = "bundled"
        skill.loaded_from = "bundled"
        self._bundled.append(skill)

    # -- System Prompt Summaries (Layer 1) ---------------------------------

    def get_skill_summaries(self, budget_chars: int = 8000) -> str:
        """
        Generate the skill listing for injection into the system prompt.
        Only name + description + whenToUse are included.

        Budget: 1% of context window (default 8000 chars = 1% of 200k * 4 chars/token).
        Bundled skills are never truncated.

        Real code: formatCommandsWithinBudget() in src/tools/SkillTool/prompt.ts
        """
        MAX_DESC_CHARS = 250  # per-entry cap from real code

        skills = list(self._skills.values())
        if not skills:
            return ""

        # Build full entries
        entries: list[tuple[SkillDefinition, str]] = []
        for s in skills:
            desc = s.description
            if s.when_to_use:
                desc = f"{desc} - {s.when_to_use}"
            if len(desc) > MAX_DESC_CHARS:
                desc = desc[: MAX_DESC_CHARS - 1] + "\u2026"
            entries.append((s, f"- {s.name}: {desc}"))

        total_chars = sum(len(text) for _, text in entries)
        if total_chars <= budget_chars:
            return "\n".join(text for _, text in entries)

        # Budget exceeded -- truncate non-bundled descriptions
        # Real code: bundled skills keep full descriptions, others are trimmed
        bundled_chars = sum(
            len(text) for s, text in entries if s.source == "bundled"
        )
        remaining_budget = budget_chars - bundled_chars
        non_bundled = [(s, text) for s, text in entries if s.source != "bundled"]

        if not non_bundled:
            return "\n".join(text for _, text in entries)

        name_overhead = sum(len(s.name) + 4 for s, _ in non_bundled)
        available = remaining_budget - name_overhead
        max_desc = max(available // len(non_bundled), 0)

        lines: list[str] = []
        for s, full_text in entries:
            if s.source == "bundled":
                lines.append(full_text)
            elif max_desc < 20:
                lines.append(f"- {s.name}")  # names only
            else:
                desc = s.description[:max_desc]
                lines.append(f"- {s.name}: {desc}")
        return "\n".join(lines)

    # -- On-Demand Body Loading (Layer 2) ----------------------------------

    def load_skill_body(self, skill_name: str) -> Optional[str]:
        """
        Load the full markdown body of a skill.  This is called ONLY when
        the model invokes the skill via the Skill tool.

        Real code: getPromptForCommand() inside createSkillCommand()
        in src/skills/loadSkillsDir.ts
        """
        skill = self._skills.get(skill_name)
        if skill is None:
            return None

        # Check cache first
        if skill._body_cache is not None:
            return skill._body_cache

        if skill.file_path and os.path.exists(skill.file_path):
            raw = Path(skill.file_path).read_text(encoding="utf-8")
            _, body = parse_frontmatter(raw)

            # Prepend base directory if the skill has reference files
            # Real code: "Base directory for this skill: <dir>"
            if skill.skill_dir:
                body = f"Base directory for this skill: {skill.skill_dir}\n\n{body}"

            skill._body_cache = body
            return body

        # Bundled skills generate body dynamically
        if skill.source == "bundled" and skill._body_cache:
            return skill._body_cache

        return None


# ---------------------------------------------------------------------------
# 4. Skill Tool (mirrors src/tools/SkillTool/SkillTool.ts)
# ---------------------------------------------------------------------------

class SkillTool:
    """
    The tool the model calls to invoke a skill.

    Real code: SkillTool in src/tools/SkillTool/SkillTool.ts
    Input schema: { skill: string, args?: string }
    """

    def __init__(self, loader: SkillLoader):
        self.loader = loader

    def invoke(self, skill_name: str, args: str = "") -> dict:
        """
        Invoke a skill by name.  Mirrors SkillTool.call() which:
        1. Validates the skill exists and is a prompt type
        2. Checks permissions (deny/allow rules, safe-property auto-allow)
        3. Loads the full body (Layer 2)
        4. Runs inline (inject into conversation) or forked (sub-agent)

        Real code: SkillTool.call() in src/tools/SkillTool/SkillTool.ts
        """
        # Strip leading slash for compatibility
        # Real code: const commandName = trimmed.startsWith('/') ? trimmed.substring(1) : trimmed
        clean_name = skill_name.lstrip("/")

        skill = self.loader._skills.get(clean_name)
        if skill is None:
            return {"success": False, "error": f"Unknown skill: {clean_name}"}

        # Load the full body (Layer 2 -- on demand)
        body = self.loader.load_skill_body(clean_name)
        if body is None:
            return {"success": False, "error": f"Could not load body for: {clean_name}"}

        # Substitute $ARGUMENTS
        # Real code: substituteArguments(finalContent, args, true, argumentNames)
        if args:
            body = body.replace("$ARGUMENTS", args)

        # Branch on execution context
        if skill.execution_context == "fork":
            # Real code: executeForkedSkill() -> runAgent() sub-agent
            return {
                "success": True,
                "command_name": clean_name,
                "status": "forked",
                "result": f"[Sub-agent would execute with body ({len(body)} chars)]",
                "body_preview": body[:200],
            }
        else:
            # Real code: processPromptSlashCommand() -> inject as newMessages
            return {
                "success": True,
                "command_name": clean_name,
                "status": "inline",
                "allowed_tools": skill.allowed_tools,
                "model": skill.model,
                "body_preview": body[:200],
                "body_length": len(body),
            }


# ---------------------------------------------------------------------------
# 5. Demo Setup -- Create example skills on disk
# ---------------------------------------------------------------------------

DEMO_DIR = Path(__file__).parent / "skills"


def create_example_skills() -> None:
    """Create example skill directories for the demo."""

    skills = {
        "deploy": {
            "frontmatter": textwrap.dedent("""\
                ---
                description: Assists with deployment workflows
                when_to_use: When the user asks about deploying or releasing
                allowed-tools: Bash, Read
                context: fork
                shell: bash
                argument-hint: "<environment>"
                ---
            """),
            "body": textwrap.dedent("""\
                # Deploy Helper

                You are a deployment assistant. Help the user deploy to $ARGUMENTS.

                ## Steps
                1. Check current branch with `git branch --show-current`
                2. Run pre-deploy checks
                3. Execute deployment script
            """),
        },
        "review-pr": {
            "frontmatter": textwrap.dedent("""\
                ---
                description: Review a pull request for quality and correctness
                when_to_use: When the user asks to review a PR or code changes
                allowed-tools: Bash, Read, Grep
                model: sonnet
                ---
            """),
            "body": textwrap.dedent("""\
                # PR Review

                Review the pull request thoroughly.

                ## Checklist
                - Code correctness
                - Test coverage
                - Documentation
                - Security implications
            """),
        },
        "scaffold": {
            "frontmatter": textwrap.dedent("""\
                ---
                description: Generate boilerplate code for new components
                when_to_use: When creating new files, modules, or components from templates
                allowed-tools: Write, Bash
                ---
            """),
            "body": textwrap.dedent("""\
                # Scaffold Generator

                Create a new component based on the project's conventions.

                Use the project structure to determine the right patterns.
                Generate tests alongside the implementation.
            """),
        },
    }

    for skill_name, parts in skills.items():
        skill_dir = DEMO_DIR / skill_name
        skill_dir.mkdir(parents=True, exist_ok=True)
        skill_file = skill_dir / "SKILL.md"
        skill_file.write_text(parts["frontmatter"] + parts["body"], encoding="utf-8")

    print(f"  Created {len(skills)} example skills in {DEMO_DIR}/")


# ---------------------------------------------------------------------------
# 6. Demo -- Putting it all together
# ---------------------------------------------------------------------------

def main() -> None:
    print("=" * 70)
    print("Session 07: Skill System -- Two-Layer Loading")
    print("=" * 70)

    # -- Step 1: Create example skills on disk -----------------------------
    print("\n[1] Creating example skills on disk...")
    create_example_skills()

    # -- Step 2: Initialize loader with search directories -----------------
    print("\n[2] Initializing SkillLoader with search directories...")
    loader = SkillLoader(search_dirs=[str(DEMO_DIR)])

    # Register a bundled skill (like /verify or /remember in real code)
    # Real code: registerBundledSkill() in src/skills/bundledSkills.ts
    bundled_verify = SkillDefinition(
        name="verify",
        description="Verify code changes are correct by running tests and checks",
        when_to_use="When the user wants to validate their changes before committing",
        _body_cache="# Verify\n\nRun the project's test suite and lint checks.\n"
                    "Report any failures with suggested fixes.",
    )
    loader.register_bundled_skill(bundled_verify)

    # -- Step 3: Discover all skills (Layer 1) -----------------------------
    print("\n[3] Discovering skills (Layer 1 -- frontmatter only)...")
    skills = loader.discover_all()
    print(f"  Discovered {len(skills)} skills:")
    for s in skills:
        print(f"    - {s.name} (source={s.source}, context={s.execution_context})")

    # -- Step 4: Generate system prompt listing ----------------------------
    print("\n[4] Generating system prompt skill listing (Layer 1)...")
    print("  Budget: 8000 chars (1% of 200k context * 4 chars/token)")
    listing = loader.get_skill_summaries(budget_chars=8000)
    print(f"\n  --- Skill listing ({len(listing)} chars) ---")
    for line in listing.splitlines():
        print(f"  {line}")
    print("  --- end listing ---")

    # Show token estimation
    # Real code: estimateSkillFrontmatterTokens() in loadSkillsDir.ts
    estimated_tokens = len(listing) // 4  # rough: ~4 chars per token
    print(f"\n  Estimated tokens for listing: ~{estimated_tokens}")
    print("  (Full bodies would cost much more -- that is the Layer 2 savings)")

    # -- Step 5: Simulate tight budget -------------------------------------
    print("\n[5] Simulating tight budget (200 chars)...")
    tight_listing = loader.get_skill_summaries(budget_chars=200)
    print(f"\n  --- Tight listing ({len(tight_listing)} chars) ---")
    for line in tight_listing.splitlines():
        print(f"  {line}")
    print("  --- end listing ---")

    # -- Step 6: Invoke a skill (Layer 2) ----------------------------------
    print("\n[6] Invoking skill 'deploy' with args='production' (Layer 2)...")
    tool = SkillTool(loader)
    result = tool.invoke("deploy", args="production")
    print(f"  Result: {result}")

    # -- Step 7: Invoke inline skill ---------------------------------------
    print("\n[7] Invoking skill 'review-pr' (inline, Layer 2)...")
    result = tool.invoke("review-pr")
    print(f"  Result: {result}")

    # -- Step 8: Invoke bundled skill --------------------------------------
    print("\n[8] Invoking bundled skill 'verify' (Layer 2)...")
    result = tool.invoke("verify")
    print(f"  Result: {result}")

    # -- Step 9: Try unknown skill -----------------------------------------
    print("\n[9] Invoking unknown skill 'nonexistent'...")
    result = tool.invoke("nonexistent")
    print(f"  Result: {result}")

    # -- Step 10: Show the two-layer cost comparison -----------------------
    print("\n" + "=" * 70)
    print("TOKEN COST COMPARISON")
    print("=" * 70)
    total_body_chars = 0
    for s in skills:
        body = loader.load_skill_body(s.name)
        if body:
            total_body_chars += len(body)

    listing_tokens = len(listing) // 4
    body_tokens = total_body_chars // 4

    print(f"\n  Layer 1 (system prompt listing):  ~{listing_tokens:>5} tokens  (paid every turn)")
    print(f"  Layer 2 (all bodies combined):    ~{body_tokens:>5} tokens  (paid only on invocation)")
    print(f"  Savings if 0 skills invoked:       ~{body_tokens:>5} tokens saved")
    print(f"  Savings if 1 skill invoked:        ~{body_tokens - body_tokens // len(skills):>5} tokens saved")
    print(f"\n  With {len(skills)} skills, the two-layer approach saves")
    print(f"  ~{body_tokens} tokens per turn when no skills are invoked.")

    print("\n" + "=" * 70)
    print("Done.  See SOURCE_ANALYSIS.md for the full annotated walkthrough.")
    print("=" * 70)


if __name__ == "__main__":
    main()
