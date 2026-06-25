"""LanceDB-backed store for the datasheet dependency graph (ds_graph table).

No vector column — edges are looked up by filtering source_id or target_id.
"""

from __future__ import annotations

import uuid

import pyarrow as pa

from .model import GraphEdge
from ..db import get_db

_TABLE = "ds_graph"

_SCHEMA = pa.schema([
    pa.field("id",          pa.string()),
    pa.field("part",        pa.string()),
    pa.field("edge_type",   pa.string()),
    pa.field("source_type", pa.string()),
    pa.field("source_id",   pa.string()),
    pa.field("target_type", pa.string()),
    pa.field("target_id",   pa.string()),
    pa.field("label",       pa.string()),
    pa.field("weight",      pa.float32()),
])

_BATCH = 256


class GraphStore:
    def __init__(self):
        self._db = get_db()
        self._pending: list[dict] = []
        self._tbl = self._open_or_create()

    def _open_or_create(self):
        if _TABLE not in self._db.list_tables():
            return self._db.create_table(_TABLE, schema=_SCHEMA)
        return self._db.open_table(_TABLE)

    # ── write ─────────────────────────────────────────────────────────────────

    def add_edge(self, edge: GraphEdge) -> None:
        self._pending.append({
            "id":          str(uuid.uuid4()),
            "part":        edge.part,
            "edge_type":   edge.edge_type,
            "source_type": edge.source_type,
            "source_id":   edge.source_id,
            "target_type": edge.target_type,
            "target_id":   edge.target_id,
            "label":       edge.label,
            "weight":      float(edge.weight),
        })
        if len(self._pending) >= _BATCH:
            self._flush()

    def commit(self) -> None:
        self._flush()

    def _flush(self) -> None:
        if self._pending:
            self._tbl.add(self._pending)
            self._pending = []

    def clear_part(self, part: str) -> None:
        try:
            self._tbl.delete(f"part = '{part}'")
        except Exception:
            pass

    # ── read ──────────────────────────────────────────────────────────────────

    def get_neighbors(
        self,
        part: str,
        node_id: str,
        *,
        direction: str = "out",
        edge_types: list[str] | None = None,
        limit: int = 200,
    ) -> list[GraphEdge]:
        edges: list[GraphEdge] = []

        def _fetch(id_field: str) -> list[dict]:
            where = f"part = '{part}' AND {id_field} = '{node_id}'"
            if edge_types:
                et_list = ", ".join(f"'{t}'" for t in edge_types)
                where += f" AND edge_type IN ({et_list})"
            try:
                return (
                    self._tbl.search()
                    .where(where, prefilter=True)
                    .limit(limit)
                    .to_list()
                )
            except Exception:
                return []

        if direction in ("out", "both"):
            for r in _fetch("source_id"):
                edges.append(self._row_to_edge(r))
        if direction in ("in", "both"):
            for r in _fetch("target_id"):
                edges.append(self._row_to_edge(r))

        return edges[:limit]

    def count(self, part: str) -> int:
        try:
            return self._tbl.count_rows(f"part = '{part}'")
        except Exception:
            return 0

    def close(self) -> None:
        pass

    @staticmethod
    def _row_to_edge(r: dict) -> GraphEdge:
        return GraphEdge(
            part=r["part"],
            edge_type=r["edge_type"],
            source_type=r["source_type"],
            source_id=r["source_id"],
            target_type=r["target_type"],
            target_id=r["target_id"],
            label=r.get("label", ""),
            weight=float(r.get("weight", 1.0)),
        )
