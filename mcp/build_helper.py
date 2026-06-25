#!/usr/bin/env python3
"""Build helper: load mcp/.env, optionally drop ds_* LanceDB tables, then index a part.

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

_TABLES = ["ds_registers", "ds_prose", "ds_pins", "ds_graph"]


def _reset_tables() -> None:
    from ds.db import get_db
    db = get_db()
    existing = db.list_tables()
    for tbl in _TABLES:
        if tbl in existing:
            db.drop_table(tbl)
            print(f"  dropped {tbl}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--part", required=True, help="Part name, e.g. ADXL345")
    ap.add_argument("--reset", action="store_true",
                    help="Drop all ds_* tables first (needed when changing embed model)")
    ap.add_argument("--no-prose", action="store_true")
    ap.add_argument("--no-graph", action="store_true")
    args = ap.parse_args()

    if args.reset:
        print("Resetting LanceDB tables…")
        _reset_tables()

    from ds.ingest.build import build_part
    build_part(args.part, with_prose=not args.no_prose, with_graph=not args.no_graph)


if __name__ == "__main__":
    main()
