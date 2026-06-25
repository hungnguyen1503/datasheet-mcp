"""Core data model shared across the datasheet pipeline.

Every object carries enough provenance (vendor, part, block, register, bit,
section) that a retrieved fragment is never ambiguous about *which* part and
*which* functional block it describes — the structural fix for the cross-part /
cross-block collision problem (e.g. ADXL345.FIFO.FIFO_CTL vs another part's).

Terminology (mirrors HUM, mapped to component datasheets):
  HUM `chip`       → `part`   (e.g. "ADXL345", "OV7670", "MX25LM51245G")
  HUM `peripheral` → `block`  (functional block / section, e.g. "FIFO", "POWER")
  HUM `brand`      → `vendor`  (e.g. "Analog Devices", "OmniVision", "Macronix")
"""

from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Optional


@dataclass(frozen=True)
class BitField:
    """One row of a register's bit-description table."""

    bits: str            # canonical bit span, e.g. "D7", "D6 to D3", "7:0", "b0"
    symbol: str          # e.g. "MEASURE", "RANGE[1:0]", "—" for reserved
    name: str            # human bit name, e.g. "Measurement Mode"
    description: str     # collapsed description text (read/write semantics)
    access: str          # R/W, R, W, etc.

    @property
    def reserved(self) -> bool:
        sym = self.symbol.strip()
        return sym in ("", "—", "-") or self.name.strip().lower() == "reserved"


@dataclass
class RegisterCard:
    """A fully-scoped register definition — the atomic unit of the deterministic
    index. Renders to a compact canonical form that replaces the bloated source
    HTML/markdown tables."""

    vendor: str
    part: str
    block: str                  # functional block, e.g. "FIFO" (from the section)
    register: str               # symbol, e.g. "POWER_CTL"
    name: str                   # full name, e.g. "Power-Saving Features Control"
    section: str                # doc section number / address, e.g. "0x2D"
    addresses: list[tuple[str, str]] = field(default_factory=list)  # (label, addr)
    bitfields: list[BitField] = field(default_factory=list)
    notes: str = ""             # trailing prose attached to the register
    revision: str = ""          # datasheet revision, e.g. "Rev.E"

    @property
    def key(self) -> str:
        """Stable lookup key: vendor/part/block/register, case-normalized."""
        return f"{self.vendor}/{self.part}/{self.block}/{self.register}".upper()

    def to_dict(self) -> dict:
        d = asdict(self)
        d["bitfields"] = [asdict(b) for b in self.bitfields]
        return d


@dataclass
class Pin:
    """One pin / pad description-table row."""

    vendor: str
    part: str
    block: str       # functional block this pin belongs to (may be "" / "GENERAL")
    pin: str         # pin number or name, e.g. "1", "A3", "VS"
    signal: str      # signal/pad name, e.g. "SDA", "SCL", "CS", "VDD I/O"
    type: str        # direction / kind, e.g. "I", "O", "I/O", "Power", "GND"
    description: str  # short functional description


@dataclass
class ProseBlock:
    """A heading-scoped chunk of explanatory text for the semantic index.

    `breadcrumb` is prepended to the embedded/returned text so the chunk is
    self-describing (e.g. "ADXL345 > FIFO > Bypass Mode")."""

    vendor: str
    part: str
    block: str                  # functional block / chapter, e.g. "FIFO"
    section: str                # nearest section number, may be ""
    heading: str                # the heading text this block lives under
    breadcrumb: str             # "Part > Block > Heading"
    text: str                   # the prose body (no tables)
    register: Optional[str] = None   # set when the prose is register-scoped
    revision: str = ""               # datasheet revision

    def embed_text(self) -> str:
        return f"[{self.breadcrumb}]\n{self.text}"


@dataclass
class PartMeta:
    """Catalog metadata for one indexed datasheet part."""

    part: str
    vendor: str = ""
    title: str = ""             # datasheet title, e.g. "3-Axis Digital Accelerometer"
    blocks: list[str] = field(default_factory=list)
    revision: str = ""
