"""Graph edge model for the datasheet dependency graph.

Node ID conventions (all uppercase, part-scoped):
  Block    : "{part}/{block}"                       e.g. "ADXL345/FIFO"
  Register : "{part}/{block}/{register}"            e.g. "ADXL345/FIFO/FIFO_CTL"
  Bit      : "{part}/{block}/{register}/{symbol}"   e.g. "ADXL345/FIFO/FIFO_CTL/TRIGGER"
  Pin      : "{part}/{block}/PIN:{pin}"             e.g. "ADXL345/SERIAL/PIN:SDA"
  Prose    : "{part}/{block}/{section}:{heading}"   (source only, not a query target)
"""

from __future__ import annotations

from dataclasses import dataclass

# ── Edge type constants ───────────────────────────────────────────────────────

# Structural — extracted from RegisterCard / Pin objects (weight = 1.0)
REGISTER_IN_BLOCK = "REGISTER_IN_BLOCK"
BIT_IN_REGISTER   = "BIT_IN_REGISTER"
PIN_OF_BLOCK      = "PIN_OF_BLOCK"

# Reference — regex scan of ProseBlock.text for known symbol names (weight = 0.8)
PROSE_REFERENCES_REGISTER = "PROSE_REFERENCES_REGISTER"
PROSE_REFERENCES_BLOCK    = "PROSE_REFERENCES_BLOCK"

# Dependency — pattern-extracted from operation-section prose (weight = 0.6)
BLOCK_DEPENDS_ON = "BLOCK_DEPENDS_ON"
REGISTER_ENABLES = "REGISTER_ENABLES"

ALL_EDGE_TYPES: frozenset[str] = frozenset({
    REGISTER_IN_BLOCK, BIT_IN_REGISTER, PIN_OF_BLOCK,
    PROSE_REFERENCES_REGISTER, PROSE_REFERENCES_BLOCK,
    BLOCK_DEPENDS_ON, REGISTER_ENABLES,
})

STRUCTURAL_EDGE_TYPES = frozenset({REGISTER_IN_BLOCK, BIT_IN_REGISTER, PIN_OF_BLOCK})
REFERENCE_EDGE_TYPES  = frozenset({PROSE_REFERENCES_REGISTER, PROSE_REFERENCES_BLOCK})
DEPENDENCY_EDGE_TYPES = frozenset({BLOCK_DEPENDS_ON, REGISTER_ENABLES})


# ── Data model ────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class GraphEdge:
    part: str
    edge_type: str
    source_type: str   # "REGISTER" | "BIT" | "BLOCK" | "PIN" | "PROSE"
    source_id: str     # canonical node ID (see module docstring)
    target_type: str
    target_id: str
    label: str         # human-readable description of the relationship
    weight: float = 1.0  # confidence: 1.0=structural, 0.8=regex-ref, 0.6=dep-pattern
