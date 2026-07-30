# Deployment and Configuration

## Topology

The documented deployment separates GPU ingestion from the CPU service host:

- A build machine runs Stages 1–3 and produces `datasheet/<PART>/` artifacts.
- An Ubuntu host runs Stage 4, Qdrant, and the systemd-managed MCP server.
- `deploy/docker-compose.yml` runs the Cloudflare Tunnel container in host networking mode.
- `deploy/datasheetmcp.service` starts `mcp/server.py` from the repository's virtual environment on port `8060`.

The service is exposed through the tunnel; do not hardcode tunnel credentials or bearer tokens in source, docs, client config, or test fixtures.

## Environment

Copy `mcp/.env.example` to `mcp/.env` and set values for the target host. Important variables:

| Variable | Purpose |
|---|---|
| `DS_TRANSPORT` | `stdio`, `streamable-http`, or `sse`; HTTP is the documented server mode |
| `DS_HOST`, `DS_PORT` | Bind address and port; the service uses `8060` |
| `QDRANT_URL`, `QDRANT_API_KEY` | Qdrant connection |
| `DS_API_KEYS` | Comma-separated bearer tokens; empty means no token verifier |
| `DS_SERVER_URL` | Public URL used for auth/resource metadata and allowed origins |
| `DS_COLLECTION_PREFIX` | Qdrant namespace; must match indexing and serving processes |
| `DS_EMBED_MODEL`, `DS_EMBED_DEVICE` | Dense model (default BGE base, 768 dimensions) and device override |
| `DS_LOCAL_AI_URL`, `DS_LOCAL_AI_MODEL` | Optional local-only ingestion enrichment endpoint and model |
| `DS_RERANKER_MODEL` | Optional cross-encoder reranker |

`mcp/server.py` and `tools/_bootstrap.py` load `.env` relative to `mcp/`, so startup does not depend on the caller's working directory.

## Operational checks

1. Confirm the service's `WorkingDirectory`, `EnvironmentFile`, Python path, and Qdrant URL match the host.
2. After an index build, verify the expected prefixed collections and part payloads in Qdrant.
3. Check service logs before changing query code: `sudo journalctl -u datasheetmcp -n 100 --no-pager`.
4. Check tunnel logs with `docker logs ds-cloudflared --tail 20` when public connectivity fails.
5. Test the MCP endpoint with a fresh token supplied through the shell environment; never paste a real token into this repository.

Do not run collection resets, service restarts, or tunnel changes against a shared host without explicit operational authorization.

For controlled code deployment and per-part data transfer to the lab host, use the project-local `$sync-datasheet-server` skill and [data synchronization](sync.md) guide. Code deployment uses Git push plus remote fast-forward only; data transfer is preview-first. Neither action restarts the service.
