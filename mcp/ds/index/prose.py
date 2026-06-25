"""LanceDB-backed hybrid prose index (dense vector + BM25 full-text search).

Table  : ds_prose
Vectors: dense (dim from DS_EMBED_MODEL, default 384 for bge-small-en-v1.5)
FTS    : tantivy-based BM25 via LanceDB create_fts_index on (text, heading, breadcrumb)
Hybrid : LinearCombinationReranker(weight=0.7) — 70% dense + 30% BM25

Quality features
----------------
- Dense ANN + BM25 FTS with hybrid fusion  → best result first
- Adaptive batch size (256 GPU / 32 CPU)   → fast on laptop CPUs
- search_groups() post-processes for block diversity
- is_operation bool column for fast get_operation() scroll
"""

from __future__ import annotations

import uuid

import pyarrow as pa

from ..model import ProseBlock
from ..embed import get_embedder, _BATCH_SIZE
from ..db import get_db
from .. import reranker as _reranker

_TABLE = "ds_prose"


def is_op_heading(heading: str) -> bool:
    h = heading.lower()
    return any(kw in h for kw in (
        "operation", "operating", "initializ", "configur", "sequence",
        "procedure", "startup", "start-up", "power-up", "power up", "setup", "set up",
    ))


def _prose_schema(dim: int) -> pa.Schema:
    return pa.schema([
        pa.field("id",           pa.string()),
        pa.field("vendor",       pa.string()),
        pa.field("part",         pa.string()),
        pa.field("block",        pa.string()),
        pa.field("section",      pa.string()),
        pa.field("heading",      pa.string()),
        pa.field("breadcrumb",   pa.string()),
        pa.field("register",     pa.string()),
        pa.field("text",         pa.string()),
        pa.field("is_operation", pa.bool_()),
        pa.field("revision",     pa.string()),
        pa.field("vector",       pa.list_(pa.float32(), dim)),
    ])


class ProseIndex:
    def __init__(self):
        self._db = get_db()
        self.embedder = get_embedder()
        self._pending: list[dict] = []
        self._tbl = self._open_or_create()

    def _open_or_create(self):
        schema = _prose_schema(self.embedder.dim)
        existing = self._db.list_tables()
        if _TABLE not in existing:
            return self._db.create_table(_TABLE, schema=schema)
        tbl = self._db.open_table(_TABLE)
        try:
            stored_dim = tbl.schema.field("vector").type.list_size
            if stored_dim != self.embedder.dim:
                print(f"  [!] {_TABLE} dim mismatch "
                      f"(stored={stored_dim}, model={self.embedder.dim}) — recreating.")
                self._db.drop_table(_TABLE)
                return self._db.create_table(_TABLE, schema=schema)
        except Exception:
            pass
        return tbl

    # ── write ─────────────────────────────────────────────────────────────────

    def clear_part(self, part: str) -> None:
        try:
            self._tbl.delete(f"part = '{part}'")
        except Exception:
            pass

    def add_blocks(self, blocks: list[ProseBlock]) -> int:
        blocks = [b for b in blocks if len(b.text) >= 60]
        if not blocks:
            return 0
        texts = [b.embed_text() for b in blocks]
        dense_vecs = self.embedder.embed_documents(texts)

        for b, dv in zip(blocks, dense_vecs):
            self._pending.append({
                "id":           str(uuid.uuid4()),
                "vendor":       b.vendor,
                "part":         b.part,
                "block":        b.block,
                "section":      b.section,
                "heading":      b.heading,
                "breadcrumb":   b.breadcrumb,
                "register":     b.register or "",
                "text":         b.text,
                "is_operation": is_op_heading(b.heading),
                "revision":     b.revision,
                "vector":       dv,
            })
        if len(self._pending) >= _BATCH_SIZE:
            self.flush()
        return len(blocks)

    def flush(self) -> None:
        if not self._pending:
            return
        self._tbl.add(self._pending)
        self._pending = []

    def build_indexes(self) -> None:
        """Call once after all add_blocks()/flush() calls are done."""
        try:
            self._tbl.create_fts_index(
                ["text", "heading", "breadcrumb"], replace=True
            )
        except Exception as e:
            print(f"  [warn] FTS index skipped: {e}")

    # ── read ──────────────────────────────────────────────────────────────────

    def search(self, part: str, query: str, *, block: str | None = None, k: int = 5) -> list[dict]:
        """Hybrid search: dense vector + BM25 FTS, fused with LinearCombination."""
        qv = self.embedder.embed_query(query)
        where = f"part = '{part}'"
        if block:
            where += f" AND block = '{block}'"

        fetch_k = max(k * 4, 20)
        try:
            from lancedb.rerankers import LinearCombinationReranker
            lc = LinearCombinationReranker(weight=0.7)
            rows = (
                self._tbl.search(qv, query_type="hybrid")
                .where(where, prefilter=True)
                .rerank(reranker=lc)
                .limit(fetch_k if _reranker.is_enabled() else k)
                .to_list()
            )
        except Exception:
            # Fallback: dense-only search (FTS index may not be built yet)
            try:
                rows = (
                    self._tbl.search(qv, vector_column_name="vector")
                    .where(where, prefilter=True)
                    .limit(fetch_k if _reranker.is_enabled() else k)
                    .to_list()
                )
            except Exception:
                rows = []

        hits = [self._hit(r) for r in rows]
        return _reranker.rerank(query, hits, top_k=k)

    def search_groups(self, part: str, query: str, *, k: int = 3, group_size: int = 2) -> list[dict]:
        """Hybrid search with block diversity (post-processing grouping)."""
        all_hits = self.search(part, query, k=k * 4)
        # group by block, take top group_size per block, up to k blocks
        groups: dict[str, list[dict]] = {}
        for h in all_hits:
            blk = h.get("block", "")
            if blk not in groups:
                groups[blk] = []
            if len(groups[blk]) < group_size:
                groups[blk].append(h)
        out = [h for hits in list(groups.values())[:k] for h in hits]
        return _reranker.rerank(query, out, top_k=k * group_size)

    @staticmethod
    def _hit(r: dict) -> dict:
        return {
            "part":       r.get("part", ""),
            "block":      r.get("block", ""),
            "section":    r.get("section", ""),
            "heading":    r.get("heading", ""),
            "breadcrumb": r.get("breadcrumb", ""),
            "register":   r.get("register", ""),
            "text":       r.get("text", ""),
            "score":      r.get("_relevance_score", r.get("score", 0.0)),
        }

    def stats(self) -> dict:
        try:
            return {"rows": self._tbl.count_rows()}
        except Exception:
            return {"rows": 0}
