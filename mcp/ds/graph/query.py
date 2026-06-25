"""Graph traversal and dependency summary for the datasheet graph.

Public interface:
    dependency_summary(store, part, node_id, depth=2) -> str
    traverse(store, part, root_id, depth=2, edge_types=None) -> dict
"""

from __future__ import annotations

from collections import deque

from .model import (
    REGISTER_IN_BLOCK,
    BIT_IN_REGISTER,
    PIN_OF_BLOCK,
    BLOCK_DEPENDS_ON,
    REGISTER_ENABLES,
    PROSE_REFERENCES_REGISTER,
    PROSE_REFERENCES_BLOCK,
    GraphEdge,
)
from .store import GraphStore

_DEPENDENCY = [BLOCK_DEPENDS_ON, REGISTER_ENABLES]
_REFERENCE  = [PROSE_REFERENCES_REGISTER, PROSE_REFERENCES_BLOCK]

_MAX_REGS_SHOWN  = 24
_MAX_PROSE_SHOWN = 8
_MAX_DEPS_SHOWN  = 12


def traverse(
    store: GraphStore,
    part: str,
    root_id: str,
    *,
    depth: int = 2,
    edge_types: list[str] | None = None,
    direction: str = "both",
) -> dict[str, list[GraphEdge]]:
    """BFS from root_id up to `depth` hops; returns {node_id: [edges from that node]}."""
    visited: set[str] = {root_id}
    queue: deque[tuple[str, int]] = deque([(root_id, 0)])
    result: dict[str, list[GraphEdge]] = {}

    while queue:
        node, level = queue.popleft()
        if level >= depth:
            continue
        edges = store.get_neighbors(part, node, direction=direction, edge_types=edge_types)
        if edges:
            result[node] = edges
        for e in edges:
            neighbor = e.target_id if e.source_id == node else e.source_id
            if neighbor not in visited:
                visited.add(neighbor)
                queue.append((neighbor, level + 1))
    return result


def dependency_summary(
    store: GraphStore,
    part: str,
    node_id: str,
    *,
    depth: int = 2,
    max_lines: int = 60,
) -> str:
    """Compact multi-line dependency report for a block or register."""
    node_short = node_id.split("/")[-1]
    lines: list[str] = [f"{node_short} — dependency graph  [part={part}, depth={depth}]"]

    # ── 1. Dependency edges (most actionable) ─────────────────────────────
    dep_edges = store.get_neighbors(part, node_id, direction="both", edge_types=_DEPENDENCY)
    if dep_edges:
        lines.append("\nPrerequisites / dependencies:")
        for e in dep_edges[:_MAX_DEPS_SHOWN]:
            if e.source_id == node_id:
                other = e.target_id.split("/")[-1]
                arrow = "→ needs"
            else:
                other = e.source_id.split("/")[-1]
                arrow = "← needed by"
            conf = f"  [conf={e.weight:.1f}]" if e.weight < 1.0 else ""
            lines.append(f"  {node_short} {arrow} {other}  ({e.edge_type}){conf}")
        if len(dep_edges) > _MAX_DEPS_SHOWN:
            lines.append(f"  … and {len(dep_edges) - _MAX_DEPS_SHOWN} more dependency edges")

    # ── 2. Registers in this block ────────────────────────────────────────
    reg_in_edges = store.get_neighbors(
        part, node_id, direction="in", edge_types=[REGISTER_IN_BLOCK]
    )
    if reg_in_edges:
        reg_names = sorted(e.source_id.split("/")[-1] for e in reg_in_edges)
        shown = reg_names[:_MAX_REGS_SHOWN]
        lines.append(f"\nRegisters ({len(reg_in_edges)} total):")
        for i in range(0, len(shown), 8):
            lines.append("  " + "  ".join(shown[i:i + 8]))
        if len(reg_in_edges) > _MAX_REGS_SHOWN:
            lines.append(f"  … and {len(reg_in_edges) - _MAX_REGS_SHOWN} more")

    # ── 3. Block this register belongs to ─────────────────────────────────
    block_edges = store.get_neighbors(
        part, node_id, direction="out", edge_types=[REGISTER_IN_BLOCK]
    )
    if block_edges:
        blocks = [e.target_id.split("/")[-1] for e in block_edges]
        lines.append(f"\nBelongs to block: {', '.join(blocks)}")

    # ── 4. Bits in this register ──────────────────────────────────────────
    bit_edges = store.get_neighbors(
        part, node_id, direction="in", edge_types=[BIT_IN_REGISTER]
    )
    if bit_edges:
        bit_names = sorted(e.source_id.split("/")[-1] for e in bit_edges)
        lines.append(f"\nBit fields ({len(bit_edges)}):")
        for i in range(0, min(len(bit_names), 16), 8):
            lines.append("  " + "  ".join(bit_names[i:i + 8]))
        if len(bit_edges) > 16:
            lines.append(f"  … and {len(bit_edges) - 16} more")

    # ── 5. Pins of this block ─────────────────────────────────────────────
    pin_edges = store.get_neighbors(
        part, node_id, direction="in", edge_types=[PIN_OF_BLOCK]
    )
    if pin_edges:
        pin_names = sorted(e.source_id.split(":")[-1] for e in pin_edges)
        lines.append(f"\nPins ({len(pin_edges)}): " + ", ".join(pin_names[:16]))

    # ── 6. Prose back-links ───────────────────────────────────────────────
    ref_edges = store.get_neighbors(
        part, node_id, direction="in", edge_types=_REFERENCE
    )
    if ref_edges:
        headings = sorted({
            e.source_id.split(":")[-1] for e in ref_edges if ":" in e.source_id
        })
        lines.append(f"\nMentioned in {len(ref_edges)} prose section(s):")
        for h in headings[:_MAX_PROSE_SHOWN]:
            lines.append(f"  • {h}")
        if len(headings) > _MAX_PROSE_SHOWN:
            lines.append(f"  • … and {len(headings) - _MAX_PROSE_SHOWN} more headings")

    # ── 7. Deep dependency chain (depth > 1) ──────────────────────────────
    if depth > 1 and dep_edges:
        second_hop: dict[str, list[str]] = {}
        already = {node_id}
        for e in dep_edges[:6]:
            hop1 = e.target_id if e.source_id == node_id else e.source_id
            if hop1 in already:
                continue
            already.add(hop1)
            deeper = store.get_neighbors(part, hop1, direction="out", edge_types=_DEPENDENCY)
            if deeper:
                short = hop1.split("/")[-1]
                second_hop[short] = [
                    d.target_id.split("/")[-1] for d in deeper[:4]
                    if d.target_id not in already
                ]
        if second_hop:
            lines.append("\nTransitive dependencies (depth 2):")
            for via, targets in second_hop.items():
                if targets:
                    lines.append(f"  {via} → {', '.join(targets)}")

    if len(lines) == 1:
        lines.append(f"  No graph edges found for node '{node_id}' on part {part}.")
        lines.append(f"  Re-index with: build.bat --part {part}")

    return "\n".join(lines[:max_lines])
