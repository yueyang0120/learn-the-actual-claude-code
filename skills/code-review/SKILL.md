---
name: code-review
description: Review code changes for bugs, style issues, and security concerns
whenToUse: When the user asks for a code review or wants feedback on their changes
allowed-tools:
  - bash
  - read_file
---

# Code Review

Review the current diff or specified files for:

1. **Correctness** — Logic errors, off-by-one, missing edge cases
2. **Security** — Injection vulnerabilities, hardcoded secrets, unsafe deserialization
3. **Style** — Naming, structure, consistency with project conventions
4. **Performance** — Obvious N+1 queries, unnecessary allocations, blocking calls

## Steps

1. Run `git diff` to see what changed
2. Read each modified file for full context
3. For each issue found, report:
   - File and line number
   - Severity (critical / warning / suggestion)
   - What's wrong and how to fix it
4. Summarize overall assessment

## Output Format

```
## Review Summary

**Overall**: [PASS / NEEDS CHANGES / CRITICAL ISSUES]

### Issues

1. **[severity]** `file:line` — description
   - Suggestion: ...

### What Looks Good

- ...
```
