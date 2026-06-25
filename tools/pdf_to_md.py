#!/usr/bin/env python3
"""Stage 1 — Datasheet PDF → chapter markdown.

GPU/CPU auto-detection:
  GPU (CUDA available): mineru --backend hybrid-engine  (VLM table understanding)
  CPU (no CUDA):        mineru --backend pipeline        (text-only, much faster on CPU)

Folder creation:
  Given any PDF path, derives part_name from the filename stem, creates
  data/<part_name>/ and copies the PDF to data/<part_name>/source.pdf.

Usage:
    python tools/pdf_to_md.py --part ADXL345
    python tools/pdf_to_md.py --pdf /downloads/ADXL345.pdf    # auto-names
    python tools/pdf_to_md.py --pdf file.pdf --backend pymupdf
    python tools/pdf_to_md.py --pdf file.pdf --mineru-backend pipeline
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
from pathlib import Path

import _bootstrap  # noqa: F401  — puts mcp/ on sys.path and loads .env

from ds import catalog


# ── GPU/CPU detection ────────────────────────────────────────────────────────

def _has_cuda() -> bool:
    try:
        import torch
        return torch.cuda.is_available()
    except ImportError:
        return False


def _choose_mineru_backend() -> str:
    """Auto-select MinerU backend based on hardware. Explicit override still possible."""
    return "hybrid-engine" if _has_cuda() else "pipeline"


# ── Folder setup from PDF path ────────────────────────────────────────────────

def _sanitize(name: str) -> str:
    """Turn a filename stem into a safe part name."""
    name = re.sub(r"[^\w.-]", "_", name)
    name = re.sub(r"_+", "_", name).strip("_.-")
    return name[:80] or "datasheet"


def setup_part_folder(pdf_path: Path) -> tuple[str, str]:
    """Derive part name from PDF stem; create data/<part>/ and copy PDF there.

    Returns (part_name, backend_hint) where backend_hint is "gpu" or "cpu".
    """
    part_name = _sanitize(pdf_path.stem)
    dest_dir = catalog.part_dir(part_name)
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / "source.pdf"
    if pdf_path.resolve() != dest.resolve() and not dest.exists():
        shutil.copy2(pdf_path, dest)
        print(f"  Copied {pdf_path.name} → data/{part_name}/source.pdf")
    return part_name, dest_dir


# ── helpers ───────────────────────────────────────────────────────────────────

def _safe(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", name).strip("_")[:80] or "section"


def _hlevel(lvl: int) -> str:
    return "#" * max(1, min(lvl + 1, 6))


# ── MinerU backend ────────────────────────────────────────────────────────────

_CHAPTER_RE = re.compile(r"(?m)^#{1,3}\s+\d+[\.\s]")
_HEADING_RE = re.compile(r"^#{1,6}\s+(.*?)\s*#*\s*$")
_IMG_REF_RE = re.compile(r"!\[[^\]]*\]\((images/[^)\s]+)\)")


def _run_mineru(pdf: Path, raw: Path, backend: str, effort: str) -> bool:
    cuda = _has_cuda()
    device_mode = os.environ.get("MINERU_DEVICE_MODE", "cuda" if cuda else "cpu")
    env = dict(os.environ, MINERU_DEVICE_MODE=device_mode)
    cmd = ["mineru", "-p", str(pdf), "-o", str(raw), "-m", "txt", "-b", backend, "-l", "en"]
    if backend.startswith("hybrid"):
        cmd += ["--effort", effort]
    print(f"  $ {' '.join(cmd)}  (device={device_mode})")
    try:
        subprocess.run(cmd, check=False, env=env)
    except FileNotFoundError:
        raise SystemExit("MinerU not found. Install it or use --backend pymupdf.")
    return bool(list(raw.rglob("*.md")))


def _find_md(raw: Path) -> Path | None:
    mds = list(raw.rglob("*.md"))
    return max(mds, key=lambda p: p.stat().st_size) if mds else None


def _chapter_title(seg: str, fallback: str) -> str:
    for line in seg.splitlines():
        m = _HEADING_RE.match(line)
        if m and m.group(1).strip():
            return m.group(1).strip()
    return fallback


def extract_mineru(pdf: Path, part: str, mineru_backend: str, effort: str) -> int:
    md_root = catalog.md_dir(part)
    raw = md_root.parent / "_mineru_raw"
    if raw.exists():
        shutil.rmtree(raw, ignore_errors=True)
    raw.mkdir(parents=True, exist_ok=True)

    ok = _run_mineru(pdf, raw, mineru_backend, effort)
    if not ok and mineru_backend.startswith("hybrid"):
        print("  [!] hybrid-engine produced no output — falling back to pipeline.")
        shutil.rmtree(raw, ignore_errors=True)
        raw.mkdir(parents=True, exist_ok=True)
        ok = _run_mineru(pdf, raw, "pipeline", effort)
    if not ok:
        raise SystemExit("MinerU produced no markdown.")

    md = _find_md(raw)
    text = md.read_text(encoding="utf-8", errors="replace")
    images_src = md.parent / "images"

    if md_root.exists():
        for child in md_root.iterdir():
            if child.is_dir():
                shutil.rmtree(child, ignore_errors=True)
    md_root.mkdir(parents=True, exist_ok=True)

    starts = [m.start() for m in _CHAPTER_RE.finditer(text)]
    segments: list[str] = []
    if len(starts) >= 2:
        if starts[0] > 0 and text[:starts[0]].strip():
            segments.append(text[:starts[0]])
        for i, p in enumerate(starts):
            end = starts[i + 1] if i + 1 < len(starts) else len(text)
            segments.append(text[p:end])
    else:
        segments = [text]

    n = 0
    for idx, seg in enumerate(segments, 1):
        seg = seg.strip()
        if len(seg) < 40:
            continue
        title = _chapter_title(seg, "Section")
        folder_name = f"{idx:02d}_{_safe(title)}"
        folder = md_root / folder_name
        (folder / "images").mkdir(parents=True, exist_ok=True)
        (folder / f"{folder_name}.md").write_text(seg + "\n", encoding="utf-8")
        copied = 0
        for ref in set(_IMG_REF_RE.findall(seg)):
            src = images_src / Path(ref).name
            if src.exists():
                shutil.copy2(src, folder / "images" / Path(ref).name)
                copied += 1
        n += 1
        print(f"  {folder_name}  ({copied} figure(s))")

    shutil.rmtree(raw, ignore_errors=True)
    return n


# ── PyMuPDF fallback ──────────────────────────────────────────────────────────

def extract_pymupdf(pdf: Path, part: str) -> int:
    import fitz

    doc = fitz.open(str(pdf))
    toc = doc.get_toc(simple=True)
    npages = doc.page_count
    md_root = catalog.md_dir(part)
    md_root.mkdir(parents=True, exist_ok=True)

    if not toc:
        text = "\n".join(doc[p].get_text() for p in range(npages))
        folder = md_root / "01_Datasheet"
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "01_Datasheet.md").write_text(
            f"# {part} Datasheet\n\n{text}", encoding="utf-8"
        )
        doc.close()
        return 1

    entries = [(lvl, title.strip(), max(0, page - 1)) for lvl, title, page in toc]
    chapters: list[dict] = []
    cur = None
    for i, (lvl, title, start) in enumerate(entries):
        end = entries[i + 1][2] if i + 1 < len(entries) else npages
        end = max(end, start + 1)
        seg = {"lvl": lvl, "title": title, "start": start, "end": end}
        if lvl <= 1 or cur is None:
            cur = {"title": title, "segments": [seg]}
            chapters.append(cur)
        else:
            cur["segments"].append(seg)

    n = 0
    for idx, ch in enumerate(chapters, 1):
        folder_name = f"{idx:02d}_{_safe(ch['title'])}"
        folder = md_root / folder_name
        folder.mkdir(parents=True, exist_ok=True)
        parts = [f"# {ch['title']}\n"]
        for seg in ch["segments"]:
            text = "".join(
                doc[p].get_text() for p in range(seg["start"], seg["end"])
            ).strip()
            if not text:
                continue
            if seg is not ch["segments"][0]:
                parts.append(f"\n{_hlevel(seg['lvl'])} {seg['title']}\n")
            parts.append(text)
        (folder / f"{folder_name}.md").write_text("\n".join(parts), encoding="utf-8")
        n += 1
        print(f"  {folder_name}  ({len(ch['segments'])} segments)")
    doc.close()
    return n


# ── main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    cuda = _has_cuda()
    auto_backend = _choose_mineru_backend()
    device_label = "GPU (hybrid-engine)" if cuda else "CPU (pipeline, text-only)"

    ap = argparse.ArgumentParser(
        description=f"Datasheet PDF → chapter markdown  [{device_label}]"
    )
    ap.add_argument("--part", default="",
                    help="Part name (derived from --pdf stem if omitted)")
    ap.add_argument("--pdf", default="",
                    help="PDF path — creates data/<stem>/ folder automatically")
    ap.add_argument("--backend", default="mineru", choices=["mineru", "pymupdf"])
    ap.add_argument("--mineru-backend", default=auto_backend,
                    choices=["hybrid-engine", "pipeline"],
                    help=f"MinerU engine (auto-selected: {auto_backend})")
    ap.add_argument("--effort", default="high", choices=["medium", "high"])
    args = ap.parse_args()

    # Resolve PDF and part name
    if args.pdf:
        pdf = Path(args.pdf).resolve()
        if not pdf.exists():
            raise SystemExit(f"PDF not found: {pdf}")
        part_name, _ = setup_part_folder(pdf)
        part = args.part or part_name
    elif args.part:
        part = args.part
        src = catalog.source_pdf(part)
        if src.exists():
            pdf = src
        else:
            # search repo root
            matches = [p for p in catalog.REPO_ROOT.glob("*.pdf")
                       if part.lower() in p.name.lower()]
            if not matches:
                raise SystemExit(
                    f"No PDF found for '{part}'. "
                    f"Use --pdf to specify the file path."
                )
            pdf = matches[0]
            setup_part_folder(pdf)
    else:
        raise SystemExit("Provide --pdf <path> or --part <name>.")

    if args.mineru_backend != auto_backend:
        print(f"  Note: overriding auto-selected backend ({auto_backend}) "
              f"with {args.mineru_backend}")

    print(f"PDF → markdown  backend={args.backend}/{args.mineru_backend}  part={part}")
    if args.backend == "mineru":
        n = extract_mineru(pdf, part, args.mineru_backend, args.effort)
    else:
        n = extract_pymupdf(pdf, part)
    print(f"Done: {n} sections → {catalog.md_dir(part)}")
    print(f"Next: python tools/extract_structured.py --part {part}")


if __name__ == "__main__":
    main()
