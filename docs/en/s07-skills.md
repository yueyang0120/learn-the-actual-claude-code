# s07: Skills

`s01 > s02 > s03 > s04 > s05 | s06 > [ s07 ] s08 > s09 > s10 | s11 > s12 > s13 > s14`

> "Pay for what you use, not for what you might use."

## Problem

Skills are reusable prompt instructions packaged as markdown files. A project might have 20 skills. Loading every skill's full body into the system prompt costs ~2,000 tokens each -- that is 40,000 tokens wasted per turn when no skill is actually invoked.

## Solution

Claude Code uses two-layer loading: cheap summaries on every turn, full body only on demand.

```
  System Prompt (every turn)          Skill Tool (on demand)
  +-------------------------+        +---------------------------+
  | Available skills:       |        | User says "/deploy prod"  |
  | - deploy: Assists with  |        |                           |
  |   deployment workflows  | -----> | Load SKILL.md full body   |
  | - review-pr: Review a  |        | (~2000 tokens, once)      |
  |   pull request          |        | Inject into conversation  |
  +-------------------------+        +---------------------------+
  ~100 tokens per skill               ~2000 tokens, paid once
```

## How It Works

### Step 1: Skill definition

Each skill lives in a directory with a `SKILL.md` file. YAML frontmatter carries metadata; the markdown body carries the actual instructions. Source: `loadSkillsDir.ts`.

```python
# agents/s07_skills.py (simplified)

@dataclass
class SkillDefinition:
    name: str
    description: str
    allowed_tools: list[str] = field(default_factory=list)
    execution_context: str = "inline"  # 'inline' or 'fork'
    _body_cache: Optional[str] = None  # NOT loaded at discovery
```

### Step 2: Discovery (Layer 1)

The loader scans multiple directories, parses only frontmatter, and deduplicates by resolved file path. The body stays on disk.

```python
class SkillLoader:
    def discover_all(self):
        seen_paths = set()
        for search_dir in self.search_dirs:
            for entry in sorted(search_path.iterdir()):
                skill_file = entry / "SKILL.md"
                resolved = str(skill_file.resolve())
                if resolved in seen_paths:
                    continue
                seen_paths.add(resolved)
                frontmatter, _body = parse_frontmatter(skill_file.read_text())
                # Build SkillDefinition from frontmatter only
```

### Step 3: Budget-aware listing (Layer 1)

The listing injected into the system prompt is capped at 1% of the context window. Bundled skills keep full descriptions; user skills get truncated if tight. Source: `prompt.ts`.

```python
def get_skill_summaries(self, budget_chars=8000):
    entries = [f"- {s.name}: {s.description}" for s in self.skills]
    if total_chars(entries) <= budget_chars:
        return "\n".join(entries)
    # Truncate non-bundled descriptions to fit budget
```

### Step 4: On-demand body loading (Layer 2)

When the Skill tool is invoked, the full body is read from disk and cached. `$ARGUMENTS` is substituted. Source: `SkillTool.ts`.

```python
class SkillTool:
    def invoke(self, skill_name, args=""):
        body = self.loader.load_skill_body(skill_name)
        if args:
            body = body.replace("$ARGUMENTS", args)
        if skill.execution_context == "fork":
            return {"status": "forked"}   # runs as sub-agent
        return {"status": "inline"}       # injected into conversation
```

## What Changed

| Component | Before (s06) | After (s07) |
|-----------|-------------|-------------|
| Skill loading | N/A | Frontmatter only; body on demand |
| Per-turn cost | N/A | ~100 tokens per skill (summaries) |
| 20 skills overhead | N/A | ~2,000 tokens/turn instead of ~40,000 |
| Discovery | N/A | Multi-directory with symlink dedup |
| Budget control | N/A | 1% of context window with truncation |
| Argument passing | N/A | `$ARGUMENTS` substitution in body |

## Try It

```bash
cd learn-the-actual-claude-code
python agents/s07_skills.py
```

The demo creates example skills on disk, discovers them, generates budget-aware listings, and invokes skills with argument substitution.

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
