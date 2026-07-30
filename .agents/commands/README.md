# Project command recipes

These files define the delegation policy for this repository. They are prompt recipes for the available subagents, not shell commands. Include the target paths, symbols, expected output, and whether writes are authorized in every delegated task.

- [`plan.md`](plan.md): `sol high`; read-only planning.
- [`explore.md`](explore.md): `luna high`; read-only file and symbol exploration.
- [`search.md`](search.md): `luna high`; repository search and impact analysis.
- [`update.md`](update.md): `luna xhigh`; implementation only with explicit user authorization to modify.

If a subagent runner uses different syntax, preserve these effort levels, read/write permissions, and verification requirements.
