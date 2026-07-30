# Update recipe

Delegate to `luna xhigh` only after the user explicitly authorizes modification.

Prompt shape:

```text
Implement <approved change> in this datasheet MCP repository.
Modification is authorized for the stated scope only; do not reset Qdrant, change secrets, or touch unrelated files.
Inspect callers and tests before editing. Preserve MCP tool contracts, part scoping, bootstrap paths, and collection schema.
Use the smallest safe patch, run focused tests plus python -m pytest, and report changed files, test results, and any blocked external checks.
```

The parent agent must inspect `git diff` and `git status` before handoff.
