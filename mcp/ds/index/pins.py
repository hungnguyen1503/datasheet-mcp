"""LanceDB-backed pin store (ds_pins table).

No vector column — payload-only filter queries.
"""

from __future__ import annotations

import uuid

import pyarrow as pa

from ..model import Pin
from ..db import get_db

_TABLE = "ds_pins"

_SCHEMA = pa.schema([
    pa.field("id",          pa.string()),
    pa.field("vendor",      pa.string()),
    pa.field("part",        pa.string()),
    pa.field("block",       pa.string()),
    pa.field("pin",         pa.string()),
    pa.field("signal",      pa.string()),
    pa.field("type",        pa.string()),
    pa.field("description", pa.string()),
])


class PinStore:
    def __init__(self):
        self._db = get_db()
        self._pending: list[dict] = []
        self._tbl = self._open_or_create()

    def _open_or_create(self):
        if _TABLE not in self._db.list_tables():
            return self._db.create_table(_TABLE, schema=_SCHEMA)
        return self._db.open_table(_TABLE)

    def clear_part(self, part: str) -> None:
        try:
            self._tbl.delete(f"part = '{part}'")
        except Exception:
            pass

    def add_pins(self, pins: list[Pin]) -> None:
        for p in pins:
            self._pending.append({
                "id":          str(uuid.uuid4()),
                "vendor":      p.vendor,
                "part":        p.part,
                "block":       p.block,
                "pin":         p.pin,
                "signal":      p.signal,
                "type":        p.type,
                "description": p.description,
            })
        if len(self._pending) >= 256:
            self.commit()

    def commit(self) -> None:
        if self._pending:
            self._tbl.add(self._pending)
            self._pending = []

    def find_pins(self, part: str, *, block: str | None = None, signal: str | None = None) -> list[dict]:
        where = f"part = '{part}'"
        if block:
            where += f" AND block = '{block}'"
        if signal:
            where += f" AND signal = '{signal}'"
        try:
            rows = self._tbl.search().where(where, prefilter=True).limit(5000).to_list()
        except Exception:
            rows = []
        result = [
            {
                "block":       r.get("block", ""),
                "pin":         r.get("pin", ""),
                "signal":      r.get("signal", ""),
                "type":        r.get("type", ""),
                "description": r.get("description", ""),
            }
            for r in rows
        ]
        result.sort(key=lambda r: (r["block"], r["pin"], r["signal"]))
        return result

    def list_pin_blocks(self, part: str) -> list[str]:
        try:
            rows = self._tbl.search().where(f"part = '{part}'", prefilter=True).to_list()
            seen, out = set(), []
            for r in rows:
                b = r.get("block", "")
                if b and b not in seen:
                    seen.add(b)
                    out.append(b)
            return sorted(out)
        except Exception:
            return []
