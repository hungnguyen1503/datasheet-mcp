"""LLM-assisted structured extraction from MinerU markdown.

Scans each section markdown for register-map tables, bit-field tables, and pin
tables. Sends each candidate table (plus surrounding prose context) to an
OpenAI-compatible LLM and normalises the response into RegisterCard / BitField /
Pin JSON.

Results are cached per-section in data/<part>/.extract_cache.json (same
caching idiom as SchematicMCP's caption_tiles.py) so the pass is fully
resumable.

LLM backend: any OpenAI-compatible endpoint.  Configure via mcp/.env:
  EXTRACT_LLM_BACKEND=lmstudio   # or ollama, openai
  EXTRACT_LLM_HOST=http://localhost:1234/v1
  EXTRACT_LLM_MODEL=qwen3:14b    # or any instruction-following model
  EXTRACT_LLM_KEY=lm-studio      # set to real key for openai
  EXTRACT_WORKERS=4              # parallel section workers
"""

from __future__ import annotations

import json
import os
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

from ..catalog import (
    iter_sections, block_title, registers_json, pins_json, catalog_json,
    extract_cache, part_dir,
)
from ..model import RegisterCard, BitField, Pin, PartMeta

# ── LLM client ────────────────────────────────────────────────────────────────

_BACKEND_DEFAULTS = {
    "lmstudio": ("http://localhost:1234/v1", "lm-studio"),
    "ollama":   ("http://localhost:11434/v1", "ollama"),
    "openai":   ("https://api.openai.com/v1", ""),
}
_DEFAULT_MODEL = {
    "lmstudio": "qwen3:14b",
    "ollama":   "qwen3:14b",
    "openai":   "gpt-4o-mini",
}


def _llm_client():
    from openai import OpenAI
    backend = os.environ.get("EXTRACT_LLM_BACKEND", "lmstudio")
    default_url, default_key = _BACKEND_DEFAULTS.get(backend, _BACKEND_DEFAULTS["lmstudio"])
    host = os.environ.get("EXTRACT_LLM_HOST", default_url)
    key = os.environ.get("EXTRACT_LLM_KEY", default_key)
    model = os.environ.get("EXTRACT_LLM_MODEL", _DEFAULT_MODEL.get(backend, "qwen3:14b"))
    return OpenAI(base_url=host, api_key=key or "none"), model


# ── Heuristic table detector ──────────────────────────────────────────────────

_TABLE_RE = re.compile(r"(\|.+\|\n(?:\|[-: |]+\|\n)(?:\|.+\|\n)+)", re.MULTILINE)
# Detect address-like patterns that suggest a register-map table context
_ADDR_RE = re.compile(r"0x[0-9A-Fa-f]{2,4}|address|register|bit\s*\d+|d7|b7", re.IGNORECASE)
_PIN_RE = re.compile(r"\bpin\b|\bpad\b|\bpackage\b|\bsda\b|\bscl\b|\bvdd\b|\bgnd\b", re.IGNORECASE)


def _extract_tables(md_text: str) -> list[dict]:
    """Find markdown tables and classify them as register/pin/other."""
    tables = []
    for m in _TABLE_RE.finditer(md_text):
        tbl = m.group(0)
        # grab up to 200 chars of context before the table
        start = max(0, m.start() - 200)
        ctx = md_text[start:m.start()]
        is_reg = bool(_ADDR_RE.search(tbl) or _ADDR_RE.search(ctx))
        is_pin = bool(_PIN_RE.search(tbl) or _PIN_RE.search(ctx))
        tables.append({"text": tbl, "ctx": ctx.strip(), "is_reg": is_reg, "is_pin": is_pin})
    return tables


# ── LLM prompts ───────────────────────────────────────────────────────────────

_REGISTER_SYSTEM = """\
You are a datasheet parser. Extract register definitions from the markdown table(s) below.
For each register, produce one JSON object with these fields:
  register  : symbol in SCREAMING_SNAKE_CASE or the original symbol (e.g. "POWER_CTL", "BW_RATE")
  name      : full register name (string)
  section   : address or section number if visible (string, e.g. "0x2D", "")
  addresses : list of [label, address] pairs — e.g. [["", "0x2D"]] — empty list if unknown
  bitfields : list of bit field objects with keys:
                bits (e.g. "D7", "D7-D5", "7:5"), symbol (screaming snake or "—"),
                name (string), description (string), access (e.g. "R/W", "R", "W")
  notes     : any trailing notes as a single string (or "")

Return a JSON array of register objects. If no registers are found return [].
Do NOT add markdown fences. Return raw JSON only."""

_PIN_SYSTEM = """\
You are a datasheet parser. Extract pin/pad descriptions from the markdown table(s) below.
For each pin, produce one JSON object:
  pin         : pin number or identifier (string, e.g. "1", "A3", "VS")
  signal      : signal/pad name (e.g. "SDA", "SCL", "VDD I/O", "CS")
  type        : direction/kind (e.g. "I", "O", "I/O", "Power", "GND", "")
  description : short functional description (string)
  block       : if this pin belongs to a named interface/block write it (e.g. "SERIAL"), else ""

Return a JSON array of pin objects. If no pins found return [].
Return raw JSON only."""

_CATALOG_SYSTEM = """\
You are a datasheet parser. Read the text below and return ONE JSON object:
  vendor  : manufacturer name (string, e.g. "Analog Devices")
  title   : product description / datasheet title (string, e.g. "3-Axis Digital Accelerometer")
  revision: revision string if visible (string, e.g. "Rev.E", "")

Return raw JSON only."""


def _call_llm(client, model: str, system: str, user_text: str, max_tokens: int = 2048) -> str:
    resp = client.chat.completions.create(
        model=model,
        max_tokens=max_tokens,
        temperature=0,
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": user_text[:8000]},  # safety truncation
        ],
    )
    return (resp.choices[0].message.content or "").strip()


def _parse_json(text: str) -> Any:
    # strip markdown fences if model added them despite instructions
    text = re.sub(r"^```[a-z]*\n?", "", text.strip())
    text = re.sub(r"\n?```$", "", text.strip())
    return json.loads(text)


# ── Per-section extraction ────────────────────────────────────────────────────

def _extract_section(
    client, model: str, part: str, section_name: str, md_text: str, cache: dict
) -> tuple[list[dict], list[dict]]:
    """Return (register_dicts, pin_dicts) for one section, using cache where possible."""
    blk = block_title(section_name)
    tables = _extract_tables(md_text)
    reg_tables = [t for t in tables if t["is_reg"]]
    pin_tables = [t for t in tables if t["is_pin"]]

    registers: list[dict] = []
    pins: list[dict] = []

    for t in reg_tables:
        ckey = f"reg::{section_name}::{hash(t['text'])}"
        if ckey in cache:
            registers.extend(cache[ckey])
            continue
        try:
            user = f"Context:\n{t['ctx']}\n\nTable:\n{t['text']}"
            raw = _call_llm(client, model, _REGISTER_SYSTEM, user)
            parsed = _parse_json(raw)
            if isinstance(parsed, list):
                for r in parsed:
                    r.setdefault("block", blk)
                    r.setdefault("part", part)
                cache[ckey] = parsed
                registers.extend(parsed)
        except Exception as e:
            print(f"  [warn] register extract failed ({section_name}): {e}")

    for t in pin_tables:
        ckey = f"pin::{section_name}::{hash(t['text'])}"
        if ckey in cache:
            pins.extend(cache[ckey])
            continue
        try:
            user = f"Context:\n{t['ctx']}\n\nTable:\n{t['text']}"
            raw = _call_llm(client, model, _PIN_SYSTEM, user)
            parsed = _parse_json(raw)
            if isinstance(parsed, list):
                for p in parsed:
                    p.setdefault("block", blk)
                    p.setdefault("part", part)
                cache[ckey] = parsed
                pins.extend(parsed)
        except Exception as e:
            print(f"  [warn] pin extract failed ({section_name}): {e}")

    return registers, pins


def _extract_catalog(client, model: str, part: str, md_text: str) -> dict:
    try:
        raw = _call_llm(client, model, _CATALOG_SYSTEM, md_text[:3000], max_tokens=256)
        return _parse_json(raw)
    except Exception as e:
        print(f"  [warn] catalog extract failed ({part}): {e}")
        return {}


# ── Model → dataclass helpers ─────────────────────────────────────────────────

def _to_register_cards(raws: list[dict], part: str, vendor: str) -> list[RegisterCard]:
    cards = []
    seen: set[str] = set()
    for r in raws:
        reg = (r.get("register") or "").strip().upper()
        blk = (r.get("block") or "").strip()
        if not reg or not blk:
            continue
        key = f"{blk}::{reg}"
        if key in seen:
            continue
        seen.add(key)

        bfs = []
        for b in r.get("bitfields") or []:
            bfs.append(BitField(
                bits=str(b.get("bits", "")),
                symbol=str(b.get("symbol", "—")),
                name=str(b.get("name", "")),
                description=str(b.get("description", "")),
                access=str(b.get("access", "")),
            ))
        addrs = []
        for pair in r.get("addresses") or []:
            if isinstance(pair, list) and len(pair) == 2:
                addrs.append((str(pair[0]), str(pair[1])))
            elif isinstance(pair, str):
                addrs.append(("", pair))
        cards.append(RegisterCard(
            vendor=vendor,
            part=part,
            block=blk,
            register=reg,
            name=str(r.get("name", "")),
            section=str(r.get("section", "")),
            addresses=addrs,
            bitfields=bfs,
            notes=str(r.get("notes", "")),
        ))
    return cards


def _to_pins(raws: list[dict], part: str, vendor: str) -> list[Pin]:
    pins = []
    seen: set[str] = set()
    for p in raws:
        pin = str(p.get("pin", "")).strip()
        signal = str(p.get("signal", "")).strip()
        if not pin and not signal:
            continue
        key = f"{pin}::{signal}"
        if key in seen:
            continue
        seen.add(key)
        pins.append(Pin(
            vendor=vendor,
            part=part,
            block=str(p.get("block", "")),
            pin=pin,
            signal=signal,
            type=str(p.get("type", "")),
            description=str(p.get("description", "")),
        ))
    return pins


# ── Main public API ────────────────────────────────────────────────────────────

def extract_structured(
    part: str,
    *,
    workers: int | None = None,
    reset: bool = False,
) -> tuple[list[RegisterCard], list[Pin], PartMeta]:
    """Extract structured data from MinerU markdown for one part.

    Returns (register_cards, pins, part_meta).
    Writes registers.json, pins.json, catalog.json under data/<part>/.
    """
    workers = workers or int(os.environ.get("EXTRACT_WORKERS", "4"))
    client, model = _llm_client()

    cache_path = extract_cache(part)
    cache: dict = {}
    if not reset and cache_path.exists():
        try:
            cache = json.loads(cache_path.read_text(encoding="utf-8"))
        except Exception:
            pass

    sections = iter_sections(part)
    if not sections:
        raise SystemExit(f"No MinerU markdown found for part '{part}'. "
                         f"Run pdf_to_md.py first.")

    all_regs: list[dict] = []
    all_pins: list[dict] = []

    print(f"  Extracting {len(sections)} sections for {part} "
          f"(model={model}, workers={workers})…")

    def _process(sec):
        md = sec.path.read_text(encoding="utf-8", errors="replace")
        return _extract_section(client, model, part, sec.section_name, md, cache)

    try:
        with ThreadPoolExecutor(max_workers=workers) as ex:
            futs = {ex.submit(_process, sec): sec for sec in sections}
            for fut in as_completed(futs):
                sec = futs[fut]
                try:
                    regs, pins = fut.result()
                    all_regs.extend(regs)
                    all_pins.extend(pins)
                    print(f"  {sec.section_name}: {len(regs)} registers, {len(pins)} pins")
                except Exception as e:
                    print(f"  {sec.section_name}: FAILED — {e}")
    finally:
        cache_path.write_text(json.dumps(cache, indent=2, ensure_ascii=False), encoding="utf-8")

    # Extract catalog metadata from the first section
    first_md = sections[0].path.read_text(encoding="utf-8", errors="replace")
    cat_raw = _extract_catalog(client, model, part, first_md)
    vendor = str(cat_raw.get("vendor", ""))
    meta = PartMeta(
        part=part,
        vendor=vendor,
        title=str(cat_raw.get("title", "")),
        blocks=sorted({block_title(s.section_name) for s in sections}),
        revision=str(cat_raw.get("revision", "")),
    )

    reg_cards = _to_register_cards(all_regs, part, vendor)
    pins = _to_pins(all_pins, part, vendor)

    # Persist JSON artefacts
    _pdir = part_dir(part)
    registers_json(part).write_text(
        json.dumps([c.to_dict() for c in reg_cards], indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    pins_json(part).write_text(
        json.dumps([p.__dict__ for p in pins], indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    from dataclasses import asdict
    catalog_json(part).write_text(
        json.dumps(asdict(meta), indent=2, ensure_ascii=False),
        encoding="utf-8",
    )

    print(f"  Done: {len(reg_cards)} register cards, {len(pins)} pins → data/{part}/")
    return reg_cards, pins, meta
