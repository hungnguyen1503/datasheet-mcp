# AGENTS.md

## 1. Project Overview

Datasheet MCP turns IC datasheets into source-linked evidence for embedded-development decisions. It preserves tables, commands, modes, timing, operations, registers, pins, and their graph relationships, then exposes three part-scoped MCP tools.

## 2. Dev Commands

Run commands from the repository root; install dependencies with `python -m pip install -r mcp/requirements.txt`.

- Tests: `python -m pytest`
- Extract a local artifact: `python tools/extract_evidence.py --part <PART> --no-enrich`
- Windows full build: `build_all.bat --part <PART> --yes`
- Linux/macOS full build: `bash build_all.sh --part <PART> --yes`
- Resume/CI pipeline: `python tools/pipeline.py --part <PART> --yes`
- Start local server: `python mcp/server.py` (configure `mcp/.env`; Qdrant is required for queries)
- Stage 4 only: `mcp\build.bat --part <PART>` on Windows or `bash mcp/build.sh --part <PART>` on Unix

Use `.agents/docs/ingestion.md` for stage-specific commands and `.agents/docs/development.md` for setup and verification details.

## 3. Subagent Workflow

Use `.agents/commands/` as the project-local delegation recipes. Use `sol high` for plans, `luna high` for file exploration and repository search, and `luna xhigh` for code updates. Exploration and planning are read-only; allow modifications only when the user explicitly requests them, then review the diff and run relevant tests.

## 4. Key Constraints

- Keep public tools limited to `ds_catalog`, `ds_query`, and `ds_get`.
- Require `part` for evidence queries; never merge evidence across parts or infer unsupported register values, timing, or procedures.
- Preserve exact source provenance and lossless table structure; AI enrichment must be local, optional, source-anchored, and validated.
- Keep embeddings at 768 dimensions by default. Do not change a collection schema, model, or prefix without a migration/re-index plan.
- Treat `mcp/.env`, API keys, tunnel tokens, PDFs, and generated `datasheet/` artifacts as local or secret; never commit credentials or assume data is present.
- Do not use `--reset` or otherwise drop shared Qdrant collections without explicit authorization.
- Preserve the repository bootstrap paths (`mcp/server.py` and `tools/_bootstrap.py`); do not rely on the current working directory.
- MinerU, CUDA, VLM, and remote Qdrant availability are environment-dependent; keep CPU/optional paths working.

## 5. Additional Documentation

- [Architecture](.agents/docs/architecture.md)
- [Ingestion pipeline](.agents/docs/ingestion.md)
- [Query and indexing](.agents/docs/query-indexing.md)
- [Evidence model and tools](.agents/docs/evidence-tools.md)
- [Deployment and configuration](.agents/docs/deployment.md)
- [Data synchronization](.agents/docs/sync.md)
- [Development and testing](.agents/docs/development.md)
