# 第 4 章: 系统提示词

Agent 循环 (第 1 章) 向 API 发送消息; 工具系统 (第 2-3 章) 定义模型能做什么。System prompt 定义模型知道什么 — 它的身份, 规则, 环境, 可用工具, 以及用户提供的指令。本章描述 Claude Code 如何从数十个来源将 system prompt 组装为一个 cache 优化的字符串。

## 问题

AI 编程助手的 system prompt 必须同时服务于多个目的。它要建立模型的身份和行为边界。它要描述每个可用工具, 包括每次会话动态注册的 MCP 工具。它要传达用户的项目特定指令 (编码风格, 禁止模式, 首选库)。它要通信环境事实 (操作系统, 当前目录, git 分支)。而且它要在有限的 token 预算内完成这一切, 同时最大化跨请求的 cache 命中率。

这些需求相互冲突。工具描述在会话内是静态的, 但环境事实随每条命令变化。用户指令在项目内稳定, 但跨项目不同。如果静态和动态内容交错排列, 动态部分的任何变化都会使整个 prompt cache 失效, 迫使 API 重新处理数千个未变化的 token。

Anthropic API 的 prompt caching 基于前缀: cache key 从 prompt 开头计算, 只有前缀完全匹配才能命中。prompt 中间的一个字符变化就会使其后所有内容的 cache 失效。因此, prompt 组装系统必须按易变性分离内容: 稳定内容在前 (积极 cache), 易变内容在后 (因短且位于尾部, 重新处理成本低)。

## Claude Code 的解法

### 组装函数

System prompt 组装位于 `prompts.ts` (914 行)。顶层函数从多个来源收集段落, 拼接为单个字符串。尽管文件较长, 逻辑很直接: 收集段落, 按可缓存性排序, 用边界标记连接。

### 双层段落架构

System prompt 的每个段落通过两个构造器之一创建:

```typescript
// src/prompts.ts — 段落构造器 (概念性)
systemPromptSection(title: string, content: string)
DANGEROUS_uncachedSystemPromptSection(title: string, content: string)
```

命名是刻意的。`systemPromptSection()` 产出缓存段落 — 在会话内各请求间一致的内容。`DANGEROUS_uncachedSystemPromptSection()` 产出易变段落, 可能在请求间变化。"DANGEROUS" 前缀是代码气味信号: 添加非缓存段落会损害 cache 命中率, 因此每次这样的添加都应在代码审查中被审视。

组装后的 prompt 结构如下:

```
[缓存段落 1]
[缓存段落 2]
...
[缓存段落 N]
SYSTEM_PROMPT_DYNAMIC_BOUNDARY
[非缓存段落 1]
[非缓存段落 2]
...
```

`SYSTEM_PROMPT_DYNAMIC_BOUNDARY` 是插入 prompt 文本的字面字符串标记。它告诉 API client 在何处设置 cache 断点: 边界之上的所有内容带有 cache-control header 标记为应缓存; 边界之下的不缓存。这单个架构决策决定了整个系统的 cache 经济性。

实际中, 缓存前缀通常占 system prompt token 总量的 80-90%。动态尾部很小 — 通常几百 token 的环境信息和 CLAUDE.md 内容。这意味着绝大多数请求中, 80-90% 的 system prompt 处理成本被 caching 消除。

### 静态 (缓存) 段落

Cache 边界之上的段落在整个会话内稳定, 仅在 Claude Code 更新或启用工具集变化时才改变。

**1. 身份。** 模型名称, 版本和核心行为框架。几句话建立这是 Claude Code, 一个在终端中运行的 AI 编程助手。这个段落刻意简短 — 它的存在是为了锚定模型的自我认知, 而非提供详细指令。保持简短意味着即使跨 Claude Code 版本也很少变化。

**2. 系统规则。** 模型必须和不可做之事的硬约束。包括安全边界 (未经明确许可不执行可能损坏主机系统的命令), 输出格式规则 (代码使用 markdown, 回复保持简洁), 交互协议 (需求模糊时请求澄清而非猜测)。规则段落是最长的静态段落之一, 通常数百 token。规则以指令而非建议形式措辞, 因为模型对显式指令的遵循比隐式规范更可靠。

**3. 工具使用指南。** 如何有效使用工具的通用指令, 与单个工具文档不同。例如: 编辑前先读文件, 使用绝对路径而非相对路径, 优先有针对性搜索而非读取整个文件, 写入前检查文件是否存在。这些跨工具的最佳实践编码于此, 减少了每个工具在各自 prompt 中重复通用模式的需要。

**4. 语气指导。** 沟通风格指令。简洁。技术性。不加不必要的开场白。不重复用户的问题。直接提供代码而非描述要写什么代码。不为错误道歉 — 修复它们。这些指令使模型输出符合开发者期望。

**5. 单个工具 prompt。** 组装的工具池中每个工具通过第 2 章描述的 `.prompt()` 方法贡献自己的 prompt 文本。Bash 工具可能贡献一段关于超时行为, 后台进程用法和退出码报告方式的说明。FileRead 工具可能说明其行号格式和如何请求特定行范围。Grep 工具可能描述正则语法和可用标志。

因为工具池按字母序排列 (第 2 章), 这个段落的内容对于给定的启用工具集是确定性的。如果 Bash 始终排第一, WebSearch 始终排最后, token 序列跨请求完全一致, 这对前缀缓存至关重要。添加或删除工具 (如连接 MCP server) 会改变此段落并使 cache 失效 — 但工具集变更在会话内不频繁, 通常最多发生一次 (在启动时)。

### 动态 (每请求) 段落

Cache 边界之下, 可能在请求间变化的段落:

**1. CLAUDE.md 层级。** 用户提供的指令文件, 从最多四个层级加载:

- **Managed**: `~/.claude/CLAUDE.md` 中由 Claude Code 自身管理的部分 — 自动生成的项目结构摘要, 常见模式, 学习到的偏好。
- **User**: `~/.claude/CLAUDE.md` — 适用于所有项目的每用户指令。开发者可能放置个人偏好: "示例中使用 vim 键绑定", "偏好函数式风格"。
- **Project**: `<project-root>/CLAUDE.md` — 项目级指令, 纳入版本控制。全团队可见。典型内容: 编码标准, 测试约定, 架构说明。
- **Local**: `<project-root>/.claude/CLAUDE.md` — 不纳入版本控制的本地覆盖 (`.claude/` 目录通常在 `.gitignore` 中)。适合不应强加给团队的个人偏好或实验性指令。

文件按此顺序加载。指令冲突时, 后者覆盖前者 — local 覆盖 project, project 覆盖 user, user 覆盖 managed。

优先级匹配每个层级的特异性: 最特定的上下文 (local) 胜出。

四个层级的内容被拼接为 system prompt 的单个段落, 带有清晰的头部标识各指令来自哪个层级。这种透明性让模型 (以及用户, 如果检查 prompt) 理解每条指令的出处。

**2. MEMORY.md。** 持久记忆文件 (`~/.claude/MEMORY.md`) 存储模型跨会话学习的事实。例如: "此项目使用 pnpm 而非 npm", "用户偏好 const 而非 let", "开发环境中 API server 运行在 3001 端口"。模型可以在会话中写入这个文件 (通过专用工具或作为回复处理的一部分), 内容在 prompt 组装时被加载。

MEMORY.md 受硬截断限制: 200 行或 25 KB, 以先触及的为准。截断从文件尾部应用, 保留最旧的条目。理由: 存活了多次会话的旧条目可能比新增条目更重要。硬上限防止增长的记忆文件消耗 context window 中越来越大的份额, 逐渐降低编码任务的性能。一个经过数月使用增长到 500 行的记忆文件会消耗数千 token — 这些 context 本可用于代码, 工具结果或对话历史。

与 CLAUDE.md 文件不同, MEMORY.md 不按主题或层级组织。它是按时间顺序累积的扁平事实列表。需要更广泛持久上下文的用户更适合使用 CLAUDE.md 文件, 后者可以手动编辑, 按主题组织, 并分布在四层层级中。

**3. 环境信息。** 结构化块, 包含:

- 当前工作目录 (绝对路径)
- Git 状态: 当前分支, 工作树是否 dirty, staged/unstaged 变更数
- 平台: 操作系统名称和版本
- Shell: 用户默认 shell
- 当前模型名称
- 知识截止日期

这个段落在用户切换目录, 切换 git 分支, 提交或暂存文件时变化。相对较小 (通常 50-100 token), 每次请求重新处理, 成本很低。

环境信息对工具使用至关重要: 模型需要知道当前目录来构造正确的文件路径, 需要 git 分支来避免意外向 main 提交, 需要平台来生成平台适配的 shell 命令。

没有这个段落, 模型需要在每轮开始时运行诊断工具 (`pwd`, `git status`), 浪费 tool call 和延迟来获取运行时已经掌握的信息。

**4. MCP 指令。** 如果有 MCP server 连接, 其服务端提供的指令被包含于此。MCP server 可以声明描述其工具预期用途, 限制和约定的指令。因为 MCP server 可在会话中连接和断开 (如数据库 server 重启, 或用户通过 `mcp add` 添加新 server), 这部分内容本质上是易变的, 必须位于非缓存段落。MCP 指令段落通常较小 (每个 server 100-300 token), 但当多个 server 同时连接时可能显著增长。

### CLAUDE.md 的 Include 指令

CLAUDE.md 文件支持 `@include` 指令, 从其他文件引入内容:

```markdown
# 项目指令

@include ./docs/coding-standards.md
@include ./docs/api-conventions.md
```

这允许项目指令模块化。大型 monorepo 可能为前端, 后端和基础设施子系统分别维护指令文件, 根 CLAUDE.md 只包含与当前工作目录相关的子集。微服务项目可能有一个共享的通用约定指令文件, 由每个服务自己的 CLAUDE.md 引用。

Include 解析在 prompt 组装时发生 — 被引用的文件从磁盘读取, 内容在进入 prompt 前拼接到 CLAUDE.md 内容中。路径相对于包含 CLAUDE.md 文件的目录解析, 而非相对于当前工作目录。这确保 include 在项目内任何位置启动 Claude Code 时都能正确工作。

被引用的文件受与父 CLAUDE.md 相同的大小限制。如果 include 解析后的总内容超出每层上限, 它会被截断。支持嵌套 include: 被引用文件本身如果包含 `@include` 指令, 会递归解析 (受循环引用保护)。循环引用通过跟踪 include 链检测, 以警告打断循环而非导致无限递归。

### SIMPLE 模式

当完整 system prompt 不必要或适得其反时, `SIMPLE` 模式剥离大部分指导段落。仅保留身份段落和基本安全规则, 省略工具指南, 语气指令和大部分 CLAUDE.md 内容。

适用场景:

- 一次性问题, 大 system prompt 的开销与任务复杂度不匹配。
- 自动化流水线, Claude Code 用作简单命令执行器, 不需要行为指导。
- 调试, 最小 prompt 帮助隔离异常模型行为的原因。

Token 节省显著: 完整 system prompt 可能 5,000-8,000 token, SIMPLE 模式可能 500-1,000。对于高频自动化用例, 这一缩减直接转化为更低的 API 成本。

### 工具注入自己的 Prompt

如第 2 章所述, 每个工具通过 `.prompt()` 方法提供自己的 prompt 文本。组装期间, 组装器遍历工具池并拼接每个工具的 prompt 贡献:

```typescript
// src/prompts.ts — 工具 prompt 注入 (概念性)
for (const tool of toolPool) {
  const toolPrompt = tool.prompt()
  if (toolPrompt) {
    sections.push(systemPromptSection(tool.name, toolPrompt))
  }
}
```

因为工具 prompt 来自工具自身, 添加新工具自动添加其文档, 删除工具自动删除其文档, 修改行为和更新 prompt 发生在同一次代码变更中。

共置防止了在工具描述与工具实现分开维护的系统中常见的文档漂移。无需额外的注册步骤。

工具 prompt 被放在 system prompt 的缓存段落中。这是正确的, 因为工具集在会话内的请求间不变 (MCP 工具变更触发 prompt 重新组装, 有效开启新的 cache epoch)。

## 关键设计决策

**Cache 边界作为显式标记。** 不依赖 API 推断哪些部分可缓存, Claude Code 放置显式边界字符串。这给予系统对 cache 行为的精确控制, 使代码中边界的位置清晰可见。将段落从边界上方移到下方 (或反之) 是一行修改, cache 影响显而易见。显式性也使开发过程中测量 cache 比率 (缓存 token / 总 token) 变得容易。

**四层 CLAUDE.md 层级。** managed/user/project/local 的分层镜像了 git (`system` > `global` > `local`) 和 VS Code (default > user > workspace) 等工具中的配置系统。它提供了自然的升级路径: 组织级约定在 managed 层, 个人偏好在 user 层, 项目标准在 project 层, 本地实验在 local 层。更特定的指令覆盖更通用的, 符合开发者对配置优先级的直觉。

**MEMORY.md 截断。** 200 行 / 25 KB 上限是硬限制, 不是建议。没有它, 一个在多次会话中积极写入 MEMORY.md 的模型可能积累一个占据 context window 相当部分的文件, 降低实际任务的性能。上限也是记忆质量的强制函数: 当文件接近限制时, 旧的低价值条目自然被更新更相关的条目取代。截断边界同时也是记忆质量的有用施压机制。

**工具 prompt 按名称排序。** 字母序排列是 cache 优化, 没有语义意义 — 模型不关心工具描述出现的顺序。但 API 的 prompt caching 基于前缀: 相同的前缀共享 cache 条目。如果工具排序在请求间变化 (例如因为非确定性的集合或 hash map 迭代), 每次请求都会 cache miss。字母序排列以零成本消除了这一 cache 失效源。同一原则适用于嵌入在缓存段落中的任何列表: 确定性排序是有效前缀缓存的前提。

**DANGEROUS_ 前缀约定。** 将非缓存段落构造器命名以 DANGEROUS_ 前缀是社会-技术机制。它在类型层面不强制任何东西, 但确保每次代码审查中新增非缓存段落都会触发关于其必要性的讨论 — 内容是否真正需要是易变的。命名约定将成本模型嵌入了 API 表面: 添加非缓存段落很容易, 但名字迫使作者承认正在降低 cache 性能。大多数内容不需要是易变的, 命名压力帮助将其保留在缓存段落中。

## 实际体验

开发者在项目目录中启动 Claude Code 时, system prompt 被组装一次。静态部分 — 身份, 规则, 工具描述 — 对于运行相同版本 Claude Code 且启用相同工具的所有用户都是一致的, API 可以在全局所有用户间从 cache 提供服务。动态部分 — CLAUDE.md 内容, 环境信息, 记忆 — 很小 (通常几百 token), 在每次请求时廉价地重新处理。

如果项目有 `CLAUDE.md` 文件, 包含 "TypeScript 中始终使用单引号" 或 "编辑 src/ 中的文件后运行 `npm test`" 之类的指令, 模型在每轮都能看到并遵循。用户个人偏好在 `~/.claude/CLAUDE.md` 中同样出现, 但冲突时项目级指令优先。

当用户在会话中连接 MCP server (如 `mcp add github`), prompt 被重新组装: 新工具的 prompt 文本加入缓存段落, MCP 指令加入动态段落, 新的 cache epoch 开始。后续请求缓存更新后的前缀。

效果是 system prompt 感觉个性化且有上下文感知, 但传输成本低, 因为大部分被 cache。典型会话每次请求处理 6,000 缓存 token 和 400 非缓存 token — 94% 的 cache 命中率, 直接降低 API 延迟和成本。

System prompt 组装也是确定性的: 给定相同的 Claude Code 版本, 工具集, CLAUDE.md 文件和环境状态, 产出相同的 prompt。这种确定性对调试很重要 — 如果用户报告异常模型行为, prompt 可以从已知输入精确重建。

## 总结

- System prompt 分为缓存 (静态) 和非缓存 (易变) 段落, 由显式 `SYSTEM_PROMPT_DYNAMIC_BOUNDARY` 标记分隔, 控制前缀式 prompt caching。通常 80-90% 的 prompt token 在缓存前缀中。
- 静态段落包含身份, 系统规则, 语气指导, 工具使用指南和按字母序排列的单个工具 prompt。动态段落包含 CLAUDE.md 文件, MEMORY.md, 环境信息和 MCP 指令。
- CLAUDE.md 遵循四层层级 (managed, user, project, local), 后者覆盖前者。`@include` 指令支持模块化指令, 具有递归解析和循环引用检测。
- MEMORY.md 硬上限为 200 行 / 25 KB, 防止跨会话的无限 context 消耗。截断保留最旧 (最持久) 的条目。
- 工具 prompt 由工具自身通过 `.prompt()` 注入, 确保文档与实现共置。`DANGEROUS_uncachedSystemPromptSection` 命名约定迫使贡献者将内容保持在缓存段落, 除非易变性确实必要。
