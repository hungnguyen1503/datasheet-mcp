#!/usr/bin/env python3
"""Shared scaffolding for datasheet MCP tools — part discovery, directory creation,
vendor detection, PDF matching.

Used by: build_all.py, pipeline.py, pdf_to_md.py, ingest.py
"""

from __future__ import annotations

import re
import shutil
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DATA_ROOT = REPO_ROOT / "datasheet"


def sanitize_part_name(name: str) -> str:
    """Convert a raw name to a valid part folder name.

    >>> sanitize_part_name('ADXL345 (Rev. E)')
    'ADXL345'
    >>> sanitize_part_name('MX25LM51245G,3V,512Mb,v11')
    'MX25LM51245G'
    """
    # Remove file extension
    name = Path(name).stem
    # Strip common suffixes
    name = re.sub(r',\s*(3V|5V|1\.8V).*$', '', name, flags=re.IGNORECASE)
    name = re.sub(r',\s*v\d+.*$', '', name, flags=re.IGNORECASE)
    name = re.sub(r'\s*\(.*?\)', '', name)
    # Replace spaces/special chars with nothing, keep alphanumeric and dashes
    name = re.sub(r'[^\w-]', '', name)
    return name.strip()


def detect_vendor(part_name: str) -> str:
    """Guess the vendor from the part name prefix."""
    vendors = {
        "AD": "Analog Devices",
        "ADXL": "Analog Devices",
        "OV": "OmniVision",
        "MX": "Macronix",
        "MX25": "Macronix",
        "SV": "Sitronix",
        "W25": "Winbond",
        "AT": "Microchip",
        "PIC": "Microchip",
        "STM": "STMicroelectronics",
        "STM32": "STMicroelectronics",
        "LM": "Texas Instruments",
        "TL": "Texas Instruments",
        "TLV": "Texas Instruments",
        "SN": "Texas Instruments",
        "MAX": "Maxim/Analog Devices",
        "DS": "Maxim/Analog Devices",
        "IS": "ISSI",
        "SST": "Microchip",
        "MCP": "Microchip",
        "ENC": "Microchip",
        "LAN": "Microchip",
        "KSZ": "Microchip",
        "DP": "Microchip",
        "RTL": "Realtek",
        "IP": "IC Plus",
    }
    for prefix, vendor in sorted(vendors.items(), key=lambda x: -len(x[0])):
        if part_name.upper().startswith(prefix.upper()):
            return vendor
    return ""


def setup_part_dir(part: str, *, vendor: str = "") -> Path:
    """Create data/<PART>/ directory and return its path."""
    d = DATA_ROOT / part
    d.mkdir(parents=True, exist_ok=True)
    return d


def copy_source_pdf(pdf_path: Path, part: str) -> Path:
    """Copy a PDF into data/<PART>/source.pdf. Returns the destination path."""
    dst = setup_part_dir(part) / "source.pdf"
    if pdf_path.resolve() != dst.resolve():
        shutil.copy2(pdf_path, dst)
    return dst


def discover_pdfs(search_dir: Path | None = None) -> list[Path]:
    """Find all PDFs in the given directory or repo root (non-recursive)."""
    d = search_dir or REPO_ROOT
    pdfs = []
    for p in sorted(d.glob("*.pdf")):
        if p.is_file():
            pdfs.append(p)
    return pdfs


def detect_part_from_filename(filename: str) -> str:
    """Extract a plausible part name from a PDF filename.

    >>> detect_part_from_filename('ADXL345.pdf')
    'ADXL345'
    >>> detect_part_from_filename('MX25LM51245G,3V,512Mb,v11.pdf')
    'MX25LM51245G'
    """
    return sanitize_part_name(filename)


# ── MD discovery ──────────────────────────────────────────────────────────

def md_dir(part: str) -> Path:
    """data/<PART>/MD/ directory."""
    return DATA_ROOT / part / "MD"


def md_files(part: str) -> list[Path]:
    """All .md files under data/<PART>/MD/."""
    d = md_dir(part)
    if not d.is_dir():
        return []
    return sorted(d.rglob("*.md"))


def has_markdown(part: str) -> bool:
    """Check if a part has MinerU markdown output."""
    return bool(md_files(part))


# ── JSON discovery ────────────────────────────────────────────────────────

def registers_json(part: str) -> Path:
    return DATA_ROOT / part / "registers.json"


def pins_json(part: str) -> Path:
    return DATA_ROOT / part / "pins.json"


def catalog_json(part: str) -> Path:
    return DATA_ROOT / part / "catalog.json"


def has_structured_data(part: str) -> bool:
    """Check if a part has Stage 2 extraction output."""
    return registers_json(part).exists()
