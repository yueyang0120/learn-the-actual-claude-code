[English](./README.md) | [中文](./README-zh.md)

# 学习真正的 Claude Code

一个 14 节课的教程，从 **真实的 TypeScript 源码** 出发，教你 Claude Code 的 harness 工程 — 带注释的源码深度解读 + 简化的 Python 复现，可以直接运行和修改。

文档语言: [English](./docs/en/) | [中文](./docs/zh/)

## 灵感来源

本课程受到 [shareAI-lab/learn-claude-code](https://github.com/shareAI-lab/learn-claude-code) 的启发，它是一个优秀的渐进式课程，通过 Python 实践来教授 Agent harness 工程。我们沿用了他们开创的分节教学方法。

我们更进一步的地方在于：由于 [Claude Code 源码](https://github.com/AprilNEA/claude-code-source) 已经被公开恢复，我们可以将每一个架构细节追溯到真实 TypeScript 实现中的具体文件和行号。这意味着我们的复现反映的是真正的设计模式 — 流式生成器、并发/串行工具分区、prompt cache 共享等 — 而不是从外部行为推测的结果。

## 你会学到什么

Claude Code 是 Anthropic 构建的生产级 AI 编程 Agent。本课程带你走进它的架构 — Agent 循环、工具系统、权限引擎、上下文压缩等 — 以真实源码为依据。

每节课覆盖一个子系统，提供两个产物:

1. **源码分析** — 带注释的真实 TypeScript 代码解读，包含文件路径、行号引用和设计思路
2. **Python Agent** — 基于真实架构的简化可运行 Python 复现

## 选择你的路径

**想要构建 Agent?** 从学习指南和 Python 代码开始:

```
docs/zh/          -- 渐进式学习指南 (从这里开始)
agents/            -- 可运行的 Python 复现
```

**想要理解 Claude Code 的内部实现?** 深入源码分析:

```
source-analysis/   -- 带注释的 TypeScript 源码深度解读
```

## 快速开始

```bash
git clone https://github.com/yueyang0120/learn-the-actual-claude-code.git
cd learn-the-actual-claude-code
pip install -r requirements.txt
cp .env.example .env  # 添加你的 ANTHROPIC_API_KEY

# 运行任意一节课
python agents/s01_agent_loop.py

# 运行完整 Agent (所有课程合并)
python agents/s_full.py
```

## 14 节课程大纲

### 基础篇 (01-05): 核心引擎

| # | 课程 | 核心洞察 |
|---|------|----------|
| 01 | [Agent 循环](docs/zh/s01-the-agent-loop.md) | 循环是流式生成器，不是简单的 while 循环 |
| 02 | [工具系统](docs/zh/s02-tool-system.md) | 每个工具 15+ 字段，基于特性开关的条件注册 |
| 03 | [工具编排](docs/zh/s03-tool-orchestration.md) | 只读工具并发执行，写入工具串行执行 |
| 04 | [系统提示词](docs/zh/s04-system-prompt.md) | 缓存/非缓存分区，CLAUDE.md 记忆层级 |
| 05 | [权限系统](docs/zh/s05-permissions.md) | 基于规则的系统，4 个来源，模式匹配，bash 分类器 |

### 进阶篇 (06-10): 能力扩展

| # | 课程 | 核心洞察 |
|---|------|----------|
| 06 | [上下文压缩](docs/zh/s06-context-compaction.md) | 阈值 = 上下文窗口 - 13,000 缓冲; 3 次失败后熔断 |
| 07 | [技能](docs/zh/s07-skills.md) | 提示词中 ~100 token 摘要，调用时才加载完整内容 |
| 08 | [子 Agent](docs/zh/s08-subagents.md) | 缓存安全的 prompt 共享，侧链记录 |
| 09 | [任务系统](docs/zh/s09-task-system.md) | 7 种任务类型，依赖 DAG，磁盘输出流 |
| 10 | [钩子](docs/zh/s10-hooks.md) | 27 种事件类型，shell 执行，结构化 JSON I/O |

### 高级篇 (11-14): 生产级模式

| # | 课程 | 核心洞察 |
|---|------|----------|
| 11 | [MCP 集成](docs/zh/s11-mcp.md) | 3 种传输协议，工具 + 资源 + 提示词作为一等公民 |
| 12 | [状态管理](docs/zh/s12-state-management.md) | 类 Zustand 存储，8+ 消息类型，不可变/可变分区 |
| 13 | [团队与集群](docs/zh/s13-teams.md) | 后端无关的集群，协调者委托，权限桥接 |
| 14 | [工作树隔离](docs/zh/s14-worktrees.md) | Slug 校验，设置符号链接，钩子集成 |

## 架构概览

```
+-----------------------------------------------+
|            cli.tsx (引导启动)                   |
|  版本检查 -> 特性开关 -> 快速路径              |
+----------------------+------------------------+
                       |
+----------------------v------------------------+
|            main.tsx (完整初始化)                |
|  配置, 认证, 工具, MCP, 插件, 技能             |
+----------------------+------------------------+
                       |
+----------------------v-------------------------------+
|                 QueryEngine.ts                        |
|  编排: 查询循环, 工具执行, 上下文压缩                |
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
| 分区 -> 并发 /               |
| 串行 -> 钩子 -> 结果         |
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

## 前置条件

- Python 3.10+
- Anthropic API 密钥
- 对 LLM 和工具调用有基本了解

## 项目结构

```
learn-the-actual-claude-code/
  agents/            Python 复现 (每节课一个 + 合并版)
  docs/en/           渐进式学习指南 (English)
  docs/zh/           渐进式学习指南 (中文)
  source-analysis/   带注释的 TypeScript 源码解读
  skills/            示例技能定义
  architecture/      系统架构参考
  lib/               共享 Python 库 (类型, 工具函数)
  tests/             冒烟测试
```

## 参考资料与来源

### 主要来源

所有源码分析引用自 [AprilNEA/claude-code-source](https://github.com/AprilNEA/claude-code-source) 的 **Claude Code v2.1.88** TypeScript 源码，通过提取 `@anthropic-ai/claude-code` npm 包中的 source map 文件 (`cli.js.map`) 恢复。

核心源文件 (核心模块共 ~6,000+ 行):

| 文件 | 行数 | 职责 |
|------|------|------|
| `src/QueryEngine.ts` | 1,295 | 主 Agent 循环编排器 |
| `src/constants/prompts.ts` | 914 | 系统提示词组装 |
| `src/Tool.ts` | 792 | 工具接口定义 |
| `src/tools.ts` | 389 | 基于特性开关的工具注册 |
| `src/utils/permissions/permissions.ts` | 1,486 | 权限决策引擎 |
| `src/services/compact/autoCompact.ts` | 351 | 自动压缩与阈值 |
| `src/services/tools/toolOrchestration.ts` | 188 | 并发/串行工具调度 |

### Claude Code 官方资源

- [Claude Code](https://docs.anthropic.com/en/docs/claude-code) — Anthropic 官方 CLI 工具
- 由 Anthropic PBC 构建 (专有，保留所有权利)

> **免责声明**: 这是一个教育项目。源码分析引用的是通过 npm 公开分发的反编译代码。本仓库不重新分发原始源码 — 仅提供带注释的分析和独立的 Python 复现，用于学习目的。

## 致谢

- [AprilNEA](https://github.com/AprilNEA) 恢复并发布了 Claude Code 源码
- [shareAI-lab/learn-claude-code](https://github.com/shareAI-lab/learn-claude-code) 开创了渐进式分节教学 Agent 工程的方法
- [Anthropic](https://anthropic.com) 构建了 Claude Code

## 许可证

MIT
