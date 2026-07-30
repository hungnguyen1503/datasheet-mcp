#!/usr/bin/env python3
"""Datasheet MCP — Full pipeline orchestrator.

4-stage pipeline:
  Stage 1  pdf_to_md.py          PDF -> Markdown via MinerU
  Stage 2  extract_structured.py  Markdown -> JSON (heuristic, no LLM)
  Stage 3  describe_images.py    VLM figure descriptions (optional)
  Stage 4  mcp/build.bat/sh      Embed + push to Qdrant

Smart resumability: each stage is skipped when its output already exists.

Usage:
  python tools/build_all.py                           interactive
  python tools/build_all.py --part ADXL345            single part
  python tools/build_all.py --part ADXL345 --yes      non-interactive
  python tools/build_all.py --part ADXL345 --from 2   resume from stage 2
  python tools/build_all.py --part ADXL345 --only 4   re-index only
  python tools/build_all.py --part ADXL345 --index-only  alias for --only 4
  python tools/build_all.py --part ADXL345 --no-describe  skip VLM stage
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

import _bootstrap  # noqa: F401
from _scaffold import (
    REPO_ROOT, DATA_ROOT, discover_pdfs, detect_vendor, sanitize_part_name,
    copy_source_pdf, setup_part_dir, has_markdown, has_structured_data,
    md_files, md_dir,
)

STAGES = {
    1: "PDF → Markdown (MinerU)",
    2: "Markdown → JSON (heuristic)",
    3: "VLM figure descriptions",
    4: "Embed + push to Qdrant",
}

_SCRIPT_DIR = Path(__file__).resolve().parent


# ── Part discovery ────────────────────────────────────────────────────────

def discover_parts() -> list[str]:
    """Find parts from PDFs in the repo root + data/ directories."""
    parts: set[str] = set()
    # From PDFs in repo root
    for pdf in discover_pdfs(REPO_ROOT):
        name = sanitize_part_name(pdf.stem)
        if name:
            parts.add(name)
    # From data/ subdirectories with source.pdf
    for d in sorted(DATA_ROOT.iterdir()) if DATA_ROOT.is_dir() else []:
        if d.is_dir() and (d / "source.pdf").exists():
            parts.add(d.name)
    # From data/ subdirectories with any content
    for d in sorted(DATA_ROOT.iterdir()) if DATA_ROOT.is_dir() else []:
        if d.is_dir() and d.name not in parts:
            parts.add(d.name)
    return sorted(parts)


def find_source_pdf(part: str) -> Path | None:
    """Find the source PDF for a part."""
    # Check data/<part>/source.pdf first
    src = DATA_ROOT / part / "source.pdf"
    if src.exists():
        return src
    # Check repo root for matching PDFs
    for pdf in discover_pdfs(REPO_ROOT):
        if sanitize_part_name(pdf.stem).upper() == part.upper():
            return pdf
    return None


# ── Stage checks ──────────────────────────────────────────────────────────

def check_stage1(part: str) -> bool:
    """Return True if Stage 1 appears complete."""
    m = md_dir(part)
    return m.is_dir() and any(m.iterdir())


def check_stage2(part: str) -> bool:
    """Return True if Stage 2 appears complete."""
    return has_structured_data(part)


def check_stage3(part: str) -> bool:
    """Return True if Stage 3 appears complete."""
    cache = md_dir(part) / ".describe_images.json"
    if not cache.exists():
        return False
    try:
        import json
        data = json.loads(cache.read_text(encoding="utf-8"))
        # Count bare images vs cache entries
        bare_count = 0
        for md in md_files(part):
            import re
            text = md.read_text(encoding="utf-8", errors="replace")
            bare_count += len(re.findall(r'!\[\]\(([^)]+)\)', text))
        return bare_count == 0 or len(data) >= bare_count
    except Exception:
        return False


def check_stage4(part: str) -> bool:
    """Return True if part appears indexed in Qdrant."""
    # Simple check: if catalog data exists, assume indexed
    # More accurate: query Qdrant, but that requires imports
    j = DATA_ROOT / part / "catalog.json"
    return j.exists()


# ── Overview table ────────────────────────────────────────────────────────

def print_overview(parts: list[str]) -> None:
    """Show readiness table for all parts."""
    print()
    print(f"{'Part':<24} {'S1(MD)':<8} {'S2(JSON)':<10} {'S3(VLM)':<8} {'S4(Qdrant)':<10}")
    print("-" * 62)
    for p in parts:
        s1 = "✓" if check_stage1(p) else "—"
        s2 = "✓" if check_stage2(p) else "—"
        s3 = "✓" if check_stage3(p) else "—"
        s4 = "✓" if check_stage4(p) else "—"
        print(f"{p:<24} {s1:<8} {s2:<10} {s3:<8} {s4:<10}")
    print()


# ── Stage execution ───────────────────────────────────────────────────────

def run_stage(part: str, stage: int, args: argparse.Namespace) -> bool:
    """Run one pipeline stage. Returns True on success."""
    print(f"\n{'='*60}")
    print(f"  Stage {stage}: {STAGES[stage]}")
    print(f"{'='*60}")

    scripts_dir = _SCRIPT_DIR
    mcp_dir = REPO_ROOT / "mcp"
    venv_python = REPO_ROOT / ".venv" / "Scripts" / "python.exe"
    if not venv_python.exists():
        venv_python = REPO_ROOT / ".venv" / "bin" / "python"
    python = str(venv_python) if venv_python.exists() else sys.executable

    if stage == 1:
        pdf = find_source_pdf(part)
        if not pdf:
            print(f"  No source PDF found for part '{part}'. Skip.")
            return False
        if check_stage1(part):
            print(f"  Markdown already exists for '{part}'. Skip.")
            return True
        cmd = [python, str(scripts_dir / "pdf_to_md.py"), "--pdf", str(pdf)]
        if args.mineru_backend:
            cmd += ["--mineru-backend", args.mineru_backend]

    elif stage == 2:
        if check_stage2(part):
            print(f"  Structured data already exists for '{part}'. Skip.")
            return True
        cmd = [python, str(scripts_dir / "extract_structured.py"), "--part", part]

    elif stage == 3:
        if args.no_describe:
            print("  Skipped (--no-describe).")
            return True
        if check_stage3(part):
            print(f"  All images already described for '{part}'. Skip.")
            return True
        cmd = [python, str(scripts_dir / "describe_images.py"), "--part", part]
        if args.workers:
            cmd += ["--workers", str(args.workers)]

    elif stage == 4:
        # Use build.bat on Windows, build.sh on Linux
        if sys.platform == "win32":
            build_script = str(mcp_dir / "build.bat")
            cmd = [build_script, "--part", part]
        else:
            build_script = str(mcp_dir / "build.sh")
            cmd = ["bash", build_script, "--part", part]
        if args.no_prose:
            cmd.append("--no-prose")
        if args.no_graph:
            cmd.append("--no-graph")
        if args.no_enrich:
            cmd.append("--no-enrich")
        if args.reset:
            cmd.append("--reset")

    else:
        print(f"  Unknown stage: {stage}")
        return False

    print(f"  Command: {' '.join(cmd)}")
    result = subprocess.run(cmd)
    if result.returncode != 0:
        print(f"  Stage {stage} FAILED (exit code {result.returncode})")
        return False
    print(f"  Stage {stage} complete.")
    return True


# ── Main ──────────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser(
        description="Datasheet MCP — Full 4-stage pipeline orchestrator"
    )
    ap.add_argument("--part", default="", help="Part name (e.g. ADXL345). Omit for interactive.")
    ap.add_argument("--from", dest="from_stage", type=int, default=1,
                    help="Start from this stage (1-4, default: 1)")
    ap.add_argument("--only", type=int, default=0,
                    help="Run only this stage (1-4)")
    ap.add_argument("--index-only", action="store_true", help="Alias for --only 4")
    ap.add_argument("--no-describe", action="store_true", help="Skip VLM figure descriptions")
    ap.add_argument("--no-prose", action="store_true", help="Deprecated; evidence requires prose")
    ap.add_argument("--no-graph", action="store_true", help="Deprecated; evidence requires graph data")
    ap.add_argument("--no-enrich", action="store_true",
                    help="Skip optional local semantic enrichment")
    ap.add_argument("--reset", action="store_true", help="Drop Qdrant collections before indexing")
    ap.add_argument("--mineru-backend", default="", choices=["", "hybrid-engine", "pipeline"],
                    help="MinerU backend (default: auto-detect)")
    ap.add_argument("--workers", type=int, default=0, help="VLM workers (default: auto)")
    ap.add_argument("--yes", "-y", action="store_true", help="Non-interactive: run all stages")
    args = ap.parse_args()

    # Resolve index-only
    if args.index_only:
        args.only = 4

    # Discover parts
    parts = discover_parts()
    if not parts:
        print("No parts found. Place PDFs in the repo root or data/<PART>/source.pdf")
        sys.exit(1)

    if args.part:
        selected = [p for p in parts if p.upper() == args.part.upper()]
        if not selected:
            print(f"Part '{args.part}' not found. Available: {', '.join(parts)}")
            sys.exit(1)
    else:
        print_overview(parts)
        if args.yes:
            selected = parts
        else:
            print("Available parts:")
            for i, p in enumerate(parts, 1):
                print(f"  {i}. {p}")
            print("\nEnter number or 'all' (default: all): ", end="")
            choice = input().strip()
            if not choice or choice.lower() == "all":
                selected = parts
            elif choice.isdigit():
                idx = int(choice) - 1
                if 0 <= idx < len(parts):
                    selected = [parts[idx]]
                else:
                    print("Invalid selection.")
                    sys.exit(1)
            else:
                selected = [p for p in parts if p.upper() == choice.upper()]
                if not selected:
                    print(f"Part '{choice}' not found.")
                    sys.exit(1)

    print(f"\nParts: {', '.join(selected)}")

    # Determine stages
    if args.only:
        stages = [args.only]
    else:
        stages = list(range(args.from_stage, 5))

    # Confirm
    if not args.yes:
        print(f"\nStages to run: {', '.join(f'Stage {s} ({STAGES[s]})' for s in stages)}")
        print("Proceed? [Y/n]: ", end="")
        if input().strip().lower() in ("n", "no"):
            print("Cancelled.")
            return

    # Run stages for each part
    for part in selected:
        print(f"\n{'#'*60}")
        print(f"  Part: {part}")
        print(f"{'#'*60}")

        # Stage 1: Setup part directory + copy PDF
        pdf = find_source_pdf(part)
        if pdf and pdf.parent != (DATA_ROOT / part):
            setup_part_dir(part, vendor=detect_vendor(part))
            copy_source_pdf(pdf, part)

        ok = True
        for stage in stages:
            if not run_stage(part, stage, args):
                ok = False
                if not args.yes:
                    print(f"\nStage {stage} failed. Continue? [y/N]: ", end="")
                    if input().strip().lower() != "y":
                        break
                else:
                    break

        if ok:
            print(f"\n  Part '{part}' pipeline complete!")

    print("\nDone. Start the server with:  python mcp/server.py")


if __name__ == "__main__":
    main()
