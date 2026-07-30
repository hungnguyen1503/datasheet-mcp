"""Qdrant-backed register + catalog + pin store.

Drop-in replacement for the LanceDB RegisterStore — same public interface so
query.py and mcp_server.py require minimal changes beyond swapping the import.

Collections
-----------
ds_registers : dense vector (dim from DS_EMBED_MODEL) for semantic fuzzy search
               + full register payload for exact lookup.
ds_pins      : payload-only (KEYWORD indexes) for exact pin lookups.
ds_catalog   : payload-only (KEYWORD indexes) for list_parts/list_blocks.

Env vars
--------
QDRANT_URL      (default http://localhost:6333)
QDRANT_API_KEY  (optional)
DS_EMBED_MODEL  (default BAAI/bge-base-en-v1.5, 768-dim)
"""

from __future__ import annotations

import json
import os
import time
import uuid
from functools import lru_cache
from typing import Any

from ..model import RegisterCard, BitField, Pin
from ..embed import get_embedder

# ── Import the collections module lazily to avoid circular imports at startup ──
# The _col() helper reads the DS_COLLECTION_PREFIX at runtime.


def _col(name: str) -> str:
    """Return prefixed Qdrant collection name."""
    from ..collections import get_prefix
    return f"{get_prefix()}{name}"


_BATCH = 256


class RegisterStoreQdrant:
    """Qdrant-backed register, pin, and catalog store.

    Public API matches the LanceDB RegisterStore + PinStore so query.py
    can use a single store object for all deterministic lookups.
    """

    def __init__(self, url: str | None = None, api_key: str | None = None):
        from qdrant_client import QdrantClient

        self._url = url or os.environ.get("QDRANT_URL", "http://localhost:6333")
        self._api_key = api_key or os.environ.get("QDRANT_API_KEY") or None
        self._client = QdrantClient(url=self._url, api_key=self._api_key, timeout=120)
        self._embedder = None
        self._last_ensured_prefix: str = ""

        self._pending_regs: list[dict] = []
        self._pending_pins: list[dict] = []
        self._pending_cats: list[dict] = []

    @property
    def embedder(self):
        """Lazy-load the BGE embedding model — only when actually needed."""
        if self._embedder is None:
            self._embedder = get_embedder()
        return self._embedder

    # ── collection bootstrap ──────────────────────────────────────────────

    def _ensure_collections(self) -> None:
        """Ensure all required collections + indexes exist (create only if missing)."""
        from qdrant_client.models import (
            Distance, VectorParams, PayloadSchemaType,
            TextIndexParams, TokenizerType,
        )

        existing = {c.name for c in self._client.get_collections().collections}

        # ── ds_registers (dense vector) ─────
        if _col("ds_registers") not in existing:
            self._client.create_collection(
                collection_name=_col("ds_registers"),
                vectors_config=VectorParams(size=self.embedder.dim, distance=Distance.COSINE),
            )
            for kw in ("part", "block", "revision"):
                self._client.create_payload_index(
                    collection_name=_col("ds_registers"),
                    field_name=kw,
                    field_schema=PayloadSchemaType.KEYWORD,
                )
            for txt in ("register", "name"):
                self._client.create_payload_index(
                    collection_name=_col("ds_registers"),
                    field_name=txt,
                    field_schema=TextIndexParams(
                        type="text", tokenizer=TokenizerType.WORD,
                        min_token_len=2, lowercase=True,
                    ),
                )

        # ── ds_pins (payload-only) ─────────
        if _col("ds_pins") not in existing:
            self._client.create_collection(
                collection_name=_col("ds_pins"),
                vectors_config={},
            )
            for kw in ("part", "block", "signal"):
                self._client.create_payload_index(
                    collection_name=_col("ds_pins"),
                    field_name=kw,
                    field_schema=PayloadSchemaType.KEYWORD,
                )

        # ── ds_catalog (payload-only) ───────
        if _col("ds_catalog") not in existing:
            self._client.create_collection(
                collection_name=_col("ds_catalog"),
                vectors_config={},
            )
            for kw in ("part", "vendor"):
                self._client.create_payload_index(
                    collection_name=_col("ds_catalog"),
                    field_name=kw,
                    field_schema=PayloadSchemaType.KEYWORD,
                )

    def _ensure_collections_lazy(self) -> None:
        """One-shot guard: ensure collections exist on first access per prefix."""
        from ..collections import get_prefix
        current = get_prefix()
        if self._last_ensured_prefix == current:
            return
        self._ensure_collections()
        self._last_ensured_prefix = current

    # ── write helpers ─────────────────────────────────────────────────────

    def _flush_collection(self, collection: str, pending: list[dict], with_vector: bool) -> None:
        if not pending:
            return
        from qdrant_client.models import PointStruct
        from qdrant_client.http.exceptions import UnexpectedResponse, ResponseHandlingException

        for i in range(0, len(pending), _BATCH):
            batch = pending[i: i + _BATCH]
            if with_vector:
                points = [
                    PointStruct(id=str(uuid.uuid4()), vector=p["vector"], payload=p["payload"])
                    for p in batch
                ]
            else:
                points = [
                    PointStruct(id=str(uuid.uuid4()), vector={}, payload=p["payload"])
                    for p in batch
                ]

            max_attempts = 3
            backoff = [2, 4, 8]
            for attempt in range(1, max_attempts + 1):
                try:
                    self._client.upsert(collection_name=collection, points=points)
                    break
                except UnexpectedResponse as exc:
                    status = exc.status_code or 0
                    if status and 400 <= status < 500:
                        raise
                    if attempt < max_attempts:
                        print(f"  ⚠️  upsert failed ({status}), retrying in {backoff[attempt - 1]}s…")
                        time.sleep(backoff[attempt - 1])
                    else:
                        raise
                except (ResponseHandlingException, ConnectionError, TimeoutError, OSError) as exc:
                    if attempt < max_attempts:
                        print(f"  ⚠️  upsert connection error ({type(exc).__name__}), retrying in {backoff[attempt - 1]}s…")
                        time.sleep(backoff[attempt - 1])
                    else:
                        raise
        pending.clear()

    # ── clear part ───────────────────────────────────────────────────────

    def clear_part(self, part: str) -> None:
        """Clear all data for a part across all collections."""
        from qdrant_client.models import Filter, FieldCondition, MatchValue

        self._ensure_collections_lazy()

        part_filter = Filter(
            must=[FieldCondition(key="part", match=MatchValue(value=part))]
        )
        for col_name_func in [_col("ds_registers"), _col("ds_pins"), _col("ds_catalog")]:
            try:
                self._client.delete(collection_name=col_name_func, points_selector=part_filter)
            except Exception:
                pass

    # ── registers ─────────────────────────────────────────────────────────

    def add_register(self, card: RegisterCard) -> None:
        self._pending_regs.append({
            "embed_text": f"{card.register} {card.name}",
            "payload": {
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
        # Also track in catalog
        self._pending_cats.append({
            "payload": {
                "vendor":  card.vendor,
                "part":    card.part,
                "block":   card.block,
                "revision": card.revision,
            },
        })
        if len(self._pending_regs) >= _BATCH:
            self._flush_regs()

    def _flush_regs(self) -> None:
        if not self._pending_regs:
            return
        texts = [p["embed_text"] for p in self._pending_regs]
        vecs = self.embedder.embed_documents(texts)
        for p, vec in zip(self._pending_regs, vecs):
            p["vector"] = vec
        self._flush_collection(_col("ds_registers"), self._pending_regs, with_vector=True)
        self._pending_regs = []

    def commit(self) -> None:
        self._flush_regs()
        self._flush_collection(_col("ds_pins"), self._pending_pins, with_vector=False)
        # Deduplicate catalog entries
        seen: set[tuple] = set()
        deduped = []
        for p in self._pending_cats:
            key = (p["payload"]["part"], p["payload"]["block"])
            if key not in seen:
                seen.add(key)
                deduped.append(p)
        self._flush_collection(_col("ds_catalog"), deduped, with_vector=False)
        self._pending_cats.clear()

    def close(self) -> None:
        pass  # HTTP client — nothing to close

    # ── register read ─────────────────────────────────────────────────────

    @lru_cache(maxsize=512)
    def _get_register_cached(self, part: str, register: str, block: str | None) -> list[RegisterCard]:
        from qdrant_client.models import Filter, FieldCondition, MatchValue

        self._ensure_collections_lazy()

        must = [
            FieldCondition(key="part", match=MatchValue(value=part)),
            FieldCondition(key="register", match=MatchValue(value=register)),
        ]
        if block:
            must.append(FieldCondition(key="block", match=MatchValue(value=block)))

        results, _ = self._client.scroll(
            collection_name=_col("ds_registers"),
            scroll_filter=Filter(must=must),
            limit=10,
            with_payload=True,
            with_vectors=False,
        )
        return [self._payload_to_card(r.payload) for r in results]

    def get_register(self, part: str, register: str, block: str | None = None) -> list[RegisterCard]:
        return self._get_register_cached(
            part.upper(), register.upper(), block.upper() if block else None
        )

    def search_registers(self, part: str, query: str, limit: int = 10) -> list[RegisterCard]:
        from qdrant_client.models import Filter, FieldCondition, MatchValue

        self._ensure_collections_lazy()

        must = [FieldCondition(key="part", match=MatchValue(value=part))]
        qv = self.embedder.embed_query(query)
        results = self._client.query_points(
            collection_name=_col("ds_registers"),
            query=qv,
            query_filter=Filter(must=must),
            limit=limit,
            with_payload=True,
        ).points
        seen, out = set(), []
        for r in results:
            card = self._payload_to_card(r.payload)
            if card.key not in seen:
                seen.add(card.key)
                out.append(card)
        return out

    @staticmethod
    def _payload_to_card(p: dict[str, Any]) -> RegisterCard:
        bfs = [BitField(**b) for b in json.loads(p.get("bitfields") or "[]")]
        return RegisterCard(
            vendor=p.get("vendor", ""),
            part=p.get("part", ""),
            block=p.get("block", ""),
            register=p.get("register", ""),
            name=p.get("name", ""),
            section=p.get("section") or "",
            addresses=[tuple(a) for a in json.loads(p.get("addresses") or "[]")],
            bitfields=bfs,
            notes=p.get("notes") or "",
            revision=p.get("revision") or "",
        )

    # ── operation sections (read from ds_prose) ───────────────────────────

    def get_operation(self, part: str, block: str | None = None) -> list[dict]:
        from qdrant_client.models import Filter, FieldCondition, MatchValue

        self._ensure_collections_lazy()

        must = [
            FieldCondition(key="part", match=MatchValue(value=part)),
            FieldCondition(key="content_type", match=MatchValue(value="operation")),
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
            results = []

        rows = [
            {
                "block":      r.payload.get("block", ""),
                "section":    r.payload.get("section", ""),
                "heading":    r.payload.get("heading", ""),
                "breadcrumb": r.payload.get("breadcrumb", ""),
                "text":       r.payload.get("text", ""),
            }
            for r in results
        ]
        rows.sort(key=lambda r: (r["section"], r.get("heading", "")))
        return rows

    def list_operation_blocks(self, part: str) -> list[str]:
        from qdrant_client.models import Filter, FieldCondition, MatchValue

        self._ensure_collections_lazy()

        must = [
            FieldCondition(key="part", match=MatchValue(value=part)),
            FieldCondition(key="content_type", match=MatchValue(value="operation")),
        ]
        try:
            results, _ = self._client.scroll(
                collection_name=_col("ds_prose"),
                scroll_filter=Filter(must=must),
                limit=5000,
                with_payload=["block"],
                with_vectors=False,
            )
        except Exception:
            return []

        seen = set()
        out = []
        for r in results:
            b = r.payload.get("block", "")
            if b and b not in seen:
                seen.add(b)
                out.append(b)
        return sorted(out)

    # ── catalog (list_parts, list_blocks) ─────────────────────────────────

    def list_parts(self) -> list[tuple[str, str, str]]:
        """Return sorted list of (vendor, part, revision) for all indexed parts."""
        self._ensure_collections_lazy()
        try:
            results, _ = self._client.scroll(
                collection_name=_col("ds_catalog"),
                limit=5000,
                with_payload=["vendor", "part", "revision"],
                with_vectors=False,
            )
        except Exception:
            return []

        seen: dict[tuple, str] = {}
        for r in results:
            vendor = r.payload.get("vendor", "")
            part = r.payload.get("part", "")
            rev = r.payload.get("revision", "")
            key = (vendor, part)
            if key not in seen:
                seen[key] = rev
        return sorted((v, p, r) for (v, p), r in seen.items())

    def list_blocks(self, part: str) -> list[tuple[str, int]]:
        from qdrant_client.models import Filter, FieldCondition, MatchValue

        self._ensure_collections_lazy()

        must = [FieldCondition(key="part", match=MatchValue(value=part))]
        try:
            results, _ = self._client.scroll(
                collection_name=_col("ds_registers"),
                scroll_filter=Filter(must=must),
                limit=10000,
                with_payload=["block"],
                with_vectors=False,
            )
        except Exception:
            return []

        counts: dict[str, int] = {}
        for r in results:
            b = r.payload.get("block", "")
            if b:
                counts[b] = counts.get(b, 0) + 1
        return sorted(counts.items())

    # ── pin functions ─────────────────────────────────────────────────────

    def add_pins(self, pins: list[Pin]) -> None:
        for p in pins:
            self._pending_pins.append({
                "payload": {
                    "vendor":      p.vendor,
                    "part":        p.part,
                    "block":       p.block,
                    "pin":         p.pin,
                    "signal":      p.signal,
                    "type":        p.type,
                    "description": p.description,
                },
            })
        if len(self._pending_pins) >= _BATCH:
            self._flush_collection(_col("ds_pins"), self._pending_pins, with_vector=False)

    def find_pins(self, part: str, *, block: str | None = None, signal: str | None = None) -> list[dict]:
        from qdrant_client.models import Filter, FieldCondition, MatchValue

        self._ensure_collections_lazy()

        must = [FieldCondition(key="part", match=MatchValue(value=part))]
        if block:
            must.append(FieldCondition(key="block", match=MatchValue(value=block)))
        if signal:
            must.append(FieldCondition(key="signal", match=MatchValue(value=signal)))

        try:
            results, _ = self._client.scroll(
                collection_name=_col("ds_pins"),
                scroll_filter=Filter(must=must),
                limit=5000,
                with_payload=True,
                with_vectors=False,
            )
        except Exception:
            return []

        rows = [
            {
                "block":       r.payload.get("block", ""),
                "pin":         r.payload.get("pin", ""),
                "signal":      r.payload.get("signal", ""),
                "type":        r.payload.get("type", ""),
                "description": r.payload.get("description", ""),
            }
            for r in results
        ]
        rows.sort(key=lambda r: (r["block"], r["pin"], r["signal"]))
        return rows

    def list_pin_blocks(self, part: str) -> list[str]:
        from qdrant_client.models import Filter, FieldCondition, MatchValue

        self._ensure_collections_lazy()

        must = [FieldCondition(key="part", match=MatchValue(value=part))]
        try:
            results, _ = self._client.scroll(
                collection_name=_col("ds_pins"),
                scroll_filter=Filter(must=must),
                limit=5000,
                with_payload=["block"],
                with_vectors=False,
            )
        except Exception:
            return []

        seen = set()
        out = []
        for r in results:
            b = r.payload.get("block", "")
            if b and b not in seen:
                seen.add(b)
                out.append(b)
        return sorted(out)

    # ── stats ─────────────────────────────────────────────────────────────

    def stats(self) -> dict:
        def _count(col_name: str) -> int:
            try:
                return self._client.get_collection(col_name).points_count or 0
            except Exception:
                return 0

        return {
            "registers": _count(_col("ds_registers")),
            "pins": _count(_col("ds_pins")),
            "parts": len(self.list_parts()),
        }
