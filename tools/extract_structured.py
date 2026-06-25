#!/usr/bin/env python3
"""Stage 2 — Heuristic structured extraction from MinerU markdown.

Scans each section's markdown for register-map tables, bit-field tables, and
pin tables using column-header pattern matching. No LLM, no API key, no network.

Output files written to data/<PART>/:
    registers.json   — list of RegisterCard dicts
    pins.json        — list of Pin dicts
    catalog.json     — PartMeta (vendor, title, blocks, revision)

Usage:
    python tools/extract_structured.py --part ADXL345
    python tools/extract_structured.py --part ADXL345 --reset  # clear & re-parse
"""

from __future__ import annotations

import argparse

import _bootstrap  # noqa: F401

from ds.ingest.extract import extract_structured


def main() -> None:
    ap = argparse.ArgumentParser(
        description="Heuristic register/pin extraction from MinerU markdown (no LLM)."
    )
    ap.add_argument("--part", required=True, help="Part name, e.g. ADXL345")
    ap.add_argument("--reset", action="store_true",
                    help="Re-parse even if registers.json already exists")
    args = ap.parse_args()

    cards, pins, meta = extract_structured(args.part, reset=args.reset)

    print(f"\nSummary for {args.part}:")
    print(f"  Vendor   : {meta.vendor or '(unknown)'}")
    print(f"  Title    : {meta.title or '(unknown)'}")
    print(f"  Revision : {meta.revision or '(unknown)'}")
    print(f"  Blocks   : {', '.join(meta.blocks) or '(none)'}")
    print(f"  Registers: {len(cards)}")
    print(f"  Pins     : {len(pins)}")
    print(f"\nNext: mcp/build.bat --part {args.part}")


if __name__ == "__main__":
    main()
