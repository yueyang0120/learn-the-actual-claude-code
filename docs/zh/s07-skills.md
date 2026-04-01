# Session 07 -- Skills：双层加载

s01 > s02 > s03 > s04 > s05 | s06 > **s07** > s08 > s09 > s10 | s11 > s12 > s13 > s14

---

> *"Pay for what you use, not for what you might use."*
> *"为你实际使用的付费，而不是为你可能使用的付费。"*
>
> **Harness 层**: 本节涵盖 skill 系统——让 Claude Code 发现、列出并执行用户自定义
> 行为的机制。双层加载模式是一种 token 优化策略，每轮可以节省数千 token。

---

## 问题

Skills 是用户打包为 markdown 文件的可复用提示指令。一个项目可能有 10、20 甚至
50 个 skills。如果你把每个 skill 的完整正文都加载到 system prompt 中，每一轮都要
为此支付 token 成本——即使没有任何 skill 被调用。4 个 skill 平均每个 500 token，
那就是每轮浪费 2,000 token。扩展到 20 个 skill，你每次模型发言都要烧掉 10,000
token。

你需要一个系统来：

- 告诉模型有哪些 skills 可用（让它知道何时调用）
- 在 skill 真正被需要之前不支付完整正文的成本
- 处理多个搜索目录并去重
- 当 skills 很多时支持预算感知的截断

## 解决方案

Claude Code 使用 **双层加载**：

- **第 1 层（发现）**: 仅解析每个 `SKILL.md` 文件的 YAML frontmatter。在每个
  system prompt 中注入简短摘要（每个 skill 约 100 token）。模型以最小代价了解
  *有哪些可用*。

- **第 2 层（调用）**: 当模型调用 Skill tool 时，从磁盘读取完整的 markdown 正文
  （约 2,000 token）。正文只加载一次，缓存后注入对话。

```
  System Prompt (every turn)           Skill Tool (on demand)
  +--------------------------+        +---------------------------+
  | Available skills:        |        | User says "/deploy prod"  |
  | - deploy: Assists with   |        |                           |
  |   deployment workflows   | -----> | Load SKILL.md full body   |
  | - review-pr: Review a   |        | (~2000 tokens)            |
  |   pull request           |        | Inject into conversation  |
  | - scaffold: Generate     |        +---------------------------+
  |   boilerplate code       |
  +--------------------------+
  ~100 tokens per skill                ~2000 tokens, paid once
```

## 工作原理

### Skill 定义

每个 skill 是一个包含 `SKILL.md` 文件的目录，文件带有 YAML frontmatter。
frontmatter 携带第 1 层所需的全部元数据。

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

### 多目录发现（第 1 层）

加载器扫描多个目录，并通过解析后的文件路径去重。只解析 frontmatter——正文留在
磁盘上。

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

### 预算感知的 System Prompt 列表（第 1 层）

列表的上限为上下文窗口的 1%。内置 skills 保留完整描述；用户 skills 在预算紧张时
会被截断。

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

### 按需加载正文（第 2 层）

当 Skill tool 被调用时，从磁盘读取完整正文并缓存。

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

### Skill Tool

模型通过一个专用 tool 来调用 skills。它解析名称，加载正文，替换 `$ARGUMENTS`，
然后内联执行或 fork 执行。

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

## 变化对比

| 组件 | 之前 | 之后 |
|------|------|------|
| Skill 加载 | 每轮将完整正文放入 system prompt | 仅 frontmatter；正文按需加载 |
| 每轮成本 | 每个 skill 约 2000 token（所有 skills） | 每个 skill 约 100 token（仅摘要） |
| 20 个 skills 的开销 | 约 40,000 token/轮 | 约 2,000 token/轮 |
| 发现目录 | 单一目录 | 多目录，带符号链接去重 |
| 预算控制 | 无——所有 skills 始终列出 | 上下文窗口 1% 的预算，带截断 |
| 内置 skills | 与用户 skills 相同 | 受保护，不会被截断 |
| 参数传递 | 不支持 | 正文中的 `$ARGUMENTS` 替换 |

## 试一试

```bash
# Run the skills demo
python agents/s07_skills.py
```

演示逐步展示：

1. **创建示例 skills** -- 在磁盘上创建带 YAML frontmatter 的文件
2. **发现** -- 扫描目录，仅解析 frontmatter
3. **System prompt 列表** -- 生成预算感知的摘要
4. **紧凑预算测试** -- 当只有 200 字符时会发生什么
5. **调用** -- 按需加载完整正文（第 2 层）
6. **Token 成本对比** -- 双层加载带来的节省

试着添加你自己的 skill：

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

然后重新运行演示，观察它被自动发现。
