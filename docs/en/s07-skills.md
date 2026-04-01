# Session 07 -- Skills: Two-Layer Loading

s01 > s02 > s03 > s04 > s05 | s06 > **s07** > s08 > s09 > s10 | s11 > s12 > s13 > s14

---

> *"Pay for what you use, not for what you might use."*
>
> **Harness layer**: This session covers the skill system -- the mechanism that
> lets Claude Code discover, list, and execute user-defined behaviors. The
> two-layer loading pattern is a token optimization that saves thousands of
> tokens per turn.

---

## Problem

Skills are reusable prompt instructions that users package as markdown files.
A project might have 10, 20, or 50 skills. If you load every skill's full body
into the system prompt, you pay the token cost on every single turn -- even if
no skill is invoked. With 4 skills averaging 500 tokens each, that is 2,000
tokens wasted per turn. Scale to 20 skills and you are burning 10,000 tokens
every time the model speaks.

You need a system that:

- Tells the model what skills exist (so it knows when to invoke them)
- Does not pay the full body cost until a skill is actually needed
- Handles multiple search directories with deduplication
- Supports budget-aware truncation when there are many skills

## Solution

Claude Code uses **two-layer loading**:

- **Layer 1 (discovery)**: Parse only the YAML frontmatter from each
  `SKILL.md` file. Inject a short summary (~100 tokens per skill) into every
  system prompt. The model sees *what exists* at minimal cost.

- **Layer 2 (invocation)**: When the model calls the Skill tool, read the full
  markdown body (~2,000 tokens) from disk. The body is loaded once, cached, and
  injected into the conversation.

```
  System Prompt (every turn)           Skill Tool (on demand)
  +--------------------------+        +---------------------------+
  | Available skills:        |        | User says "/deploy prod"  |
  | - deploy: Assists with   |        |                           |
  |   deployment workflows   | -----> | Load SKILL.md full body   |
  | - review-pr: Review a   |        | (~2000 tokens)            |
  |   pull request           |        | Inject into conversation  |
  | - scaffold: Generate     |        | Execute inline or forked  |
  |   boilerplate code       |        +---------------------------+
  +--------------------------+
  ~100 tokens per skill                ~2000 tokens, paid once
```

## How It Works

### Skill Definition

Each skill is a directory containing a `SKILL.md` file with YAML frontmatter.
The frontmatter carries all the metadata needed for Layer 1.

```python
# agents/s07_skills.py -- mirrors Command type in src/types/command.ts

@dataclass
class SkillDefinition:
    name: str
    description: str
    when_to_use: Optional[str] = None
    allowed_tools: list[str] = field(default_factory=list)
    model: Optional[str] = None
    execution_context: str = "inline"  # 'inline' or 'fork'
    user_invocable: bool = True
    argument_hint: Optional[str] = None
    source: str = "skills"       # 'skills', 'bundled', 'mcp'
    file_path: Optional[str] = None

    # The body is NOT loaded at discovery time -- this is the Layer 2 secret.
    _body_cache: Optional[str] = field(default=None, repr=False)
```

### Multi-Directory Discovery (Layer 1)

The loader scans multiple directories and deduplicates by resolved file path.
Only frontmatter is parsed -- the body stays on disk.

```python
# agents/s07_skills.py -- mirrors getSkillDirCommands() in loadSkillsDir.ts

class SkillLoader:
    def __init__(self, search_dirs: list[str]):
        self.search_dirs = search_dirs  # e.g. ["~/.claude/skills", ".claude/skills"]
        self._skills: dict[str, SkillDefinition] = {}

    def discover_all(self) -> list[SkillDefinition]:
        seen_paths: set[str] = set()

        for search_dir in self.search_dirs:
            search_path = Path(search_dir).expanduser()
            if not search_path.is_dir():
                continue

            for entry in sorted(search_path.iterdir()):
                if not entry.is_dir():
                    continue
                skill_file = entry / "SKILL.md"
                if not skill_file.exists():
                    continue

                # Dedup by resolved path (handles symlinks)
                resolved = str(skill_file.resolve())
                if resolved in seen_paths:
                    continue
                seen_paths.add(resolved)

                # Parse frontmatter ONLY -- do not store full body
                raw = skill_file.read_text(encoding="utf-8")
                frontmatter, _body = parse_frontmatter(raw)
                # ... build SkillDefinition from frontmatter fields
```

### Budget-Aware System Prompt Listing (Layer 1)

The listing is capped at 1% of the context window. Bundled skills keep their
full descriptions; user skills get truncated if the budget is tight.

```python
# agents/s07_skills.py -- mirrors formatCommandsWithinBudget() in prompt.ts

def get_skill_summaries(self, budget_chars: int = 8000) -> str:
    MAX_DESC_CHARS = 250  # per-entry cap

    entries = []
    for s in self._skills.values():
        desc = s.description
        if s.when_to_use:
            desc = f"{desc} - {s.when_to_use}"
        if len(desc) > MAX_DESC_CHARS:
            desc = desc[:MAX_DESC_CHARS - 1] + "..."
        entries.append((s, f"- {s.name}: {desc}"))

    total_chars = sum(len(text) for _, text in entries)
    if total_chars <= budget_chars:
        return "\n".join(text for _, text in entries)

    # Budget exceeded -- truncate non-bundled descriptions
    # Bundled skills keep full descriptions, others are trimmed
```

### On-Demand Body Loading (Layer 2)

When the Skill tool is invoked, the full body is read from disk and cached.

```python
# agents/s07_skills.py -- mirrors getPromptForCommand() in loadSkillsDir.ts

def load_skill_body(self, skill_name: str) -> Optional[str]:
    skill = self._skills.get(skill_name)
    if skill is None:
        return None

    # Check cache first
    if skill._body_cache is not None:
        return skill._body_cache

    if skill.file_path and os.path.exists(skill.file_path):
        raw = Path(skill.file_path).read_text(encoding="utf-8")
        _, body = parse_frontmatter(raw)

        # Prepend base directory for ${CLAUDE_SKILL_DIR} resolution
        if skill.skill_dir:
            body = f"Base directory for this skill: {skill.skill_dir}\n\n{body}"

        skill._body_cache = body
        return body
    return None
```

### The Skill Tool

The model invokes skills through a dedicated tool. It resolves the name,
loads the body, substitutes `$ARGUMENTS`, and executes inline or forked.

```python
# agents/s07_skills.py -- mirrors SkillTool.call() in SkillTool.ts

class SkillTool:
    def invoke(self, skill_name: str, args: str = "") -> dict:
        clean_name = skill_name.lstrip("/")
        skill = self.loader._skills.get(clean_name)
        if skill is None:
            return {"success": False, "error": f"Unknown skill: {clean_name}"}

        # Load the full body (Layer 2 -- on demand)
        body = self.loader.load_skill_body(clean_name)

        # Substitute $ARGUMENTS
        if args:
            body = body.replace("$ARGUMENTS", args)

        # Branch on execution context
        if skill.execution_context == "fork":
            return {"status": "forked", ...}  # Sub-agent execution
        else:
            return {"status": "inline", ...}  # Inject into conversation
```

## What Changed

| Component | Before | After |
|-----------|--------|-------|
| Skill loading | Full body in system prompt every turn | Frontmatter only; body on demand |
| Per-turn cost | ~2000 tokens per skill (all skills) | ~100 tokens per skill (summaries only) |
| 20 skills overhead | ~40,000 tokens/turn | ~2,000 tokens/turn |
| Discovery dirs | Single directory | Multi-directory with symlink deduplication |
| Budget control | None -- all skills always listed | 1% of context window budget with truncation |
| Bundled skills | Same as user skills | Protected from truncation |
| Argument passing | Not supported | `$ARGUMENTS` substitution in body |

## Try It

```bash
# Run the skills demo
python agents/s07_skills.py
```

The demo walks through:

1. **Creating example skills** on disk with YAML frontmatter
2. **Discovery** -- scanning directories, parsing only frontmatter
3. **System prompt listing** -- generating the budget-aware summary
4. **Tight budget test** -- what happens when 200 chars is all you have
5. **Invocation** -- loading the full body on demand (Layer 2)
6. **Token cost comparison** -- the savings from two-layer loading

Try adding your own skill:

```bash
mkdir -p agents/skills/my-skill
cat > agents/skills/my-skill/SKILL.md << 'EOF'
---
description: My custom skill for testing
allowed-tools: Bash, Read
---

# My Skill

Do something useful with $ARGUMENTS.
EOF
```

Then re-run the demo to see it discovered automatically.
