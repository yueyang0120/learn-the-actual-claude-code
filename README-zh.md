[English](./README.md) | [中文](./README-zh.md)

# 学习真正的 Claude Code

一个生产级 AI 编程 Agent 到底是怎么工作的? 这个仓库通过追踪 Claude Code 的真实 TypeScript 源码来回答这个问题, 然后用 Python 重建每个子系统。

十四章, 每章拆解一个子系统 — Agent 循环、工具调度、权限引擎 — 讲清楚它解决什么工程问题, 用真实代码（带文件路径和行号）走一遍实现, 最后给出可运行的 Python 版本。

## 快速开始

```bash
git clone https://github.com/yueyang0120/learn-the-actual-claude-code.git
cd learn-the-actual-claude-code
pip install -r requirements.txt
cp .env.example .env  # 填入你的 ANTHROPIC_API_KEY

python agents/s01_agent_loop.py       # 运行任意一章
python agents/s_full.py               # 运行合并版 Agent
```

## 目录

### 基础篇: 核心引擎

| # | 章节 | 内容 |
|---|------|------|
| 01 | [Agent 循环](docs/zh/01-the-agent-loop.md) | Claude Code 核心的 streaming generator — 为什么 yield 事件而不是返回字符串, state machine 如何驱动循环, 工具如何在模型 streaming 期间执行 |
| 02 | [工具系统](docs/zh/02-tool-system.md) | 30+ 字段的 Tool 接口, 依赖输入的行为标记, feature gate 注册, 工具池排序保证 cache 稳定 |
| 03 | [工具编排](docs/zh/03-tool-orchestration.md) | 多个工具调用如何分区为并发和串行批次, 有界并行, 单个工具的 13 步执行流水线 |
| 04 | [系统提示词](docs/zh/04-system-prompt.md) | 静态与动态 prompt 分区, cache 边界标记, CLAUDE.md 层级, ~100 个 prompt 片段如何组装成一次 API 调用 |
| 05 | [权限系统](docs/zh/05-permissions.md) | 基于规则的访问控制, 4 个来源, 模式匹配, bash 命令分类器, 拒绝熔断器 |

### 进阶篇: 能力扩展

| # | 章节 | 内容 |
|---|------|------|
| 06 | [上下文压缩](docs/zh/06-context-compaction.md) | 四层压缩级联 — micro-compact, session memory, LLM 摘要, 手动 — 带阈值、渐进警告和熔断器 |
| 07 | [Skills](docs/zh/07-skills.md) | 两层加载: prompt 中 ~100 token 摘要, 调用时才加载完整内容。Frontmatter 解析, 预算感知列表, 条件激活 |
| 08 | [子 Agent](docs/zh/08-subagents.md) | Fork 出隔离的 Agent 并共享父级 prompt cache。CacheSafeParams, sidechain transcript, 内置 Agent 类型 |
| 09 | [任务系统](docs/zh/09-task-system.md) | 7 种任务类型, DAG 依赖图, append-only 磁盘输出, 基于 offset 的流式读取, 后台执行 |
| 10 | [Hooks](docs/zh/10-hooks.md) | 27 种事件类型, 基于 shell 的可扩展性, 结构化 JSON I/O, matcher 模式, session 级 hook 生命周期 |

### 高级篇: 生产级模式

| # | 章节 | 内容 |
|---|------|------|
| 11 | [MCP 集成](docs/zh/11-mcp.md) | 三种传输协议, 工具名命名空间, 输出截断, prompt 转 skill, 连接生命周期管理 |
| 12 | [状态管理](docs/zh/12-state-management.md) | 35 行的类 Zustand store, ~100 字段 AppState, 副作用 reactor, 消息规范化流水线, bootstrap/runtime 状态分离 |
| 13 | [团队与集群](docs/zh/13-teams.md) | 后端无关的 teammate 执行, 基于文件的 mailbox IPC, coordinator 模式, 关闭协议 |
| 14 | [工作树隔离](docs/zh/14-worktrees.md) | Git worktree 创建与 slug 校验, 设置传播, 变更检测, 安全清理 |

## 架构概览

```
+-----------------------------------------------+
|            cli.tsx (引导启动)                   |
|  版本检查 → feature gate → 快速路径            |
+----------------------+------------------------+
                       |
+----------------------v------------------------+
|            main.tsx (完整初始化)                |
|  配置, 认证, 工具, MCP, 插件, skills           |
+----------------------+------------------------+
                       |
+----------------------v-------------------------------+
|                 QueryEngine.ts                        |
|  编排: query 循环, 工具执行, 上下文压缩              |
+------+----------+----------+-----------+-------------+
       |          |          |           |
+------v---+ +---v------+ +-v-----+ +--v-----------+
| query.ts | | compact/ | | state | | permissions  |
| Agent    | | auto/    | | store | | rules/       |
| 循环     | | micro/   | | msg   | | classifier   |
| 流式     | | session  | | types | | patterns     |
+------+---+ +----------+ +-------+ +--------------+
       |
+------v-----------------------+
|   toolOrchestration.ts       |
| 分区 → 并发 /                |
| 串行 → hooks → 结果         |
+------+-----------------------+
       |
+------v-----------------------------------------------+
|                   工具注册表                           |
|                                                       |
| +------+ +------+ +-------+ +------+ +-------------+ |
| | Bash | | Read | | Agent | | Skill| | MCP Tools   | |
| | Edit | | Write| | Task* | | Hooks| | (动态)      | |
| | Glob | | Grep | | Plan  | | LSP  | | 3 种传输    | |
| +------+ +------+ +-------+ +------+ +-------------+ |
+-------------------------------------------------------+
```

## 项目结构

```
docs/en/           章节文本 (English)
docs/zh/           章节文本 (中文)
agents/            可运行的 Python 复现 (每章一个 + 合并版)
architecture/      系统架构参考和源文件索引
skills/            示例 skill 定义
lib/               共享 Python 库 (类型, 工具函数)
tests/             冒烟测试
```

## 前提条件

- Python 3.10+
- Anthropic API key
- 对 LLM 和 tool use 有基本了解

## 许可证

MIT — 源码归属和致谢见 [SOURCES.md](SOURCES.md)。
