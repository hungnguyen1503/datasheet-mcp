# Search recipe

Delegate to `luna high` for repository searching.

Prompt shape:

```text
Search this datasheet MCP repository for <term/symbol/behavior>.
Read-only: do not edit files.
Use CodeGraph first when .codegraph/ exists, then targeted rg searches.
Return matching files and line-relevant symbols, callers/callees, tests, configuration references, and likely change surface.
Distinguish source behavior from README/USAGE claims.
```
