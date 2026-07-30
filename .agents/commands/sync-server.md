# Sync server recipe

Use `$sync-datasheet-server` for a requested Datasheet MCP data transfer to the lab host.

For a server-code update, commit the intended local work, run `python tools/sync_server.py deploy`, then run `python tools/sync_server.py tools` to require exactly `ds_catalog`, `ds_query`, and `ds_get`. The deploy command refuses a dirty/diverged local worktree and uses remote fast-forward only. Restarting `datasheetmcp` needs separate authorization and interactive sudo.

For data, confirm the part and whether Qdrant publication is authorized. Run the project tool in preview mode first, then use `--apply` only for the selected part. Use `publish --part <PART> --apply` only with explicit authorization because it rebuilds that part's remote Qdrant evidence. Never transfer `.env`, deployment configuration, or unrelated datasheet parts.
