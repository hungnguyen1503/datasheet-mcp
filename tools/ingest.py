#!/usr/bin/env python3
"""Unified datasheet ingest CLI — fuzzy PDF finder + full 3-stage pipeline.

Stage 0 : Fuzzy multi-select PDF(s) from a directory (InquirerPy TUI or numbered fallback)
Stage 1 : PDF → chapter markdown  (MinerU, GPU/CPU auto-detected)
Stage 2 : Markdown → registers/pins JSON  (LLM extraction, resumable cache)
Stage 3 : JSON → LanceDB index  (embed + vector + FTS)

Usage:
    python tools/ingest.py                          # scan repo root, fuzzy select
    python tools/ingest.py --dir /path/to/pdfs      # scan custom directory
    python tools/ingest.py --pdf /path/to/file.pdf  # single file, skip TUI

    # Pipeline control:
    python tools/ingest.py --no-extract             # skip LLM extraction (use cached JSON)
    python tools/ingest.py --no-prose --no-graph    # registers + pins only
    python tools/ingest.py --reset                  # drop existing LanceDB tables first
    python tools/ingest.py --backend pymupdf        # force PyMuPDF (no MinerU needed)
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import _bootstrap  # noqa: F401 — puts mcp/ on sys.path, loads .env

from ds import catalog
from pdf_to_md import setup_part_folder, extract_mineru, extract_pymupdf, _choose_mineru_backend, _has_cuda


# ── PDF discovery ─────────────────────────────────────────────────────────────

def find_pdfs(search_dir: Path) -> list[Path]:
    """Return all PDFs in search_dir (non-recursive in data/ to avoid source.pdf duplicates)."""
    pdfs: list[Path] = []
    for p in sorted(search_dir.glob("*.pdf")):
        pdfs.append(p)
    # Also include PDFs one level deep (common drop folder pattern), excluding data/
    for sub in sorted(search_dir.iterdir()):
        if sub.is_dir() and sub.name not in ("data", ".lancedb", "__pycache__", ".git"):
            for p in sorted(sub.glob("*.pdf")):
                if p.parent.name != sub.name:  # skip data/<part>/source.pdf
                    pdfs.append(p)
    return pdfs


# ── Interactive PDF selector ──────────────────────────────────────────────────

def _select_with_inquirerpy(pdfs: list[Path]) -> list[Path]:
    from InquirerPy import inquirer
    choices = [{"name": f"{p.name}  ({p.parent.name}/)", "value": str(p)} for p in pdfs]
    selected = inquirer.fuzzy(
        message="Select datasheet PDF(s) to ingest  [Space=select, Enter=confirm]:",
        choices=choices,
        multiselect=True,
        max_height="70%",
        validate=lambda x: len(x) > 0,
        invalid_message="Select at least one PDF.",
    ).execute()
    return [Path(s) for s in selected]


def _select_numbered_fallback(pdfs: list[Path]) -> list[Path]:
    """Simple numbered menu with comma-separated multi-select."""
    print("\nAvailable PDFs:")
    for i, p in enumerate(pdfs, 1):
        print(f"  {i:3d}.  {p.name}  ({p.parent.name}/)")
    print()
    raw = input("Enter numbers to ingest (comma-separated, e.g. 1,3,5 or 'all'): ").strip()
    if raw.lower() == "all":
        return pdfs
    selected: list[Path] = []
    for tok in raw.split(","):
        tok = tok.strip()
        if tok.isdigit():
            idx = int(tok) - 1
            if 0 <= idx < len(pdfs):
                selected.append(pdfs[idx])
    return selected


def select_pdfs(search_dir: Path) -> list[Path]:
    pdfs = find_pdfs(search_dir)
    if not pdfs:
        raise SystemExit(f"No PDFs found in {search_dir}")
    print(f"Found {len(pdfs)} PDF(s) in {search_dir}\n")

    # Try InquirerPy first, fall back to numbered menu
    try:
        import InquirerPy  # noqa: F401
        return _select_with_inquirerpy(pdfs)
    except ImportError:
        print("(InquirerPy not installed — using numbered menu. "
              "Install with: pip install InquirerPy)")
        return _select_numbered_fallback(pdfs)


# ── Per-PDF pipeline ──────────────────────────────────────────────────────────

def run_pipeline(
    pdf: Path,
    *,
    backend: str,
    mineru_backend: str,
    no_extract: bool,
    no_prose: bool,
    no_graph: bool,
    reset: bool,
) -> None:
    print(f"\n{'='*60}")
    print(f"  Ingesting: {pdf.name}")
    print(f"{'='*60}")

    # Stage 0: folder setup
    part, _ = setup_part_folder(pdf)
    print(f"  Part: {part}")

    # Stage 1: PDF → Markdown
    pdf_src = catalog.source_pdf(part)
    md_root = catalog.md_dir(part)
    if md_root.exists() and any(md_root.iterdir()):
        print(f"  Stage 1: markdown already exists, skipping (delete data/{part}/MD/ to re-run)")
    else:
        print(f"  Stage 1: PDF → Markdown  [{backend}/{mineru_backend}]")
        if backend == "mineru":
            extract_mineru(pdf_src, part, mineru_backend, "high")
        else:
            extract_pymupdf(pdf_src, part)

    # Stage 2: Markdown → structured JSON
    regs_path = catalog.registers_json(part)
    if no_extract and regs_path.exists():
        print(f"  Stage 2: skipped (--no-extract, using cached {regs_path.name})")
    else:
        print(f"  Stage 2: LLM extraction…")
        from ds.ingest.extract import extract_structured
        extract_structured(part)

    # Stage 3: JSON → LanceDB
    print(f"  Stage 3: embedding + indexing…")
    if reset:
        print("  Resetting LanceDB tables…")
        from ds.db import get_db
        db = get_db()
        for tbl in ["ds_registers", "ds_prose", "ds_pins", "ds_graph"]:
            if tbl in db.list_tables():
                db.drop_table(tbl)
                print(f"    dropped {tbl}")

    from ds.ingest.build import build_part
    stats = build_part(part, with_prose=not no_prose, with_graph=not no_graph)
    print(f"  Done: {stats}")


# ── main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    cuda = _has_cuda()
    auto_backend = _choose_mineru_backend()
    device_label = "GPU (hybrid-engine)" if cuda else "CPU (pipeline)"

    ap = argparse.ArgumentParser(
        description=f"Datasheet ingest: fuzzy PDF selector → full pipeline  [{device_label}]"
    )
    # Source selection
    group = ap.add_mutually_exclusive_group()
    group.add_argument("--pdf",  default="", help="Ingest a single PDF (skip TUI)")
    group.add_argument("--dir",  default="", help="Directory to scan for PDFs (default: repo root)")

    # Pipeline control
    ap.add_argument("--backend", default="mineru", choices=["mineru", "pymupdf"],
                    help="PDF extraction backend")
    ap.add_argument("--mineru-backend", default=auto_backend,
                    choices=["hybrid-engine", "pipeline"],
                    help=f"MinerU engine (auto: {auto_backend})")
    ap.add_argument("--no-extract", action="store_true",
                    help="Skip LLM extraction if registers.json already exists")
    ap.add_argument("--no-prose",   action="store_true", help="Skip prose index")
    ap.add_argument("--no-graph",   action="store_true", help="Skip graph build")
    ap.add_argument("--reset",      action="store_true", help="Drop existing tables before indexing")
    args = ap.parse_args()

    # Select PDFs
    if args.pdf:
        pdfs = [Path(args.pdf).resolve()]
        if not pdfs[0].exists():
            raise SystemExit(f"PDF not found: {pdfs[0]}")
    else:
        search_dir = Path(args.dir).resolve() if args.dir else catalog.REPO_ROOT
        pdfs = select_pdfs(search_dir)

    if not pdfs:
        print("No PDFs selected. Exiting.")
        sys.exit(0)

    print(f"\nSelected {len(pdfs)} PDF(s):")
    for p in pdfs:
        print(f"  • {p.name}")

    # Run pipeline for each selected PDF
    for pdf in pdfs:
        run_pipeline(
            pdf,
            backend=args.backend,
            mineru_backend=args.mineru_backend,
            no_extract=args.no_extract,
            no_prose=args.no_prose,
            no_graph=args.no_graph,
            reset=args.reset,
        )

    print(f"\nAll done! {len(pdfs)} part(s) indexed.")
    print("Start the MCP server with:  python mcp/server.py")


if __name__ == "__main__":
    main()
