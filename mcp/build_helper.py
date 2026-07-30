#!/usr/bin/env python3
"""Build helper: load mcp/.env, optionally drop ds_* Qdrant collections, then index a part.

Invoked by build.bat / build.sh.

    python build_helper.py --part ADXL345 [--reset] [--no-prose] [--no-graph]
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

try:
    from dotenv import load_dotenv
    load_dotenv(_HERE / ".env")
except ImportError:
    pass

os.environ.setdefault("PYTHONUTF8", "1")
os.environ.setdefault("PYTHONIOENCODING", "utf-8")

# Qdrant collection names (same as ds/collections.py)
_PREFIX = os.environ.get("DS_COLLECTION_PREFIX", "")
_COLLECTIONS = [f"{_PREFIX}ds_catalog",
                f"{_PREFIX}ds_evidence", f"{_PREFIX}ds_graph"]


def _reset_collections() -> None:
    """Drop all ds_* Qdrant collections (idempotent)."""
    from qdrant_client import QdrantClient

    url = os.environ.get("QDRANT_URL", "http://localhost:6333")
    api_key = os.environ.get("QDRANT_API_KEY") or None
    client = QdrantClient(url=url, api_key=api_key, timeout=60)

    for col in _COLLECTIONS:
        try:
            client.delete_collection(col)
            print(f"  dropped {col}")
        except Exception:
            pass  # collection doesn't exist yet


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--part", required=True, help="Part name, e.g. ADXL345")
    ap.add_argument("--reset", action="store_true",
                    help="Drop all ds_* Qdrant collections first (needed when changing embed model)")
    ap.add_argument("--no-prose", action="store_true", help="Deprecated; rejected by evidence build")
    ap.add_argument("--no-graph", action="store_true", help="Deprecated; rejected by evidence build")
    ap.add_argument("--no-enrich", action="store_true")
    args = ap.parse_args()

    if args.reset:
        print("Resetting Qdrant ds_* collections…")
        _reset_collections()

    from ds.ingest.build import build_part
    build_part(
        args.part,
        with_prose=not args.no_prose,
        with_graph=not args.no_graph,
        with_enrichment=not args.no_enrich,
    )


if __name__ == "__main__":
    main()
