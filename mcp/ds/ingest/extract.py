"""Heuristic structured extraction from MinerU markdown — no LLM required.

Scans each section's markdown for register-map tables, bit-field tables, and
pin tables. Uses column-header pattern matching to classify and parse tables
into RegisterCard / BitField / Pin objects deterministically.

Strategy
--------
1. Split markdown into table blocks (header + separator + rows).
2. Score each column header against known register / pin / bitfield patterns.
3. If confidence is sufficient → parse into structured objects.
4. Low-confidence tables are silently skipped (they remain in the prose index).

No LLM, no network, no API keys. Runs in under a second per part.
"""

from __future__ import annotations

import json
import re
import uuid
from dataclasses import asdict
from pathlib import Path
from typing import Any

from ..catalog import (
    iter_sections, block_title, registers_json, pins_json, catalog_json,
    extract_cache, part_dir,
)
from ..model import RegisterCard, BitField, Pin, PartMeta

# ── Markdown table parsing ────────────────────────────────────────────────────

_TABLE_RE = re.compile(
    r"(?P<header>\|.+\|\n)\|[-|: ]+\|\n(?P<rows>(?:\|.+\|\n?)+)",
    re.MULTILINE,
)

_HEADING_RE = re.compile(r"^#{1,6}\s+(.+?)\s*#*\s*$", re.MULTILINE)
# Pattern that often precedes a register description: "Register: NAME (0xNN)"
_REG_ADDR_RE = re.compile(
    r"(?:register[:\s]+)?([A-Z][A-Z0-9_]{1,20})\s*\(?\s*(0[xX][0-9A-Fa-f]{1,4})\s*\)?",
    re.IGNORECASE,
)


def _split_row(row: str) -> list[str]:
    """Split a markdown table row into cells, stripping leading/trailing pipes."""
    return [c.strip() for c in row.strip().strip("|").split("|")]


def _col_role(header: str) -> str:
    """Return the semantic role of a column header string."""
    h = header.lower().strip()
    # Bit position / register bit columns
    if re.search(r"\bd[0-7]\b|bit\s*(no\.?|pos\.?|#|num\.?|position)?$|^bits?$", h):
        return "bits"
    # Register / bit symbol / name
    if re.search(r"^symbol$|mnemonic|field\s*name|bit\s*name|reg.*name|^name$", h):
        return "symbol"
    # Address / offset
    if re.search(r"address|addr|offset|hex|^0x", h):
        return "address"
    # Description
    if re.search(r"descr|function|detail|comment|note|value", h):
        return "desc"
    # Access / R/W
    if re.search(r"^r/?w$|^access$|^type$|^mode$|read.*write|r\s*/\s*w", h):
        return "access"
    # Default / reset value
    if re.search(r"default|reset|por|init|power.on", h):
        return "default"
    # Pin number / identifier
    if re.search(r"^pin\s*(no\.?|#|num\.?)?$|^pad\s*(no\.?)?$|^number$|^no\.$", h):
        return "pin"
    # Pin signal name
    if re.search(r"^signal$|pin\s*name|^mnemonic$|^function$", h):
        return "signal"
    # Direction / I/O type
    if re.search(r"direction|^i\s*/?\s*o$|^input$|^output$|^i\/o\s*type$", h):
        return "iotype"
    return "unknown"


def _classify_table(headers: list[str]) -> tuple[str, dict[str, int]]:
    """Classify a table as 'register_map', 'bitfield', 'pin', or 'other'.

    Returns (classification, {role: col_index}).
    """
    roles: dict[str, int] = {}
    for i, h in enumerate(headers):
        r = _col_role(h)
        if r != "unknown" and r not in roles:
            roles[r] = i

    has_bits    = "bits"    in roles
    has_symbol  = "symbol"  in roles
    has_desc    = "desc"    in roles
    has_access  = "access"  in roles
    has_address = "address" in roles
    has_pin     = "pin"     in roles
    has_signal  = "signal"  in roles
    has_iotype  = "iotype"  in roles

    # Bit-field table: bits column + (symbol or desc)
    if has_bits and (has_symbol or has_desc or has_access):
        return "bitfield", roles

    # Register map table: address + name/symbol (no individual bit columns)
    if has_address and (has_symbol or has_desc):
        return "register_map", roles

    # Pin table: pin number + (signal or desc)
    if has_pin and (has_signal or has_desc or has_iotype):
        return "pin", roles

    # Looser pin detection: signal + direction columns
    if has_signal and (has_iotype or has_desc):
        return "pin", roles

    return "other", roles


# ── Context extraction ────────────────────────────────────────────────────────

def _context_before(text: str, match_start: int, chars: int = 300) -> str:
    """Return up to `chars` characters of text immediately before a match."""
    return text[max(0, match_start - chars): match_start]


def _nearest_heading(context: str) -> str:
    """Return the last heading found in a context string."""
    headings = _HEADING_RE.findall(context)
    return headings[-1].strip() if headings else ""


def _extract_register_name_address(context: str) -> tuple[str, str]:
    """Try to extract a register name and hex address from context text."""
    m = _REG_ADDR_RE.search(context)
    if m:
        return m.group(1).upper(), m.group(2).upper()
    # Try just a hex address pattern in a heading
    addr_m = re.search(r"(0[xX][0-9A-Fa-f]{1,4})", context)
    addr = addr_m.group(1).upper() if addr_m else ""
    # Try to get a register symbol from the heading (ALLCAPS word)
    sym_m = re.search(r"\b([A-Z][A-Z0-9_]{2,})\b", context)
    sym = sym_m.group(1) if sym_m else ""
    return sym, addr


# ── Table parsers ─────────────────────────────────────────────────────────────

def _parse_register_map(
    rows: list[list[str]],
    col_map: dict[str, int],
    block: str,
    part: str,
    vendor: str,
    context: str,
) -> list[RegisterCard]:
    """Parse a register-map table into RegisterCard objects (no bitfields)."""
    cards: list[RegisterCard] = []
    col_sym  = col_map.get("symbol", col_map.get("desc",   -1))
    col_addr = col_map.get("address", -1)
    col_desc = col_map.get("desc",  col_map.get("symbol",  -1))
    col_acc  = col_map.get("access", -1)

    def _cell(row, idx):
        return row[idx].strip() if 0 <= idx < len(row) else ""

    for row in rows:
        sym  = _cell(row, col_sym).upper()
        addr = _cell(row, col_addr).upper()
        desc = _cell(row, col_desc)
        if not sym or sym in ("—", "-", "RESERVED", "NAME", "REGISTER"):
            continue
        # Skip header-repeat rows
        if sym.lower() in ("name", "symbol", "register", "mnemonic"):
            continue
        addresses = [("", addr)] if addr else []
        cards.append(RegisterCard(
            vendor=vendor, part=part, block=block,
            register=sym, name=desc or sym, section="",
            addresses=addresses, bitfields=[], notes="",
        ))
    return cards


def _parse_bitfield_table(
    rows: list[list[str]],
    col_map: dict[str, int],
    register: str,
    block: str,
    part: str,
    vendor: str,
) -> list[BitField]:
    """Parse a bit-field table into BitField objects."""
    fields: list[BitField] = []
    col_bits   = col_map.get("bits",   -1)
    col_sym    = col_map.get("symbol", col_map.get("desc",   -1))
    col_desc   = col_map.get("desc",   col_map.get("symbol", -1))
    col_access = col_map.get("access", -1)

    def _cell(row, idx):
        return row[idx].strip() if 0 <= idx < len(row) else ""

    for row in rows:
        bits   = _cell(row, col_bits)
        symbol = _cell(row, col_sym).upper() or "—"
        desc   = _cell(row, col_desc)
        access = _cell(row, col_access) or "R/W"
        if not bits or bits.lower() in ("bit", "bits", "—", "-"):
            continue
        name = desc[:60] if desc else symbol
        fields.append(BitField(bits=bits, symbol=symbol, name=name,
                               description=desc, access=access))
    return fields


def _parse_pin_table(
    rows: list[list[str]],
    col_map: dict[str, int],
    block: str,
    part: str,
    vendor: str,
) -> list[Pin]:
    """Parse a pin/pad table into Pin objects."""
    pins: list[Pin] = []
    col_pin    = col_map.get("pin",    -1)
    col_signal = col_map.get("signal", col_map.get("symbol", -1))
    col_iotype = col_map.get("iotype", col_map.get("access", -1))
    col_desc   = col_map.get("desc",   -1)

    def _cell(row, idx):
        return row[idx].strip() if 0 <= idx < len(row) else ""

    for row in rows:
        pin    = _cell(row, col_pin)
        signal = _cell(row, col_signal)
        iotype = _cell(row, col_iotype)
        desc   = _cell(row, col_desc)
        if not pin and not signal:
            continue
        if pin.lower() in ("pin", "no.", "#", "pad", "number", "—", "-"):
            continue
        pins.append(Pin(
            vendor=vendor, part=part, block=block,
            pin=pin, signal=signal, type=iotype, description=desc,
        ))
    return pins


# ── Per-section extraction ────────────────────────────────────────────────────

def _extract_section(
    text: str,
    block: str,
    part: str,
    vendor: str,
) -> tuple[list[RegisterCard], list[Pin]]:
    """Extract RegisterCards and Pins from one section's markdown text."""
    registers: list[RegisterCard] = []
    pins: list[Pin] = []

    # Accumulate bitfields per register name for this section
    pending_bitfields: dict[str, list[BitField]] = {}
    pending_cards: dict[str, RegisterCard] = {}

    for m in _TABLE_RE.finditer(text):
        header_line = m.group("header")
        rows_text   = m.group("rows")

        headers = _split_row(header_line)
        rows    = [_split_row(r) for r in rows_text.strip().splitlines()]
        rows    = [r for r in rows if any(c for c in r)]

        kind, col_map = _classify_table(headers)

        if kind == "register_map":
            ctx = _context_before(text, m.start())
            cards = _parse_register_map(rows, col_map, block, part, vendor, ctx)
            for c in cards:
                key = c.register
                pending_cards[key] = c
            registers.extend(cards)

        elif kind == "bitfield":
            ctx  = _context_before(text, m.start())
            heading = _nearest_heading(ctx)
            reg_name, reg_addr = _extract_register_name_address(ctx)
            if not reg_name:
                reg_name = re.sub(r"[^A-Z0-9_]", "_", heading.upper())[:20]

            bfs = _parse_bitfield_table(rows, col_map, reg_name, block, part, vendor)
            if bfs:
                pending_bitfields.setdefault(reg_name, []).extend(bfs)
                # If we already have a RegisterCard for this register, enrich it
                if reg_name in pending_cards:
                    old = pending_cards[reg_name]
                    updated = RegisterCard(
                        vendor=old.vendor, part=old.part, block=old.block,
                        register=old.register, name=old.name, section=old.section,
                        addresses=old.addresses or ([("", reg_addr)] if reg_addr else []),
                        bitfields=bfs, notes=old.notes,
                    )
                    pending_cards[reg_name] = updated
                    # Replace in registers list
                    registers = [updated if r.register == reg_name else r for r in registers]
                else:
                    # Create a minimal RegisterCard for this bitfield table
                    card = RegisterCard(
                        vendor=vendor, part=part, block=block,
                        register=reg_name,
                        name=heading or reg_name,
                        section="",
                        addresses=[("", reg_addr)] if reg_addr else [],
                        bitfields=bfs, notes="",
                    )
                    pending_cards[reg_name] = card
                    registers.append(card)

        elif kind == "pin":
            ctx = _context_before(text, m.start())
            pin_block = block  # use section block as pin group
            new_pins = _parse_pin_table(rows, col_map, pin_block, part, vendor)
            pins.extend(new_pins)

    return registers, pins


# ── Catalog extraction ────────────────────────────────────────────────────────

def _extract_catalog_heuristic(first_md: str, part: str) -> dict:
    """Extract vendor, title, revision from the first section's markdown."""
    lines = first_md.splitlines()

    title = ""
    vendor = ""
    revision = ""

    for line in lines[:40]:
        line = line.strip()
        # First level-1 heading is usually the part title
        if not title and line.startswith("# "):
            title = line.lstrip("# ").strip()
        # Look for vendor mentions
        for kw in ("Analog Devices", "Texas Instruments", "STMicroelectronics",
                   "NXP", "Microchip", "Maxim", "Infineon", "Renesas",
                   "OmniVision", "Macronix", "Winbond", "ISSI"):
            if kw.lower() in line.lower() and not vendor:
                vendor = kw
        # Look for revision / document number
        rev_m = re.search(
            r"(?:rev(?:ision)?\.?\s*|document\s*(?:no\.?\s*)?)[:\s]?([A-Z0-9][A-Z0-9._\-]{0,8})",
            line, re.IGNORECASE,
        )
        if rev_m and not revision:
            revision = rev_m.group(1).strip()

    return {"vendor": vendor, "title": title or part, "revision": revision}


# ── Main public API ────────────────────────────────────────────────────────────

def extract_structured(
    part: str,
    *,
    reset: bool = False,
    verbose: bool = True,
) -> tuple[list[RegisterCard], list[Pin], PartMeta]:
    """Parse MinerU markdown for one part into structured data — no LLM needed.

    Returns (register_cards, pins, part_meta).
    Writes registers.json, pins.json, catalog.json under data/<part>/.
    """
    sections = iter_sections(part)
    if not sections:
        raise SystemExit(
            f"No MinerU markdown found for part '{part}'. "
            f"Run tools/pdf_to_md.py --part {part} first."
        )

    if verbose:
        print(f"  Parsing {len(sections)} sections for {part} (heuristic, no LLM)…")

    # Extract catalog metadata from the first section
    first_md = sections[0].path.read_text(encoding="utf-8", errors="replace")
    cat_raw = _extract_catalog_heuristic(first_md, part)
    vendor = cat_raw.get("vendor", "")

    all_regs: list[RegisterCard] = []
    all_pins: list[Pin] = []

    for sec in sections:
        blk = block_title(sec.section_name)
        md  = sec.path.read_text(encoding="utf-8", errors="replace")
        regs, pins = _extract_section(md, blk, part, vendor)
        if verbose and (regs or pins):
            print(f"    {sec.section_name}: {len(regs)} registers, {len(pins)} pins")
        all_regs.extend(regs)
        all_pins.extend(pins)

    # Deduplicate registers by (block, register) key
    seen_regs: set[str] = set()
    deduped_regs: list[RegisterCard] = []
    for c in all_regs:
        key = f"{c.block}::{c.register}"
        if key not in seen_regs:
            seen_regs.add(key)
            deduped_regs.append(c)

    # Deduplicate pins by (pin, signal) key
    seen_pins: set[str] = set()
    deduped_pins: list[Pin] = []
    for p in all_pins:
        key = f"{p.pin}::{p.signal}"
        if key not in seen_pins:
            seen_pins.add(key)
            deduped_pins.append(p)

    meta = PartMeta(
        part=part,
        vendor=vendor,
        title=cat_raw.get("title", part),
        blocks=sorted({block_title(s.section_name) for s in sections}),
        revision=cat_raw.get("revision", ""),
    )

    # Persist JSON artefacts
    registers_json(part).write_text(
        json.dumps([c.to_dict() for c in deduped_regs], indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    pins_json(part).write_text(
        json.dumps([p.__dict__ for p in deduped_pins], indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    catalog_json(part).write_text(
        json.dumps(asdict(meta), indent=2, ensure_ascii=False),
        encoding="utf-8",
    )

    if verbose:
        print(f"  Done: {len(deduped_regs)} registers, {len(deduped_pins)} pins "
              f"→ data/{part}/")
    return deduped_regs, deduped_pins, meta
