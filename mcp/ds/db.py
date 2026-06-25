"""Shared LanceDB connection (process-wide singleton).

Data is stored under data/.lancedb/ by default — no server required.
Override with DS_DB_PATH env var (absolute or relative to repo root).
"""

from __future__ import annotations

import os
import threading
from functools import lru_cache
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_DEFAULT_DB_PATH = REPO_ROOT / "data" / ".lancedb"

_db = None
_lock = threading.Lock()


def get_db():
    """Return the process-wide LanceDB connection (created on first call)."""
    global _db
    if _db is None:
        with _lock:
            if _db is None:
                import lancedb
                raw = os.environ.get("DS_DB_PATH", "")
                p = Path(raw) if raw else _DEFAULT_DB_PATH
                if not p.is_absolute():
                    p = (REPO_ROOT / p).resolve()
                p.mkdir(parents=True, exist_ok=True)
                _db = lancedb.connect(str(p))
    return _db
