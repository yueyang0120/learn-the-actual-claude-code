# Session 05 -- The Permission System

## Overview

Claude Code's permission system is one of the most sophisticated parts of the
codebase and arguably its most important safety feature. Every tool invocation --
Bash commands, file edits, MCP calls, agent spawning -- passes through a
multi-layered permission pipeline before it can execute. This session dissects
that pipeline end to end.

## Learning Objectives

1. **PermissionRule data model** -- Understand the `{source, ruleBehavior, ruleValue}` triple and how rules are parsed from the string format `Tool(content)`.
2. **Four rule sources** -- How rules arrive from `policySettings` (managed/enterprise), `userSettings` (~/.claude/settings.json), `projectSettings` (.claude/settings.json), `localSettings`, `cliArg`, `command`, and `session` -- and the priority between them.
3. **Three behaviors** -- `allow`, `deny`, `ask` -- and how they interact at each pipeline step.
4. **Permission modes** -- `default`, `acceptEdits`, `bypassPermissions`, `dontAsk`, `plan`, and the internal `auto` mode with its AI classifier.
5. **The hasPermissionsToUseTool pipeline** -- The numbered step sequence (1a through 3) that every tool call traverses.
6. **Bash command classification** -- How the YOLO/auto-mode classifier uses a 2-stage XML model to decide if a bash command is safe.
7. **Denial tracking and circuit breaker** -- How consecutive and total denials are counted, with fallback to human prompting after limits are exceeded.
8. **Permission persistence** -- How rules are loaded from settings JSON, persisted back on update, and synced when files change on disk.

## Source Files to Study

| File | Purpose |
|------|---------|
| `src/types/permissions.ts` | All core type definitions (PermissionRule, PermissionMode, PermissionDecision, etc.) |
| `src/utils/permissions/permissions.ts` | Main pipeline: `hasPermissionsToUseTool`, `checkRuleBasedPermissions`, rule matching |
| `src/utils/permissions/PermissionRule.ts` | Re-exports + Zod schemas for rules and behaviors |
| `src/utils/permissions/PermissionMode.ts` | Mode configuration (title, symbol, color per mode) |
| `src/utils/permissions/permissionRuleParser.ts` | String parsing: `Bash(npm install)` <-> `{toolName:"Bash", ruleContent:"npm install"}` |
| `src/utils/permissions/permissionsLoader.ts` | Disk I/O: loading rules from settings files, adding/deleting rules |
| `src/utils/permissions/denialTracking.ts` | Circuit breaker: consecutive/total denial counters |
| `src/utils/permissions/yoloClassifier.ts` | Auto-mode AI classifier (2-stage XML, transcript building) |
| `src/utils/permissions/bashClassifier.ts` | Bash-specific classifier stub (full impl is internal-only) |
| `src/utils/permissions/PermissionResult.ts` | Re-exports for result types |

## What shareAI-lab and Similar Clones Miss

Most open-source Claude Code clones (shareAI-lab, etc.) have **no permission
system at all**. They execute every tool call unconditionally. Here is what the
real implementation provides that they lack:

| Feature | Real Claude Code | Typical Clone |
|---------|-----------------|---------------|
| Rule-based permissions | 4+ sources, pattern matching with `Tool(content)` syntax | None |
| Permission modes | 6 modes including auto with AI classifier | Single mode (allow all) |
| Bash command safety | 2-stage XML classifier, subcommand splitting, heuristic guards | None |
| Denial tracking | Circuit breaker after 3 consecutive or 20 total denials | None |
| Safety checks | Protected paths (.git/, .claude/, shell configs) bypass even auto-approve | None |
| Enterprise policy | `policySettings` can lock down to managed rules only | None |
| MCP tool permissions | Server-level and tool-level rules with wildcard matching | None |
| Permission persistence | Rules saved to settings JSON, synced on file change | None |

## Session Files

- **SOURCE_ANALYSIS.md** -- Deep annotated walkthrough of the permission pipeline
- **reimplementation.py** -- Runnable Python reimplementation (~230 lines) demonstrating core concepts

## How to Use This Session

1. Read SOURCE_ANALYSIS.md alongside the actual source files listed above.
2. Run `python3 reimplementation.py` to see the permission engine in action.
3. Try modifying the YAML config in the reimplementation to add your own rules.
4. Compare the real `hasPermissionsToUseToolInner` step numbering with the simplified Python version.
