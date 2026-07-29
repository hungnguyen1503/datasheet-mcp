"""Qdrant-backed store for the datasheet dependency graph (ds_graph collection).

No vector column — edges are looked up by filtering source_id or target_id.
"""

from __future__ import annotations

import os
import time
import uuid

from .model import GraphEdge

# ── Collection name helper (reads DS_COLLECTION_PREFIX at runtime) ──────


def _col(name: str) -> str:
    from ds.collections import get_prefix
    return f"{get_prefix()}{name}"


_BATCH = 256


class GraphStoreQdrant:
    """Qdrant-backed graph store — payload-only, keyword-indexed."""

    def __init__(self, url: str | None = None, api_key: str | None = None):
        from qdrant_client import QdrantClient

        self._url = url or os.environ.get("QDRANT_URL", "http://localhost:6333")
        self._api_key = api_key or os.environ.get("QDRANT_API_KEY") or None
        self._client = QdrantClient(url=self._url, api_key=self._api_key, timeout=120)
        self._pending: list[dict] = []
        self._ensure_collection()

    def _ensure_collection(self) -> None:
        from qdrant_client.models import PayloadSchemaType

        existing = {c.name for c in self._client.get_collections().collections}
        if _col("ds_graph") not in existing:
            self._client.create_collection(
                collection_name=_col("ds_graph"),
                vectors_config={},
            )
            for kw in ("part", "source_id", "target_id", "edge_type"):
                self._client.create_payload_index(
                    collection_name=_col("ds_graph"),
                    field_name=kw,
                    field_schema=PayloadSchemaType.KEYWORD,
                )

    # ── write ─────────────────────────────────────────────────────────────

    def add_edge(self, edge: GraphEdge) -> None:
        self._pending.append({
            "payload": {
                "part":        edge.part,
                "edge_type":   edge.edge_type,
                "source_type": edge.source_type,
                "source_id":   edge.source_id,
                "target_type": edge.target_type,
                "target_id":   edge.target_id,
                "label":       edge.label,
                "weight":      float(edge.weight),
            },
        })
        if len(self._pending) >= _BATCH:
            self._flush()

    def commit(self) -> None:
        self._flush()

    def _flush(self) -> None:
        if not self._pending:
            return
        from qdrant_client.models import PointStruct
        from qdrant_client.http.exceptions import UnexpectedResponse

        for i in range(0, len(self._pending), _BATCH):
            batch = self._pending[i: i + _BATCH]
            points = [
                PointStruct(id=str(uuid.uuid4()), vector={}, payload=p["payload"])
                for p in batch
            ]
            # Retry on transient errors
            max_attempts = 3
            backoff = [2, 4, 8]
            for attempt in range(1, max_attempts + 1):
                try:
                    self._client.upsert(collection_name=_col("ds_graph"), points=points)
                    break
                except UnexpectedResponse as exc:
                    status = exc.status_code or 0
                    if status and 400 <= status < 500:
                        raise
                    if attempt < max_attempts:
                        print(f"  ⚠️  graph upsert failed ({status}), retrying in {backoff[attempt - 1]}s…")
                        time.sleep(backoff[attempt - 1])
                    else:
                        raise
                except Exception as exc:
                    if attempt < max_attempts:
                        print(f"  ⚠️  graph upsert error ({type(exc).__name__}), retrying in {backoff[attempt - 1]}s…")
                        time.sleep(backoff[attempt - 1])
                    else:
                        raise
        self._pending = []

    def clear_part(self, part: str) -> None:
        from qdrant_client.models import Filter, FieldCondition, MatchValue

        try:
            self._client.delete(
                collection_name=_col("ds_graph"),
                points_selector=Filter(
                    must=[FieldCondition(key="part", match=MatchValue(value=part))]
                ),
            )
        except Exception:
            pass

    # ── read ──────────────────────────────────────────────────────────────

    def get_neighbors(
        self,
        part: str,
        node_id: str,
        *,
        direction: str = "out",
        edge_types: list[str] | None = None,
        limit: int = 200,
    ) -> list[GraphEdge]:
        from qdrant_client.models import Filter, FieldCondition, MatchValue

        edges: list[GraphEdge] = []

        def _fetch(id_field: str) -> list[dict]:
            must = [
                FieldCondition(key="part", match=MatchValue(value=part)),
                FieldCondition(key=id_field, match=MatchValue(value=node_id)),
            ]
            if edge_types:
                for et in edge_types:
                    pass  # handled below

            # Build filter — for multiple edge types, use should (OR)
            if edge_types and len(edge_types) == 1:
                must.append(FieldCondition(key="edge_type", match=MatchValue(value=edge_types[0])))
                query_filter = Filter(must=must)
            elif edge_types:
                # Multiple edge types -> use should (OR) for edge_type
                type_conditions = [
                    FieldCondition(key="edge_type", match=MatchValue(value=et))
                    for et in edge_types
                ]
                query_filter = Filter(
                    must=must,
                    should=type_conditions,
                )
            else:
                query_filter = Filter(must=must)

            try:
                results, _ = self._client.scroll(
                    collection_name=_col("ds_graph"),
                    scroll_filter=query_filter,
                    limit=limit,
                    with_payload=True,
                    with_vectors=False,
                )
                return results
            except Exception:
                return []

        if direction in ("out", "both"):
            for r in _fetch("source_id"):
                edges.append(self._row_to_edge(r.payload))
        if direction in ("in", "both"):
            for r in _fetch("target_id"):
                edges.append(self._row_to_edge(r.payload))

        return edges[:limit]

    def count(self, part: str) -> int:
        try:
            return self._client.get_collection(_col("ds_graph")).points_count or 0
        except Exception:
            return 0

    def close(self) -> None:
        pass  # HTTP client — nothing to close

    @staticmethod
    def _row_to_edge(p: dict) -> GraphEdge:
        return GraphEdge(
            part=p.get("part", ""),
            edge_type=p.get("edge_type", ""),
            source_type=p.get("source_type", ""),
            source_id=p.get("source_id", ""),
            target_type=p.get("target_type", ""),
            target_id=p.get("target_id", ""),
            label=p.get("label", ""),
            weight=float(p.get("weight", 1.0)),
        )
