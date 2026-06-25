"""Datasheet MCP server package.

Serves component datasheets (ADXL345, OV7670, flash, sensors, …) to agents as
token-bounded tools: hybrid semantic search, deterministic register/bit/pin/
operation lookups, and a dependency graph — the HUM feature set, applied to
heterogeneous multi-vendor component datasheets.

Ingestion is text-only (MinerU PDF→markdown + an LLM extraction pass). There is
no pixel / image RAG.
"""
