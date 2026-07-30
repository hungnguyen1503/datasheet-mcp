# Data synchronization

## Target

The lab server is `hungnguyen@192.168.2.8` (`HCMC-SERVER`) and the Datasheet MCP checkout is `/home/hungnguyen/datasheet-mcp`. The `datasheetmcp` systemd unit is active from that checkout. `sudo -n` requires a password, so unattended service restarts are unavailable.

## Code deployment

Use Git for server code, following the HUM/Schematic MCP pattern:

```bash
git status
git add <intended paths>
git commit -m "<message>"
python tools/sync_server.py status
python tools/sync_server.py deploy
python tools/sync_server.py tools
```

`deploy` requires a clean local worktree, pushes the current branch, then runs `git pull --ff-only` in the server checkout. It never stages, commits, force-pushes, or touches `.env`. The `tools` command imports the remote server module and requires exactly `ds_catalog`, `ds_query`, and `ds_get`.

The systemd process does not reload Python code until restarted. A restart requires interactive sudo on this host: `ssh -t hungnguyen@192.168.2.8 'sudo systemctl restart datasheetmcp'`.

## Safe data transfer

Use the project-local skill and tool from the repository root:

```bash
python tools/sync_server.py sync --part <PART>
python tools/sync_server.py sync --part <PART> --apply
```

The default payload contains only the selected part's `MD/` input, `evidence/corpus.json`, and present structured metadata. It intentionally excludes MinerU raw output, enrichment cache, local indexes, and `.env`. It never deletes remote files.

## Publishing

`--publish` invokes the canonical server-side build for only the requested part, writing its evidence to the configured Qdrant namespace:

```bash
python tools/sync_server.py publish --part <PART> --apply
```

This is a shared-state mutation; require explicit authorization. It also requires the server checkout to contain `deploy/publish-part.sh`; deploy matching code first if the preflight reports it missing. The script loads `mcp/.env` through `mcp/build_helper.py` and rejects a missing Markdown input. A service restart is normally unnecessary for an upsert. If one is authorized, run it interactively on the host: `sudo systemctl restart datasheetmcp`.
