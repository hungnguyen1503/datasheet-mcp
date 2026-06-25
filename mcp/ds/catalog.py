"""Data-corpus discovery + path resolution for the datasheet pipeline.

On-disk layout (produced by the tools/ ingestion scripts):

    data/<PART>/
      source.pdf                       (the datasheet PDF)
      MD/<NN_Section_Name>/<same>.md   (MinerU output, + images/)
      registers.json                   list[RegisterCard]   (LLM extraction)
      pins.json                        list[Pin]            (LLM extraction)
      catalog.json                     PartMeta             (LLM extraction)
      .extract_cache.json              resumable per-section LLM cache

`part` is the directory name (e.g. "ADXL345"). Each top-level section folder of
the MinerU output is treated as a functional *block*; its cleaned title is the
block name used by both the prose index and the structured register cards, so
register↔prose and graph cross-links line up.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

# ds package lives at <repo>/mcp/ds/, so the repo root is three parents up and
# the data corpus lives at <repo>/data.
REPO_ROOT = Path(__file__).resolve().parent.parent.parent
DATA_ROOT = REPO_ROOT / "data"


# ── per-part paths ─────────────────────────────────────────────────────────────

def part_dir(part: str) -> Path:
    return DATA_ROOT / part


def md_dir(part: str) -> Path:
    return DATA_ROOT / part / "MD"


def source_pdf(part: str) -> Path:
    return DATA_ROOT / part / "source.pdf"


def registers_json(part: str) -> Path:
    return DATA_ROOT / part / "registers.json"


def pins_json(part: str) -> Path:
    return DATA_ROOT / part / "pins.json"


def catalog_json(part: str) -> Path:
    return DATA_ROOT / part / "catalog.json"


def extract_cache(part: str) -> Path:
    return DATA_ROOT / part / ".extract_cache.json"


# ── section / block discovery ──────────────────────────────────────────────────

@dataclass(frozen=True)
class SectionDoc:
    part: str
    section_name: str   # raw folder name, e.g. "05_FIFO"
    path: Path          # the .md file


def iter_sections(part: str) -> list[SectionDoc]:
    docs: list[SectionDoc] = []
    root = md_dir(part)
    if not root.is_dir():
        return docs
    for sec_dir in sorted(root.iterdir()):
        if not sec_dir.is_dir() or sec_dir.name.startswith("_"):
            continue
        md_file = sec_dir / f"{sec_dir.name}.md"
        if not md_file.exists():
            mds = sorted(sec_dir.glob("*.md"))
            if not mds:
                continue
            md_file = mds[0]
        docs.append(SectionDoc(part=part, section_name=sec_dir.name, path=md_file))
    return docs


_SECNUM_RE = re.compile(r"^(\d+(?:[._]\d+)*)[._]")
_NUM_PREFIX_RE = re.compile(r"^\d+(?:[._]\d+)*[._]?")


def section_number(section_name: str) -> str:
    m = _SECNUM_RE.match(section_name)
    return m.group(1).replace("_", ".") if m else ""


def block_title(folder_name: str) -> str:
    """Clean a section folder name into a human block/chapter title.

    "05_FIFO" → "FIFO";  "03_Power_Management" → "Power Management".
    Shared by the prose splitter and the LLM extractor so block labels match.
    """
    name = _NUM_PREFIX_RE.sub("", folder_name)
    name = name.replace("_", " ").strip()
    return name or folder_name


def list_parts() -> list[str]:
    """Parts that have at least MinerU markdown or an extracted registers file."""
    if not DATA_ROOT.is_dir():
        return []
    parts: list[str] = []
    for d in sorted(DATA_ROOT.iterdir()):
        if not d.is_dir():
            continue
        if (d / "MD").is_dir() or (d / "registers.json").exists():
            parts.append(d.name)
    return parts
