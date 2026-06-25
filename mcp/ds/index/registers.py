"""LanceDB-backed register + catalog store.

Tables
------
ds_registers : dense vector + FTS on (register, name)  — exact + semantic lookup
ds_prose     : read-only here; scrolled for get_operation()  (written by prose.py)

No server required — all data lives in the local .lancedb directory.
"""

from __future__ import annotations

import json
import uuid
from functools import lru_cache
from typing import Any

import pyarrow as pa

from ..model import RegisterCard, BitField
from ..embed import get_embedder
from ..db import get_db

_REG_TABLE = "ds_registers"
_PROSE_TABLE = "ds_prose"
_BATCH = 256


def _reg_schema(dim: int) -> pa.Schema:
    return pa.schema([
        pa.field("id",        pa.string()),
        pa.field("vendor",    pa.string()),
        pa.field("part",      pa.string()),
        pa.field("block",     pa.string()),
        pa.field("register",  pa.string()),
        pa.field("name",      pa.string()),
        pa.field("section",   pa.string()),
        pa.field("addresses", pa.string()),   # JSON
        pa.field("bitfields", pa.string()),   # JSON
        pa.field("notes",     pa.string()),
        pa.field("revision",  pa.string()),
        pa.field("vector",    pa.list_(pa.float32(), dim)),
    ])


class RegisterStore:
    def __init__(self):
        self._db = get_db()
        self.embedder = get_embedder()
        self._pending_regs: list[dict] = []
        self._tbl = self._open_or_create_reg()

    # ── bootstrap ─────────────────────────────────────────────────────────────

    def _open_or_create_reg(self):
        schema = _reg_schema(self.embedder.dim)
        existing = self._db.list_tables()
        if _REG_TABLE not in existing:
            return self._db.create_table(_REG_TABLE, schema=schema)
        tbl = self._db.open_table(_REG_TABLE)
        # Recreate if dim changed (embedding model swap)
        try:
            stored = tbl.schema
            vf = stored.field("vector")
            stored_dim = vf.type.list_size
            if stored_dim != self.embedder.dim:
                print(f"  [!] {_REG_TABLE} dim mismatch "
                      f"(stored={stored_dim}, model={self.embedder.dim}) — recreating.")
                self._db.drop_table(_REG_TABLE)
                return self._db.create_table(_REG_TABLE, schema=schema)
        except Exception:
            pass
        return tbl

    # ── write ─────────────────────────────────────────────────────────────────

    def clear_part(self, part: str) -> None:
        try:
            self._tbl.delete(f"part = '{part}'")
        except Exception:
            pass
        try:
            prose_tbl = self._db.open_table(_PROSE_TABLE)
            prose_tbl.delete(f"part = '{part}'")
        except Exception:
            pass

    def add_register(self, card: RegisterCard) -> None:
        self._pending_regs.append({
            "embed_text": f"{card.register} {card.name}",
            "row": {
                "id":        str(uuid.uuid4()),
                "vendor":    card.vendor,
                "part":      card.part,
                "block":     card.block,
                "register":  card.register,
                "name":      card.name,
                "section":   card.section,
                "addresses": json.dumps(card.addresses),
                "bitfields": json.dumps([b.__dict__ for b in card.bitfields]),
                "notes":     card.notes,
                "revision":  card.revision,
            },
        })
        if len(self._pending_regs) >= _BATCH:
            self._flush_regs()

    def _flush_regs(self) -> None:
        if not self._pending_regs:
            return
        texts = [p["embed_text"] for p in self._pending_regs]
        vecs = self.embedder.embed_documents(texts)
        rows = []
        for p, vec in zip(self._pending_regs, vecs):
            row = dict(p["row"])
            row["vector"] = vec
            rows.append(row)
        self._tbl.add(rows)
        self._pending_regs = []

    def commit(self) -> None:
        self._flush_regs()
        # Build FTS index after all data is loaded
        try:
            self._tbl.create_fts_index(["register", "name"], replace=True)
        except Exception:
            pass  # FTS may not be available in all LanceDB builds

    def close(self) -> None:
        pass

    # ── read ──────────────────────────────────────────────────────────────────

    @lru_cache(maxsize=512)
    def _get_register_cached(self, part: str, register: str, block: str | None) -> list[RegisterCard]:
        where = f"part = '{part}' AND register = '{register}'"
        if block:
            where += f" AND block = '{block}'"
        try:
            rows = self._tbl.search().where(where, prefilter=True).to_list()
            return [self._row_to_card(r) for r in rows]
        except Exception:
            return []

    def get_register(self, part: str, register: str, block: str | None = None) -> list[RegisterCard]:
        return self._get_register_cached(
            part.upper(), register.upper(), block.upper() if block else None
        )

    def search_registers(self, part: str, query: str, limit: int = 10) -> list[RegisterCard]:
        qv = self.embedder.embed_query(query)
        try:
            rows = (
                self._tbl.search(qv, vector_column_name="vector")
                .where(f"part = '{part}'", prefilter=True)
                .limit(limit)
                .to_list()
            )
        except Exception:
            rows = []
        seen, out = set(), []
        for r in rows:
            card = self._row_to_card(r)
            if card.key not in seen:
                seen.add(card.key)
                out.append(card)
        return out

    @staticmethod
    def _row_to_card(r: dict[str, Any]) -> RegisterCard:
        bfs = [BitField(**b) for b in json.loads(r.get("bitfields") or "[]")]
        return RegisterCard(
            vendor=r.get("vendor", ""),
            part=r.get("part", ""),
            block=r.get("block", ""),
            register=r.get("register", ""),
            name=r.get("name", ""),
            section=r.get("section") or "",
            addresses=[tuple(a) for a in json.loads(r.get("addresses") or "[]")],
            bitfields=bfs,
            notes=r.get("notes") or "",
            revision=r.get("revision") or "",
        )

    # ── operation sections (read from ds_prose) ───────────────────────────────

    def get_operation(self, part: str, block: str | None = None) -> list[dict]:
        try:
            prose_tbl = self._db.open_table(_PROSE_TABLE)
        except Exception:
            return []
        where = f"part = '{part}' AND is_operation = true"
        if block:
            where += f" AND block = '{block}'"
        try:
            rows = prose_tbl.search().where(where, prefilter=True).limit(500).to_list()
        except Exception:
            rows = []
        result = [
            {
                "block":      r.get("block", ""),
                "section":    r.get("section", ""),
                "heading":    r.get("heading", ""),
                "breadcrumb": r.get("breadcrumb", ""),
                "text":       r.get("text", ""),
            }
            for r in rows
        ]
        result.sort(key=lambda r: (r["section"], r.get("heading", "")))
        return result

    def list_operation_blocks(self, part: str) -> list[str]:
        try:
            prose_tbl = self._db.open_table(_PROSE_TABLE)
            rows = prose_tbl.search().where(
                f"part = '{part}' AND is_operation = true", prefilter=True
            ).to_list()
            seen, out = set(), []
            for r in rows:
                b = r.get("block", "")
                if b and b not in seen:
                    seen.add(b)
                    out.append(b)
            return sorted(out)
        except Exception:
            return []

    # ── catalog (list_parts, list_blocks) ─────────────────────────────────────

    def list_parts(self) -> list[tuple[str, str, str]]:
        try:
            import pandas as pd
            df = self._tbl.to_pandas()[["vendor", "part", "revision"]]
            seen: dict[tuple, str] = {}
            for _, row in df.iterrows():
                k = (row["vendor"], row["part"])
                if k not in seen:
                    seen[k] = row["revision"]
            return sorted((v, p, r) for (v, p), r in seen.items())
        except Exception:
            return []

    def list_blocks(self, part: str) -> list[tuple[str, int]]:
        try:
            import pandas as pd
            df = self._tbl.to_pandas()
            df = df[df["part"] == part][["block"]]
            counts = df["block"].value_counts()
            return sorted(counts.items())
        except Exception:
            return []

    def stats(self) -> dict:
        try:
            n_reg = self._tbl.count_rows()
        except Exception:
            n_reg = 0
        try:
            prose_tbl = self._db.open_table(_PROSE_TABLE)
            n_prose = prose_tbl.count_rows()
        except Exception:
            n_prose = 0
        return {"registers": n_reg, "prose": n_prose, "parts": len(self.list_parts())}
