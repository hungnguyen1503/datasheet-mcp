"""Orchestrate indexing of one part into LanceDB + the dependency graph.

Reads two sources produced by earlier stages:
  JSON (Stage 2)  — data/<part>/registers.json, pins.json, catalog.json
  Markdown (Stage 1) — data/<part>/MD/**/*.md  (for prose + graph)

Pushes to LanceDB:
  1. ds_registers  ← RegisterCard objects (dense vector + FTS)
  2. ds_pins       ← Pin objects (filter-only, no vector)
  3. ds_prose      ← ProseBlocks from MinerU markdown (dense vector + FTS)
  4. ds_graph      ← structural + reference + dependency edges (filter-only)

Idempotent per part (each store clears the part before re-adding).
"""

from __future__ import annotations

import argparse
import json
from dataclasses import asdict

from .. import catalog as _catalog
from ..model import RegisterCard, BitField, Pin, PartMeta


def _load_registers(part: str) -> tuple[list[RegisterCard], str, str]:
    """Load registers.json; returns (cards, vendor, revision)."""
    p = _catalog.registers_json(part)
    if not p.exists():
        return [], "", ""
    raw = json.loads(p.read_text(encoding="utf-8"))
    cards = []
    vendor = ""
    revision = ""
    for r in raw:
        bfs = [BitField(**b) for b in r.get("bitfields", [])]
        addrs = [tuple(a) for a in r.get("addresses", [])]
        card = RegisterCard(
            vendor=r.get("vendor", ""),
            part=r.get("part", part),
            block=r.get("block", ""),
            register=r.get("register", ""),
            name=r.get("name", ""),
            section=r.get("section", ""),
            addresses=addrs,
            bitfields=bfs,
            notes=r.get("notes", ""),
            revision=r.get("revision", ""),
        )
        cards.append(card)
        if not vendor and card.vendor:
            vendor = card.vendor
        if not revision and card.revision:
            revision = card.revision
    return cards, vendor, revision


def _load_pins(part: str) -> list[Pin]:
    p = _catalog.pins_json(part)
    if not p.exists():
        return []
    raw = json.loads(p.read_text(encoding="utf-8"))
    return [Pin(**r) for r in raw]


def _load_catalog(part: str) -> PartMeta | None:
    p = _catalog.catalog_json(part)
    if not p.exists():
        return None
    raw = json.loads(p.read_text(encoding="utf-8"))
    return PartMeta(**raw)


def build_part(
    part: str,
    *,
    with_prose: bool = True,
    with_graph: bool = True,
) -> dict:
    cards, vendor, revision = _load_registers(part)
    pins = _load_pins(part)
    meta = _load_catalog(part)

    if not cards and not _catalog.md_dir(part).is_dir():
        raise SystemExit(
            f"No data for part '{part}'.\n"
            f"Run: python tools/pdf_to_md.py --part {part}\n"
            f"Then: python tools/extract_structured.py --part {part}"
        )

    if meta is None:
        meta = PartMeta(part=part, vendor=vendor, revision=revision,
                        blocks=[_catalog.block_title(s.section_name)
                                for s in _catalog.iter_sections(part)])

    print(f"  Part {part}: {len(cards)} registers, {len(pins)} pins, "
          f"vendor={meta.vendor or '?'}, rev={meta.revision or '?'}")

    # 1. Registers + catalog
    from ..index.registers import RegisterStore
    rs = RegisterStore()
    rs.clear_part(part)
    for card in cards:
        rs.add_register(card)
    rs.commit()

    # 2. Pins
    from ..index.pins import PinStore
    ps = PinStore()
    ps.clear_part(part)
    ps.add_pins(pins)
    ps.commit()

    # 3 + 4. Prose + graph
    prose_blocks = []
    if with_prose or with_graph:
        from .prose import extract_prose
        prose_blocks = extract_prose(part, vendor=meta.vendor, revision=meta.revision)
        print(f"  Prose: {len(prose_blocks)} blocks")

    if with_prose:
        from ..index.prose import ProseIndex
        pi = ProseIndex()
        pi.clear_part(part)
        pi.add_blocks(prose_blocks)
        pi.flush()
        pi.build_indexes()  # create FTS + vector ANN index after all data is loaded

    n_edges = 0
    if with_graph:
        from ..graph.store import GraphStore
        from ..graph.build import build_graph
        gs = GraphStore()
        gs.clear_part(part)
        n_edges = build_graph(part, cards, pins, prose_blocks,
                              graph_store=gs, verbose=True)
        print(f"  Graph: {n_edges} edges")

    stats = {
        "registers": len(cards), "pins": len(pins),
        "prose": len(prose_blocks), "edges": n_edges,
    }
    print(f"  Done: {stats}")
    return stats


def main() -> None:
    ap = argparse.ArgumentParser(description="Index a part into Qdrant + graph.")
    ap.add_argument("--part", required=True, help="Part name, e.g. ADXL345")
    ap.add_argument("--no-prose", action="store_true", help="Skip prose index")
    ap.add_argument("--no-graph", action="store_true", help="Skip graph build")
    args = ap.parse_args()
    build_part(args.part, with_prose=not args.no_prose, with_graph=not args.no_graph)


if __name__ == "__main__":
    main()
