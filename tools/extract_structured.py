#!/usr/bin/env python3
"""Stage 2 — LLM-assisted structured extraction from MinerU markdown.

Scans each section's markdown for register-map tables, bit-field tables, and
pin tables, sends them to an OpenAI-compatible LLM, and writes:
    data/<PART>/registers.json
    data/<PART>/pins.json
    data/<PART>/catalog.json

Results are cached in data/<PART>/.extract_cache.json — the run is fully
resumable with no re-work for already-processed sections.

Configure the LLM backend in mcp/.env:
    EXTRACT_LLM_BACKEND=lmstudio    # or ollama | openai
    EXTRACT_LLM_HOST=http://localhost:1234/v1
    EXTRACT_LLM_MODEL=qwen3:14b
    EXTRACT_LLM_KEY=lm-studio
    EXTRACT_WORKERS=4

Usage:
    python tools/extract_structured.py --part ADXL345
    python tools/extract_structured.py --part ADXL345 --reset   # clear cache first
"""

from __future__ import annotations

import argparse

import _bootstrap  # noqa: F401

from ds.ingest.extract import extract_structured


def main() -> None:
    ap = argparse.ArgumentParser(
        description="LLM extraction of registers/pins from MinerU markdown."
    )
    ap.add_argument("--part", required=True, help="Part name, e.g. ADXL345")
    ap.add_argument("--reset", action="store_true",
                    help="Clear extraction cache before running")
    ap.add_argument("--workers", type=int, default=0,
                    help="Parallel section workers (default: EXTRACT_WORKERS env or 4)")
    args = ap.parse_args()

    kw = {}
    if args.workers:
        kw["workers"] = args.workers

    cards, pins, meta = extract_structured(args.part, reset=args.reset, **kw)
    print(f"\nSummary for {args.part}:")
    print(f"  Vendor  : {meta.vendor or '(unknown)'}")
    print(f"  Title   : {meta.title or '(unknown)'}")
    print(f"  Revision: {meta.revision or '(unknown)'}")
    print(f"  Blocks  : {', '.join(meta.blocks) or '(none)'}")
    print(f"  Registers: {len(cards)}")
    print(f"  Pins     : {len(pins)}")
    print(f"\nNext: mcp/build.bat --part {args.part}")


if __name__ == "__main__":
    main()
