# s07: Skills

`s01 > s02 > s03 > s04 > s05 | s06 > [ s07 ] s08 > s09 > s10 | s11 > s12 > s13 > s14`

> "为你真正用到的付费，不要为可能用到的付费。"

## 问题

Skills 就是打包成 markdown 文件的可复用 prompt 指令。一个项目可能有 20 个 skills。如果把每个 skill 的完整正文都塞进 system prompt，每个大约 2,000 token -- 那就是每轮白白浪费 40,000 token，而实际上可能一个 skill 都没被调用。

## 解决方案

Claude Code 用双层加载：每轮只放便宜的摘要，完整正文按需加载。

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

## 工作原理

### 第 1 步：Skill 定义

每个 skill 住在一个目录里，里面有个 `SKILL.md` 文件。YAML frontmatter 带元数据，markdown 正文带实际指令。源码参考：`loadSkillsDir.ts`。

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

### 第 2 步：发现（第 1 层）

加载器扫描多个目录，只解析 frontmatter，通过解析后的文件路径去重。正文留在磁盘上不动。

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

### 第 3 步：预算感知列表（第 1 层）

注入 system prompt 的列表有上限 -- 上下文窗口的 1%。内置 skills 保留完整描述，用户 skills 在预算紧张时会被截断。源码参考：`prompt.ts`。

```python
def get_skill_summaries(self, budget_chars=8000):
    entries = [f"- {s.name}: {s.description}" for s in self.skills]
    if total_chars(entries) <= budget_chars:
        return "\n".join(entries)
    # Truncate non-bundled descriptions to fit budget
```

### 第 4 步：按需加载正文（第 2 层）

Skill tool 被调用时，从磁盘读取完整正文并缓存。`$ARGUMENTS` 会被替换。源码参考：`SkillTool.ts`。

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

## 变更内容

| 组件 | 之前 (s06) | 之后 (s07) |
|------|-----------|-----------|
| Skill 加载 | 不存在 | 只读 frontmatter，正文按需加载 |
| 每轮成本 | 不存在 | 每个 skill 约 100 token（摘要） |
| 20 个 skills 开销 | 不存在 | 约 2,000 token/轮，而不是约 40,000 |
| 发现机制 | 不存在 | 多目录扫描，符号链接去重 |
| 预算控制 | 不存在 | 上下文窗口 1% 上限，超了就截断 |
| 参数传递 | 不存在 | 正文里的 `$ARGUMENTS` 替换 |

## 试一试

```bash
cd learn-the-actual-claude-code
python agents/s07_skills.py
```

演示会在磁盘上创建示例 skills，发现它们，生成预算感知列表，然后带参数调用 skill。

试着加一个你自己的 skill：

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

然后重新跑演示，你的 skill 会被自动发现。
