# Plan recipe

Delegate to `sol high`.

Prompt shape:

```text
Plan the requested change in this datasheet MCP repository.
Read-only: do not edit files.
Inspect the relevant symbols, callers, tests, Qdrant/schema impact, and pipeline/deployment impact.
Return: assumptions, ordered implementation steps, risks, focused tests, and a verification command list.
```

The parent agent owns the final plan and must reject assumptions that violate `AGENTS.md` constraints.
