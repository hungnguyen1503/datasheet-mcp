# Explore recipe

Delegate to `luna high` for file and symbol exploration.

Prompt shape:

```text
Explore <paths/symbols> for the requested datasheet MCP task.
Read-only: do not edit files.
If .codegraph/ exists, use CodeGraph before broad text search; report definitions, callers, data flow, and relevant tests.
Return exact file paths, key symbols, current behavior, and unknowns. Do not propose code changes unless asked.
```
