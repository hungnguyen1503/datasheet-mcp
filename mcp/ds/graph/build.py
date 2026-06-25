"""Extract graph edges from already-parsed datasheet data and push to ds_graph.

Three extraction phases, called once per part after structured extraction so
documents are only read/parsed once:

  Phase 1 — Structural  (weight=1.0, 100% recall)
    REGISTER_IN_BLOCK : every register → its block
    BIT_IN_REGISTER   : every non-reserved bit → its register
    PIN_OF_BLOCK      : every pin → its block

  Phase 2 — Reference  (weight=0.8, regex-based)
    PROSE_REFERENCES_REGISTER : prose section → unique register symbols it mentions
    PROSE_REFERENCES_BLOCK    : prose section → block names it mentions

  Phase 3 — Dependency  (weight=0.6, pattern-based, operation prose only)
    BLOCK_DEPENDS_ON : prerequisite relationships ("enable X before Y")
    REGISTER_ENABLES : register causally enables a feature
"""

from __future__ import annotations

import re

from ..model import RegisterCard, Pin, ProseBlock
from .model import (
    GraphEdge,
    REGISTER_IN_BLOCK,
    BIT_IN_REGISTER,
    PIN_OF_BLOCK,
    PROSE_REFERENCES_REGISTER,
    PROSE_REFERENCES_BLOCK,
    BLOCK_DEPENDS_ON,
    REGISTER_ENABLES,
)
from .store import GraphStore

# ── Regex helpers ─────────────────────────────────────────────────────────────

_TOKEN_RE = re.compile(r'\b([A-Z][A-Z0-9_]{2,})\b')

_PREREQ_RE = [
    re.compile(r'([A-Z][A-Z0-9_]{2,})\s+(?:must|should|has\s+to)\s+be\s+(?:enabled|set|configured|started|running|supplied)\s+(?:first|before)', re.IGNORECASE),
    re.compile(r'(?:first\s+)?(?:enable|set|configure)\s+([A-Z][A-Z0-9_]{2,})\s+(?:bit\s+)?(?:before|prior\s+to)', re.IGNORECASE),
    re.compile(r'([A-Z][A-Z0-9_]{2,})\s+(?:must|needs?\s+to)\s+be\s+(?:enabled|running|supplied|started)', re.IGNORECASE),
    re.compile(r'(?:require|requires|need|needs)\s+(?:the\s+)?([A-Z][A-Z0-9_]{2,})\s+(?:to\s+be|register|bit)', re.IGNORECASE),
]

_ENABLES_RE = [
    re.compile(r'(?:setting|writing)\s+(?:a\s+1\s+to\s+)?(?:the\s+)?([A-Z][A-Z0-9_]{2,})\s+(?:bit\s+)?(?:enables|starts|activates|triggers)', re.IGNORECASE),
    re.compile(r'(?:the\s+)?([A-Z][A-Z0-9_]{2,})\s+(?:register|bit)\s+(?:enables|controls|starts|activates)', re.IGNORECASE),
]


def _is_op_heading(heading: str) -> bool:
    h = heading.lower()
    return any(kw in h for kw in (
        "operation", "operating", "procedure", "sequence", "initializ",
        "configur", "startup", "start-up", "setting",
    ))


# ── Node ID helpers ───────────────────────────────────────────────────────────

def block_id(part: str, block: str) -> str:
    return f"{part}/{block}".upper()


def reg_id(part: str, block: str, register: str) -> str:
    return f"{part}/{block}/{register}".upper()


def bit_id(part: str, block: str, register: str, symbol: str) -> str:
    return f"{part}/{block}/{register}/{symbol}".upper()


def pin_id(part: str, block: str, pin: str) -> str:
    return f"{part}/{block}/PIN:{pin}".upper()


# ── Main entry point ──────────────────────────────────────────────────────────

def build_graph(
    part: str,
    registers: list[RegisterCard],
    pins: list[Pin],
    prose_blocks: list[ProseBlock],
    *,
    graph_store: GraphStore,
    verbose: bool = True,
) -> int:
    """Extract all graph edges for one part and upsert to Qdrant."""
    n = 0

    # ── lookup tables ─────────────────────────────────────────────────────
    reg_symbol_count: dict[str, int] = {}
    for card in registers:
        sym = card.register.upper()
        reg_symbol_count[sym] = reg_symbol_count.get(sym, 0) + 1

    unique_reg_ids: dict[str, str] = {
        card.register.upper(): reg_id(part, card.block, card.register)
        for card in registers
        if reg_symbol_count.get(card.register.upper(), 0) == 1
    }
    known_blocks: dict[str, str] = {card.block.upper(): card.block for card in registers}
    for p in pins:
        if p.block:
            known_blocks.setdefault(p.block.upper(), p.block)

    # ── Phase 1: Structural ───────────────────────────────────────────────
    for card in registers:
        b_id = block_id(part, card.block)
        r_id = reg_id(part, card.block, card.register)
        graph_store.add_edge(GraphEdge(
            part=part, edge_type=REGISTER_IN_BLOCK,
            source_type="REGISTER", source_id=r_id,
            target_type="BLOCK", target_id=b_id,
            label=f"{card.register} in {card.block}", weight=1.0,
        )); n += 1

        for bf in card.bitfields:
            if not bf.reserved:
                graph_store.add_edge(GraphEdge(
                    part=part, edge_type=BIT_IN_REGISTER,
                    source_type="BIT",
                    source_id=bit_id(part, card.block, card.register, bf.symbol),
                    target_type="REGISTER", target_id=r_id,
                    label=f"{bf.symbol} in {card.register}", weight=1.0,
                )); n += 1

    for p in pins:
        if not p.block:
            continue
        graph_store.add_edge(GraphEdge(
            part=part, edge_type=PIN_OF_BLOCK,
            source_type="PIN", source_id=pin_id(part, p.block, p.pin),
            target_type="BLOCK", target_id=block_id(part, p.block),
            label=f"{p.signal or p.pin} of {p.block}", weight=1.0,
        )); n += 1

    # ── Phase 2: Reference edges from prose ───────────────────────────────
    seen_refs: set[tuple[str, str]] = set()
    for prose in prose_blocks:
        prose_src = f"{part}/{prose.block}/{prose.section}:{prose.heading}"
        for token in _TOKEN_RE.findall(prose.text):
            tok_up = token.upper()
            if tok_up in unique_reg_ids:
                key = (prose_src, unique_reg_ids[tok_up])
                if key not in seen_refs:
                    seen_refs.add(key)
                    graph_store.add_edge(GraphEdge(
                        part=part, edge_type=PROSE_REFERENCES_REGISTER,
                        source_type="PROSE", source_id=prose_src,
                        target_type="REGISTER", target_id=unique_reg_ids[tok_up],
                        label=f"{prose.heading!r} mentions {token}", weight=0.8,
                    )); n += 1
            elif tok_up in known_blocks and tok_up != prose.block.upper():
                tgt = block_id(part, tok_up)
                key = (prose_src, tgt)
                if key not in seen_refs:
                    seen_refs.add(key)
                    graph_store.add_edge(GraphEdge(
                        part=part, edge_type=PROSE_REFERENCES_BLOCK,
                        source_type="PROSE", source_id=prose_src,
                        target_type="BLOCK", target_id=tgt,
                        label=f"{prose.heading!r} mentions {token}", weight=0.8,
                    )); n += 1

    # ── Phase 3: Dependency edges from operation prose ────────────────────
    op_prose = [p for p in prose_blocks if _is_op_heading(p.heading)]
    seen_deps: set[tuple[str, str, str]] = set()
    for prose in op_prose:
        src_up = prose.block.upper()
        src_block = block_id(part, src_up)

        for pattern in _PREREQ_RE:
            for m in re.finditer(pattern, prose.text):
                tgt_token = m.group(1).upper()
                if tgt_token in known_blocks and tgt_token != src_up:
                    dep_key = (BLOCK_DEPENDS_ON, src_up, tgt_token)
                    if dep_key not in seen_deps:
                        seen_deps.add(dep_key)
                        graph_store.add_edge(GraphEdge(
                            part=part, edge_type=BLOCK_DEPENDS_ON,
                            source_type="BLOCK", source_id=src_block,
                            target_type="BLOCK", target_id=block_id(part, tgt_token),
                            label=f"{prose.block} depends on {tgt_token}", weight=0.6,
                        )); n += 1
                elif tgt_token in unique_reg_ids:
                    dep_key = (BLOCK_DEPENDS_ON, src_up, tgt_token)
                    if dep_key not in seen_deps:
                        seen_deps.add(dep_key)
                        graph_store.add_edge(GraphEdge(
                            part=part, edge_type=BLOCK_DEPENDS_ON,
                            source_type="BLOCK", source_id=src_block,
                            target_type="REGISTER", target_id=unique_reg_ids[tgt_token],
                            label=f"{prose.block} needs register {tgt_token}", weight=0.6,
                        )); n += 1

        for pattern in _ENABLES_RE:
            for m in re.finditer(pattern, prose.text):
                tgt_token = m.group(1).upper()
                if tgt_token in unique_reg_ids:
                    dep_key = (REGISTER_ENABLES, src_up, tgt_token)
                    if dep_key not in seen_deps:
                        seen_deps.add(dep_key)
                        graph_store.add_edge(GraphEdge(
                            part=part, edge_type=REGISTER_ENABLES,
                            source_type="BLOCK", source_id=src_block,
                            target_type="REGISTER", target_id=unique_reg_ids[tgt_token],
                            label=f"{tgt_token} enables a feature in {prose.block}", weight=0.6,
                        )); n += 1

    graph_store.commit()
    if verbose:
        print(f"  {part:<16} {n:>6} graph edges")
    return n
