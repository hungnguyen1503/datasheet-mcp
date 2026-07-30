# Architecture

## Runtime path

1. `mcp/server.py` adds `mcp/` to `sys.path`, loads `mcp/.env`, sets UTF-8 defaults, and calls `ds.mcp_server.main`.
2. `mcp/ds/mcp_server.py` constructs the FastMCP server and exposes the ASGI `app` for streamable HTTP. It owns transport, optional bearer-token verification, host/origin checks, and the three public tools.
3. `mcp/ds/evidence/service.py` assembles source-linked implementation packets from `mcp/ds/evidence/store_qdrant.py`.
4. `mcp/ds/evidence/model.py` defines the persisted entities and structured tool responses. Query-time assembly is deterministic and token-bounded.

The server is stateless HTTP by default. `DS_TRANSPORT`, `DS_HOST`, and `DS_PORT` control transport settings; the sample environment uses port `8060`.

## MCP tool surface

| Tool | Use |
|---|---|
| `ds_catalog` | List parts or inspect one part's outline and coverage |
| `ds_query` | Retrieve a bounded implementation packet for a development question |
| `ds_get` | Resolve one exact entity and its graph neighborhood |

`ds_query` and `ds_get` require `part`, so identical symbols from different components cannot be mixed.

## Storage boundaries

`mcp/ds/ingest/build.py` delegates to the evidence builder, which converts per-part files under `datasheet/<PART>/` into three Qdrant collections:

- `ds_evidence`: 768-dimensional dense vectors plus BM25 sparse vectors and full evidence payloads; search uses reciprocal-rank fusion.
- `ds_graph`: payload-only typed relations between evidence nodes.
- `ds_catalog`: payload-only part metadata, hierarchy, and extraction coverage.

`DS_COLLECTION_PREFIX` namespaces these names for stable/nightly or tenant isolation. The same prefix must be used by both the index builder and server.

## Module ownership

- `mcp/ds/evidence/model.py`: evidence, provenance, graph, coverage, and structured response contracts.
- `mcp/ds/evidence/ingest.py`: lossless multi-document parsing and graph construction.
- `mcp/ds/evidence/store_qdrant.py`: canonical collection schemas, indexing, exact lookup, hybrid retrieval, and graph traversal.
- `mcp/ds/evidence/service.py`: deterministic implementation-packet assembly and token budgeting.
- `mcp/ds/ingest/build.py`: stable Stage 4 entry point delegating to the evidence builder.
- `tools/`: PDF ingestion and pipeline orchestration; `tools/_bootstrap.py` is the shared import/config bootstrap.
- `tests/test_evidence_*.py` and `tests/test_mcp_tools.py`: canonical ingestion, store, service, provenance, and public-tool contracts.

When changing a boundary, update the owning adapter, its facade or tool contract, and focused tests together.
