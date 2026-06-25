"""Render RegisterCard objects to compact, fully-scoped text.

This is the core token-saving transformation: a register that occupies many
lines of source tables becomes a ~3-10 line canonical card that is *more*
precise (it always names part + block + register) and far cheaper to embed or
return.
"""

from __future__ import annotations

import re

from .model import RegisterCard, BitField


def _collapse_addresses(addresses: list[tuple[str, str]], max_show: int = 4) -> str:
    """Render an address list compactly."""
    if not addresses:
        return ""
    if len(addresses) == 1:
        label, addr = addresses[0]
        return f"@{addr}" if not label or label == addr else f"@{label}:{addr}"
    first_l, first_a = addresses[0]
    last_l, last_a = addresses[-1]
    extra = f" (+{len(addresses) - 2} more)" if len(addresses) > 2 else ""
    return f"@{first_a}…{last_a}{extra}"


def _clean_desc(text: str) -> str:
    """Tidy a bit description into a single compact line."""
    t = re.sub(r"\s+", " ", text).strip()
    t = t.replace("\\_", "_")          # MinerU markdown escape noise
    t = t.replace("When reading", "rd:").replace("When writing", " wr:")
    return t


def render_card(card: RegisterCard, *, bits: bool = True) -> str:
    """Full compact card. Set bits=False for a one-line header summary."""
    addr = _collapse_addresses(card.addresses) or (f"@{card.section}" if card.section else "")
    header = f"{card.register} ({card.name}) {addr} [{card.part}/{card.block}]".strip()
    if not bits:
        return header

    lines = [header]
    for group in _group_bitfields(card.bitfields):
        bf = group[0]
        sym = bf.symbol if not bf.reserved else "—"
        desc = _clean_desc(bf.description)
        if len(group) > 1:
            span = f"{group[0].bits}-{group[-1].bits}"
            sym = _generalize_symbol(sym)
            name = "" if bf.reserved else f" {_generalize_name(bf.name)}"
        else:
            span = bf.bits
            name = "" if bf.reserved else f" {bf.name}"
        lines.append(f"  {span:<10} {sym:<12}{name}  [{bf.access}]  {desc}".rstrip())
    if card.notes:
        note = re.sub(r"\s+", " ", card.notes).strip()
        if note:
            lines.append(f"  note: {note}")
    return "\n".join(lines)


def _generalize_symbol(sym: str) -> str:
    """CSTRT0 -> CSTRTn (trailing index replaced with n)."""
    return re.sub(r"\d+$", "n", sym)


def _generalize_name(name: str) -> str:
    """'Channel 0 Count Start' -> 'Channel n Count Start'."""
    return re.sub(r"\b\d+\b", "n", name, count=1)


def _group_bitfields(bitfields: list[BitField]) -> list[list[BitField]]:
    """Collapse consecutive single-bit fields that share one description and a
    common symbol stem (e.g. D0..D7) into one group."""
    groups: list[list[BitField]] = []
    for bf in bitfields:
        if groups:
            prev = groups[-1][-1]
            same_desc = bf.description == prev.description and bf.access == prev.access
            stem_a = re.sub(r"\d+$", "", bf.symbol)
            stem_b = re.sub(r"\d+$", "", prev.symbol)
            indexed = bf.symbol != stem_a and prev.symbol != stem_b
            if same_desc and indexed and stem_a == stem_b and stem_a:
                groups[-1].append(bf)
                continue
        groups.append([bf])
    return groups


def render_bit(card: RegisterCard, bf: BitField) -> str:
    """Render a single bit row, fully scoped — for bit-level lookups."""
    desc = _clean_desc(bf.description)
    return (
        f"{card.part}/{card.block}/{card.register}.{bf.symbol} "
        f"[{bf.bits}] {bf.name} ({bf.access}): {desc}"
    )
