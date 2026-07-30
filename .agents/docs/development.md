# Development and Testing

## Local setup

From the repository root:

```bash
python -m venv .venv
python -m pip install -r mcp/requirements.txt
python -m pytest
```

There is no package metadata or lockfile in the repository. The requirements file is the dependency source of truth. The tests add `mcp/` to `sys.path` through `tests/conftest.py`; tools do the same through `tools/_bootstrap.py`.

## Running the server

Use `mcp/.env` for local values and make Qdrant available before exercising storage-backed tools:

```bash
python mcp/server.py
```

For explicit stdio or HTTP selection, set `DS_TRANSPORT` in the environment before starting. The server entrypoint can be launched from another working directory because it bootstraps its own package path.

## Verification loop

1. Use CodeGraph first when `.codegraph/` exists; query symbols and callers before editing a shared facade, store, or tool.
2. Read the focused implementation and neighboring tests.
3. Make the smallest change at the module that owns the behavior.
4. Run focused tests, then `python -m pytest`.
5. For pipeline/index changes, run a non-destructive dry or isolated part build and inspect Qdrant before claiming success.
6. Review `git diff` and `git status`; do not include `.env`, PDFs, generated markdown/JSON, caches, or local indexes.

For corpus-only verification, run `python tools/extract_evidence.py --part <PART> --no-enrich`. After an explicitly authorized index build, run `python tools/evaluate_retrieval.py --strict` against the golden cases and record recall, completeness, provenance, leakage, and p95 latency.

## Test ownership

`test_evidence_model.py` covers contracts and enrichment validation; `test_evidence_ingest.py` covers tables, provenance, semantic rows, multi-file builds, and graph construction; `test_evidence_store.py` covers schema guards, part isolation, hybrid storage, exact lookup, and relations with injected offline dependencies; `test_evidence_service.py` and `test_mcp_tools.py` cover packet assembly and the three-tool surface. Do not mutate a shared Qdrant namespace during tests.

No formatter, linter, or type-check command is configured in the repository. Do not add one to the onboarding contract unless the project adds its configuration and CI expectation.
