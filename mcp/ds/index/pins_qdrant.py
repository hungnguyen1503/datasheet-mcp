"""Qdrant-backed pin store (ds_pins collection).

No vector column — payload-only filter queries.

Note: pin storage is now integrated into RegisterStoreQdrant
(regstore_qdrant.py). This module exists as a convenience alias
so that ingestion code can import PinStore from a dedicated module.
"""

from __future__ import annotations

# Re-export from the unified store for backward compatibility.
# The RegisterStoreQdrant class handles both registers and pins.
from .regstore_qdrant import RegisterStoreQdrant


class PinStoreQdrant:
    """Thin wrapper around RegisterStoreQdrant for pin-only operations.

    Used by ingestion/build.py when only pin data needs to be pushed.
    """

    def __init__(self, url: str | None = None, api_key: str | None = None):
        self._store = RegisterStoreQdrant(url=url, api_key=api_key)

    def clear_part(self, part: str) -> None:
        from qdrant_client.models import Filter, FieldCondition, MatchValue
        try:
            from .regstore_qdrant import _col
            self._store._client.delete(
                collection_name=_col("ds_pins"),
                points_selector=Filter(
                    must=[FieldCondition(key="part", match=MatchValue(value=part))]
                ),
            )
        except Exception:
            pass

    def add_pins(self, pins) -> None:
        self._store.add_pins(pins)

    def commit(self) -> None:
        self._store.commit()

    def find_pins(self, part: str, *, block=None, signal=None) -> list[dict]:
        return self._store.find_pins(part, block=block, signal=signal)

    def list_pin_blocks(self, part: str) -> list[str]:
        return self._store.list_pin_blocks(part)
