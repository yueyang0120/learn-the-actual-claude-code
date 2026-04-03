# Source Attribution

## Primary Source

All source analysis in this repository references **Claude Code v2.1.88** TypeScript source code, recovered from the `@anthropic-ai/claude-code` npm package by extracting the source map file (`cli.js.map`).

The recovered source is published at [AprilNEA/claude-code-source](https://github.com/AprilNEA/claude-code-source).

## Key Source Files

| File | Lines | Covered In |
|------|-------|------------|
| `src/QueryEngine.ts` | 1,295 | Chapter 01 |
| `src/query.ts` | 1,729 | Chapter 01 |
| `src/Tool.ts` | 792 | Chapter 02 |
| `src/tools.ts` | 389 | Chapter 02 |
| `src/services/tools/toolOrchestration.ts` | 188 | Chapter 03 |
| `src/constants/prompts.ts` | 914 | Chapter 04 |
| `src/utils/permissions/permissions.ts` | 1,486 | Chapter 05 |
| `src/services/compact/autoCompact.ts` | 351 | Chapter 06 |

A complete mapping of ~60 source files to chapters is available in [architecture/source_map.md](architecture/source_map.md).

## Credits

- [AprilNEA](https://github.com/AprilNEA) — recovered and published the Claude Code source
- [shareAI-lab/learn-claude-code](https://github.com/shareAI-lab/learn-claude-code) — pioneered the progressive session-based approach to teaching agent engineering
- [Anthropic](https://anthropic.com) — built Claude Code

## Disclaimer

This is an educational project. The source analysis references publicly distributed code recovered from the npm package. This repository does not redistribute the original source code — it provides annotated analysis and independent Python reimplementations for learning purposes.

Claude Code is proprietary software built by Anthropic PBC. All rights reserved by Anthropic.
