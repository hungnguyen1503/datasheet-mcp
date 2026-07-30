---
name: sync-datasheet-server
description: Deploy Datasheet MCP code to HCMC-SERVER, inspect the live checkout and three-tool surface, or synchronize and publish one datasheet part. Use when asked to push/pull Datasheet MCP server code, update the lab server, check deployed MCP tools, sync datasheet artifacts, or refresh one part's canonical evidence index.
---

# Sync Datasheet Server

Use the project tool from the repository root. It targets `hungnguyen@192.168.2.8:/home/hungnguyen/datasheet-mcp` by default.

## Workflow

1. Read `.agents/docs/deployment.md` and `.agents/docs/sync.md`. Inspect `git status` locally and remotely; do not overwrite remote code, `.env`, or deployment files.
2. For code deployment, commit the intended local changes, then run:

   ```bash
   python tools/sync_server.py status
   python tools/sync_server.py deploy
   python tools/sync_server.py tools
   ```

   `deploy` refuses a dirty or diverged local worktree and only fast-forwards the server checkout. Restart `datasheetmcp` separately and interactively only when authorized.

3. Extract and verify the local corpus before transfer:

   ```bash
   python tools/extract_evidence.py --part <PART> --no-enrich
   python tools/sync_server.py --part <PART>
   ```

4. Review the dry-run plan. Apply the transfer only after the user confirms the target part:

   ```bash
   python tools/sync_server.py --part <PART> --apply
   ```

5. Publish only when the server has the matching canonical code and the user explicitly authorizes a Qdrant write:

   ```bash
   python tools/sync_server.py --part <PART> --apply --publish
   ```

6. Verify with `ds_catalog`/`ds_query` or `python tools/evaluate_retrieval.py --strict` after an authorized publish. Do not restart `datasheetmcp` unless separately authorized; this host requires an interactive sudo password.

## Guardrails

- Require `--part`; never sync all datasheets, use `--delete`, or reset a collection.
- Treat `deploy` as code-only Git synchronization. It pushes the current committed branch and uses remote `git pull --ff-only`; it does not commit, stash, force-push, overwrite `.env`, or restart the service.
- Default to preview. `--apply` enables transfer; `--publish` additionally rebuilds the selected remote part and changes Qdrant.
- Sync only `MD/`, `evidence/corpus.json`, and existing metadata files. Use `--include-source-pdf` only when the source PDF is explicitly needed remotely.
- Stop if the remote repository lacks `deploy/publish-part.sh`. Syncing data does not deploy code.
- Keep SSH credentials and `mcp/.env` out of commands, output, and commits.
