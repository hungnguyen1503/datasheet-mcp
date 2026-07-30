# Query and Indexing

## Query routing contract

The server documents a strict one-tool-per-user-query policy. Choose by required output:

- `ds_catalog` for part discovery, outline, and coverage.
- `ds_query` for configuration, timing, operation, exact-fact, or explanatory packets.
- `ds_get` for a stable entity ID, exact symbol, table, command, mode, or register.

Do not silently list parts, chain searches, or omit `part`. Use `focus` only to sharpen packet assembly; retrieval remains part-filtered. See `evidence-tools.md` for response fields and acceptance criteria.

## Qdrant and embeddings

`QDRANT_URL` defaults to `http://localhost:6333`; `QDRANT_API_KEY` is optional locally. `DS_EMBED_MODEL` defaults to `BAAI/bge-base-en-v1.5` at 768 dimensions. The canonical collections hold evidence, relations, and catalog/coverage payloads.

The index builder clears only the selected part from evidence, catalog, and graph stores before repopulating it. Re-index a part after parser, semantic-classification, or embedding changes. A model with different vector dimensions requires an explicitly authorized collection reset and complete re-index.

## Result behavior

`CatalogResponse`, `QueryResponse`, and `GetResponse` are structured Pydantic contracts. `QueryResponse` carries normalized configuration, ordered steps, facts, constraints, relations, sources, gaps, conflicts, coverage, confidence, and truncation state. Do not remove token packing or return unbounded datasheet text.

The dense and sparse models load during server prewarm. Qdrant failures should surface as an operational diagnosis rather than being hidden by a fallback that changes result semantics or mixes parts.

## Change checklist

When changing routing or output:

1. Update `mcp/ds/evidence/service.py`, `store_qdrant.py`, or the public tool docstring at the ownership point.
2. Preserve uppercase part scoping, accepted `focus` values, exact source references, and explicit gaps.
3. Add or update focused `test_evidence_*` or `test_mcp_tools.py` coverage.
4. Run `python -m pytest`; for storage changes, use an isolated in-memory client before any explicitly authorized shared index build.
