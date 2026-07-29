#!/usr/bin/env python3
"""Zero-interaction pipeline runner for a single datasheet part.

Auto-detects the first incomplete stage and resumes from there.
Uses sentinel files (.stage_N_done) for crash-safe completion tracking.

Usage:
  python tools/pipeline.py --part ADXL345           full pipeline, auto-resume
  python tools/pipeline.py --part ADXL345 --yes     non-interactive
  python tools/pipeline.py --part ADXL345 --fresh   ignore prior state
  python tools/pipeline.py --part ADXL345 --only 4  re-index only
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

import _bootstrap  # noqa: F401
from _scaffold import (
    DATA_ROOT, find_source_pdf, copy_source_pdf, setup_part_dir,
    detect_vendor, sanitize_part_name,
)
from build_all import (
    check_stage1, check_stage2, check_stage3, check_stage4,
    run_stage, STAGES,
)


def _sentinel(part: str, stage: int) -> Path:
    return DATA_ROOT / part / f".stage_{stage}_done"


def _all_sentinels(part: str) -> list[Path]:
    return [_sentinel(part, s) for s in range(1, 5)]


def find_first_incomplete(part: str) -> int:
    """Return the first stage (1-4) that is not complete, or 5 if all done."""
    for s in range(1, 5):
        if not _sentinel(part, s).exists():
            return s
    return 5


def clear_sentinels(part: str) -> None:
    """Delete all sentinel files for a fresh start."""
    for p in _all_sentinels(part):
        p.unlink(missing_ok=True)


def write_sentinel(part: str, stage: int) -> None:
    _sentinel(part, stage).write_text("done")


def main():
    ap = argparse.ArgumentParser(
        description="Zero-interaction datasheet pipeline (auto-resume, crash-safe)"
    )
    ap.add_argument("--part", required=True, help="Part name, e.g. ADXL345")
    ap.add_argument("--from", dest="from_stage", type=int, default=0,
                    help="Start from stage (1-4, default: auto-detect)")
    ap.add_argument("--only", type=int, default=0, help="Run only this stage")
    ap.add_argument("--fresh", action="store_true", help="Delete sentinels and start fresh")
    ap.add_argument("--no-describe", action="store_true", help="Skip VLM descriptions")
    ap.add_argument("--no-prose", action="store_true", help="Skip prose index")
    ap.add_argument("--no-graph", action="store_true", help="Skip graph build")
    ap.add_argument("--reset", action="store_true", help="Drop Qdrant collections")
    ap.add_argument("--mineru-backend", default="", choices=["", "hybrid-engine", "pipeline"])
    ap.add_argument("--workers", type=int, default=0)
    ap.add_argument("--yes", "-y", action="store_true", help="Non-interactive (don't ask)")
    args = ap.parse_args()

    part = args.part

    # Ensure part directory exists
    setup_part_dir(part, vendor=detect_vendor(part))

    # Fresh start
    if args.fresh:
        clear_sentinels(part)
        print("Cleared sentinel files — starting fresh.")

    # Stage selection
    if args.only:
        stages_to_run = [args.only]
    elif args.from_stage:
        stages_to_run = list(range(args.from_stage, 5))
    else:
        first = find_first_incomplete(part)
        if first > 4:
            print(f"All stages complete for '{part}'. Use --fresh to re-run.")
            return
        stages_to_run = list(range(first, 5))

    print(f"Part: {part}")
    print(f"Stages: {', '.join(f'Stage {s} ({STAGES[s]})' for s in stages_to_run)}")

    # Copy source PDF if needed
    pdf = find_source_pdf(part)
    if pdf and pdf.parent != (DATA_ROOT / part):
        copy_source_pdf(pdf, part)
        print(f"Copied source PDF: {pdf.name}")

    # Run stages
    for stage in stages_to_run:
        ok = run_stage(part, stage, args)
        if not ok:
            print(f"\nStage {stage} failed.")
            if not args.yes:
                print("Fix the issue and re-run — pipeline will resume from this stage.")
            sys.exit(1)
        write_sentinel(part, stage)
        print(f"  [sentinel] .stage_{stage}_done written")

    print(f"\nPipeline complete for '{part}'! All 4 stages done.")


if __name__ == "__main__":
    main()
