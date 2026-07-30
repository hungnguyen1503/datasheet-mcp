"""Hybrid prose index backed by Qdrant (dense + sparse BM25, RRF fusion).

Collection : ds_prose
Vectors    : "dense"  — dim from DS_EMBED_MODEL (default 768 for bge-base-en-v1.5)
             "sparse" — BM25 via fastembed Qdrant/bm25
Distance   : Cosine (dense), inner product (sparse)
Env vars   : QDRANT_URL      (default http://localhost:6333)
             QDRANT_API_KEY  (optional)
             DS_EMBED_MODEL  (default BAAI/bge-base-en-v1.5)

Quality features
----------------
- Hybrid BM25 + dense with RRF fusion  → best result first regardless of type
- Prefetch oversampling (k×4)          → better ANN recall
- search_groups() by block             → diverse results, no block monopoly
- content_type payload flag            → fast filtered scroll (operation/spec/order)
- Dynamic dim from embedder            → swap models without code changes
"""

from __future__ import annotations

import os
import uuid
from functools import lru_cache

from ..model import ProseBlock
from ..embed import get_embedder
from .. import reranker as _reranker


def _col(name: str) -> str:
    """Return prefixed Qdrant collection name."""
    from ..collections import get_prefix
    return f"{get_prefix()}{name}"


_BATCH = 256


@lru_cache(maxsize=1)
def _get_sparse_embedder():
    """Process-wide singleton for the BM25 sparse model (fastembed)."""
    from fastembed import SparseTextEmbedding
    return SparseTextEmbedding(model_name="Qdrant/bm25")


def _sparse_encode(texts: list[str]) -> list[dict]:
    """Return list of {"indices": [...], "values": [...]} dicts."""
    model = _get_sparse_embedder()
    out = []
    for sv in model.embed(texts):
        out.append({"indices": sv.indices.tolist(), "values": sv.values.tolist()})
    if len(out) != len(texts):
        raise RuntimeError(
            f"BM25 sparse encoder returned {len(out)} vectors for {len(texts)} texts — "
            "fastembed may have silently dropped items."
        )
    return out


class ProseIndexQdrant:
    """Hybrid prose index: dense vector + sparse BM25, fused with RRF."""

    def __init__(self, url: str | None = None, api_key: str | None = None):
        from qdrant_client import QdrantClient

        self._url = url or os.environ.get("QDRANT_URL", "http://localhost:6333")
        self._api_key = api_key or os.environ.get("QDRANT_API_KEY") or None
        self._client = QdrantClient(url=self._url, api_key=self._api_key, timeout=120)
        self.embedder = get_embedder()
        self._pending: list[dict] = []
        self._ensure_collection()

    # ── collection bootstrap ──────────────────────────────────────────────

    def _ensure_collection(self) -> None:
        existing = {c.name for c in self._client.get_collections().collections}
        if _col("ds_prose") not in existing:
            self._do_create_collection()
        else:
            info = self._client.get_collection(_col("ds_prose"))
            cfg = info.config.params
            sparse_cfg = (
                getattr(cfg, "sparse_vectors", None)
                or getattr(cfg, "sparse_vectors_config", None)
                or {}
            )
            vectors_cfg = getattr(cfg, "vectors", None) or {}
            current_dim = None
            if isinstance(vectors_cfg, dict) and "dense" in vectors_cfg:
                current_dim = getattr(vectors_cfg["dense"], "size", None)
            need_dim = self.embedder.dim
            if "sparse" not in sparse_cfg or (current_dim is not None and current_dim != need_dim):
                n_points = info.points_count or 0
                if n_points > 0:
                    raise RuntimeError(
                        f"ds_prose collection has {n_points} points with "
                        f"dim={current_dim}, but the current embedder produces "
                        f"dim={need_dim} (model: {self.embedder.model_name}). "
                        f"Refusing to delete populated collection. "
                        f"Either set DS_EMBED_MODEL to match the stored dimension, "
                        f"or manually drop the collection if you are sure."
                    )
                print(
                    f"  [!] ds_prose dim mismatch or missing sparse: "
                    f"stored={current_dim}, model={need_dim} -- recreating empty collection."
                )
                self._client.delete_collection(_col("ds_prose"))
                self._do_create_collection()

        # Ensure KEYWORD indexes exist (idempotent)
        try:
            from qdrant_client.models import PayloadSchemaType
            for field in ("heading", "content_type"):
                self._client.create_payload_index(
                    collection_name=_col("ds_prose"),
                    field_name=field,
                    field_schema=PayloadSchemaType.KEYWORD,
                )
        except Exception:
            pass

    def _do_create_collection(self) -> None:
        from qdrant_client.models import (
            Distance, VectorParams, SparseVectorParams, SparseIndexParams,
            PayloadSchemaType,
        )
        self._client.create_collection(
            collection_name=_col("ds_prose"),
            vectors_config={
                "dense": VectorParams(size=self.embedder.dim, distance=Distance.COSINE),
            },
            sparse_vectors_config={
                "sparse": SparseVectorParams(
                    index=SparseIndexParams(on_disk=False)
                ),
            },
        )
        for field in ("part", "block", "revision", "heading", "content_type"):
            self._client.create_payload_index(
                collection_name=_col("ds_prose"),
                field_name=field,
                field_schema=PayloadSchemaType.KEYWORD,
            )

    # ── write side ────────────────────────────────────────────────────────

    def clear_part(self, part: str) -> None:
        from qdrant_client.models import Filter, FieldCondition, MatchValue

        try:
            self._client.delete(
                collection_name=_col("ds_prose"),
                points_selector=Filter(
                    must=[FieldCondition(key="part", match=MatchValue(value=part))]
                ),
            )
        except Exception:
            pass

    def add_blocks(self, blocks: list[ProseBlock]) -> int:
        """Encode and queue blocks for upsert. Returns number of blocks accepted."""
        blocks = [b for b in blocks if len(b.text) >= 60]
        if not blocks:
            return 0
        texts = [b.embed_text() for b in blocks]
        dense_vecs = self.embedder.embed_documents(texts)
        sparse_vecs = _sparse_encode(texts)

        for b, dv, sv in zip(blocks, dense_vecs, sparse_vecs):
            self._pending.append({
                "dense": dv,
                "sparse": sv,
                "payload": {
                    "vendor":       b.vendor,
                    "part":         b.part,
                    "block":        b.block,
                    "section":      b.section,
                    "heading":      b.heading,
                    "breadcrumb":   b.breadcrumb,
                    "register":     b.register or "",
                    "text":         b.text,
                    "content_type": getattr(b, "content_type", "general"),
                    "revision":     b.revision,
                },
            })
        if len(self._pending) >= _BATCH:
            self.flush()
        return len(blocks)

    def flush(self) -> None:
        if not self._pending:
            return
        from qdrant_client.models import PointStruct, SparseVector

        for i in range(0, len(self._pending), _BATCH):
            batch = self._pending[i: i + _BATCH]
            self._client.upsert(
                collection_name=_col("ds_prose"),
                points=[
                    PointStruct(
                        id=str(uuid.uuid4()),
                        vector={
                            "dense": p["dense"],
                            "sparse": SparseVector(
                                indices=p["sparse"]["indices"],
                                values=p["sparse"]["values"],
                            ),
                        },
                        payload=p["payload"],
                    )
                    for p in batch
                ],
            )
        self._pending = []

    def build_indexes(self) -> None:
        """Called after all add_blocks()/flush() — indexes already built in _do_create_collection."""
        pass

    # ── read side ─────────────────────────────────────────────────────────

    def search(
        self,
        part: str,
        query: str,
        *,
        block: str | None = None,
        k: int = 5,
        content_type: str | None = None,
    ) -> list[dict]:
        """Hybrid search: BM25 sparse + dense cosine, fused with RRF.

        Args:
            part: Part name (required).
            query: Natural-language query.
            block: Optional block filter.
            k: Number of results.
            content_type: Optional content_type filter ("operation", "spec", "order").
        """
        from qdrant_client.models import (
            Filter, FieldCondition, MatchValue,
            Prefetch, FusionQuery, Fusion, SparseVector,
        )

        dense_vec = self.embedder.embed_query(query)
        sparse_list = _sparse_encode([query])
        sparse_vec = SparseVector(
            indices=sparse_list[0]["indices"],
            values=sparse_list[0]["values"],
        )

        conditions = [FieldCondition(key="part", match=MatchValue(value=part))]
        if block:
            conditions.append(FieldCondition(key="block", match=MatchValue(value=block)))
        if content_type:
            conditions.append(FieldCondition(key="content_type", match=MatchValue(value=content_type)))
        part_filter = Filter(must=conditions)

        fetch_k = max(k * 4, 20) if _reranker.is_enabled() else max(k * 3, 15)
        results = self._client.query_points(
            collection_name=_col("ds_prose"),
            prefetch=[
                Prefetch(query=dense_vec, using="dense", limit=fetch_k),
                Prefetch(query=sparse_vec, using="sparse", limit=fetch_k),
            ],
            query=FusionQuery(fusion=Fusion.RRF),
            query_filter=part_filter,
            limit=fetch_k if _reranker.is_enabled() else k,
            with_payload=True,
        ).points

        hits = [
            {
                "part":       r.payload.get("part", ""),
                "block":      r.payload.get("block", ""),
                "section":    r.payload.get("section", ""),
                "heading":    r.payload.get("heading", ""),
                "breadcrumb": r.payload.get("breadcrumb", ""),
                "register":   r.payload.get("register", ""),
                "text":       r.payload.get("text", ""),
                "content_type": r.payload.get("content_type", "general"),
                "score":      r.score,
            }
            for r in results
        ]
        hits = _reranker.rerank(query, hits, top_k=k)
        return hits

    def search_groups(
        self,
        part: str,
        query: str,
        *,
        k: int = 3,
        group_size: int = 2,
        content_type: str | None = None,
    ) -> list[dict]:
        """Hybrid search grouped by block — guarantees diverse results."""
        from qdrant_client.models import (
            Filter, FieldCondition, MatchValue,
            Prefetch, FusionQuery, Fusion, SparseVector,
        )

        dense_vec = self.embedder.embed_query(query)
        sparse_list = _sparse_encode([query])
        sparse_vec = SparseVector(
            indices=sparse_list[0]["indices"],
            values=sparse_list[0]["values"],
        )
        must = [FieldCondition(key="part", match=MatchValue(value=part))]
        if content_type:
            must.append(FieldCondition(key="content_type", match=MatchValue(value=content_type)))
        part_filter = Filter(must=must)

        fetch_groups = k * 2 if _reranker.is_enabled() else k
        try:
            groups = self._client.query_points_groups(
                collection_name=_col("ds_prose"),
                prefetch=[
                    Prefetch(query=dense_vec, using="dense", limit=fetch_groups * group_size * 4),
                    Prefetch(query=sparse_vec, using="sparse", limit=fetch_groups * group_size * 4),
                ],
                query=FusionQuery(fusion=Fusion.RRF),
                query_filter=part_filter,
                group_by="block",
                group_size=group_size,
                limit=fetch_groups,
                with_payload=True,
            ).groups

            out = []
            for g in groups:
                for r in g.hits:
                    out.append({
                        "part":       r.payload.get("part", ""),
                        "block":      r.payload.get("block", ""),
                        "section":    r.payload.get("section", ""),
                        "heading":    r.payload.get("heading", ""),
                        "breadcrumb": r.payload.get("breadcrumb", ""),
                        "register":   r.payload.get("register", ""),
                        "text":       r.payload.get("text", ""),
                        "content_type": r.payload.get("content_type", "general"),
                        "score":      r.score,
                    })
            hits = _reranker.rerank(query, out, top_k=k * group_size)
            return hits
        except Exception:
            return self.search(part, query, k=k * group_size, content_type=content_type)

    def search_by_content_type(
        self, part: str, content_type: str, *, block: str | None = None
    ) -> list[dict]:
        """Filter-only scroll by content_type — no vector search. Used for
        operation_only / spec_only / order_only modes.

        Results are ordered by section then heading to follow document structure.
        """
        from qdrant_client.models import Filter, FieldCondition, MatchValue

        must = [
            FieldCondition(key="part", match=MatchValue(value=part)),
            FieldCondition(key="content_type", match=MatchValue(value=content_type)),
        ]
        if block:
            must.append(FieldCondition(key="block", match=MatchValue(value=block)))

        try:
            results, _ = self._client.scroll(
                collection_name=_col("ds_prose"),
                scroll_filter=Filter(must=must),
                limit=500,
                with_payload=True,
                with_vectors=False,
            )
        except Exception:
            return []

        rows = [
            {
                "part":       r.payload.get("part", ""),
                "block":      r.payload.get("block", ""),
                "section":    r.payload.get("section", ""),
                "heading":    r.payload.get("heading", ""),
                "breadcrumb": r.payload.get("breadcrumb", ""),
                "register":   r.payload.get("register", ""),
                "text":       r.payload.get("text", ""),
                "content_type": r.payload.get("content_type", "general"),
            }
            for r in results
        ]
        rows.sort(key=lambda r: (r["section"], r.get("heading", "")))
        return rows

    def stats(self) -> dict:
        try:
            info = self._client.get_collection(_col("ds_prose"))
            return {"rows": info.points_count}
        except Exception:
            return {"rows": 0}

    def lookup_by_source_id(self, part: str, block: str, heading: str) -> dict | None:
        """Retrieve a prose block by exact match on part, block, and heading."""
        from qdrant_client.models import Filter, FieldCondition, MatchValue

        try:
            results, _ = self._client.scroll(
                collection_name=_col("ds_prose"),
                scroll_filter=Filter(must=[
                    FieldCondition(key="part", match=MatchValue(value=part)),
                    FieldCondition(key="block", match=MatchValue(value=block)),
                    FieldCondition(key="heading", match=MatchValue(value=heading)),
                ]),
                limit=1,
                with_payload=True,
                with_vectors=False,
            )
        except Exception:
            return None

        if not results:
            return None
        r = results[0]
        return {
            "part":       r.payload.get("part", ""),
            "block":      r.payload.get("block", ""),
            "section":    r.payload.get("section", ""),
            "heading":    r.payload.get("heading", ""),
            "breadcrumb": r.payload.get("breadcrumb", ""),
            "register":   r.payload.get("register", ""),
            "text":       r.payload.get("text", ""),
            "content_type": r.payload.get("content_type", "general"),
        }
