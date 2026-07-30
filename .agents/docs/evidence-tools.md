# Evidence Model and MCP Tools

## Public tool contract

Use one tool per agent turn unless the returned `gaps` explicitly require a follow-up:

- `ds_catalog(part="")` lists indexed parts; with `part`, it returns the document outline and extraction coverage.
- `ds_query(part, question, focus="auto", max_tokens=3000)` returns an implementation packet containing normalized MCU-facing settings, ordered steps, exact facts, constraints, graph relations, sources, conflicts, and gaps.
- `ds_get(part, target, relation_depth=1)` resolves an exact entity ID or symbol and returns its lossless table/register/command/operation evidence plus nearby graph relations.

Always pass one explicit part to `ds_query` and `ds_get`. Treat `gaps`, `conflicts`, `confidence`, and per-domain coverage as part of the result, not optional metadata.

## Evidence and provenance

`mcp/ds/evidence/model.py` is the shared persistence and API contract. Every `EvidenceItem` has a stable `ds://` ID, one part, a semantic kind, source references, confidence, and validation state. Tables retain raw source, normalized rows, cell coordinates, row/column spans, and row/cell provenance; semantic row entities make exact values retrievable without discarding the original table.

The graph uses explicit entity nodes for document hierarchy, tables, figures, operations, steps, modes, commands, registers, bitfields, pins, parameters, constraints, warnings, and memory regions. Relations such as `CONTAINS`, `NEXT`, `REQUIRES`, `USES_COMMAND`, `WRITES_REGISTER`, and `CONSTRAINED_BY` are evidence links, not permission to invent missing behavior.

## Query assembly

Query-time behavior is deterministic: retrieve part-filtered dense evidence, add exact lexical matches, traverse bounded graph relations, group evidence by implementation concern, and pack to the token budget. Configuration responses should normalize CPOL/CPHA, clock frequency, dummy cycles, register/command values, prerequisites, verification, and safety constraints when the source supports them. Keep vendor HAL names out of normalized settings.

Return a partial packet when evidence is incomplete. Name each missing domain in `gaps`; never fill it from another part, an unstated family assumption, or model knowledge.

## Local enrichment boundary

Deterministic parsing is sufficient to publish an index. Optional semantic enrichment may call only a configured local OpenAI-compatible endpoint. Accept an enriched entity only when its quoted evidence and scalar values occur in the source; otherwise discard it and record partial coverage. Query serving never calls an LLM.

## Acceptance checks

For the MX25 flash corpus, keep golden cases for SPI mode, dummy-cycle/frequency selection, and safe Program/Erase including WREN/WEL/WIP, blocked reads, suspend/read/resume, timing, and accepted commands. Release targets are at least 90% exact-fact recall@5, 85% packet completeness, 100% source-reference validity, zero cross-part leakage, warm p95 `ds_query` at or below 2 seconds, and warm catalog/get at or below 500 ms.
