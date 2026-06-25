"""Tests for ds.graph.query — dependency_summary with a mock GraphStore."""
import pytest
from unittest.mock import MagicMock
from ds.graph.model import (
    GraphEdge,
    REGISTER_IN_BLOCK, BIT_IN_REGISTER, PIN_OF_BLOCK,
    BLOCK_DEPENDS_ON, REGISTER_ENABLES,
    PROSE_REFERENCES_REGISTER, PROSE_REFERENCES_BLOCK,
)
from ds.graph.query import dependency_summary, traverse


# ── fixtures / helpers ────────────────────────────────────────────────────────

def _edge(edge_type, src_type, src_id, tgt_type, tgt_id, weight=1.0, part="ADXL345"):
    return GraphEdge(
        part=part, edge_type=edge_type,
        source_type=src_type, source_id=src_id,
        target_type=tgt_type, target_id=tgt_id,
        label=f"{src_id} → {tgt_id}", weight=weight,
    )


def _mock_store(neighbors_map: dict | None = None):
    """Build a mock GraphStore whose get_neighbors() returns from a lookup map.

    neighbors_map: {node_id: [GraphEdge, ...]}  — returned regardless of direction/edge_types.
    For tests that need direction-specific control, pass a callable for richer mocking.
    """
    store = MagicMock()
    nm = neighbors_map or {}

    def _get_neighbors(part, node_id, *, direction="out", edge_types=None, limit=200):
        all_edges = nm.get(node_id, [])
        if edge_types:
            all_edges = [e for e in all_edges if e.edge_type in edge_types]
        if direction == "out":
            all_edges = [e for e in all_edges if e.source_id == node_id]
        elif direction == "in":
            all_edges = [e for e in all_edges if e.target_id == node_id]
        return all_edges[:limit]

    store.get_neighbors.side_effect = _get_neighbors
    return store


# ── dependency_summary ────────────────────────────────────────────────────────

class TestDependencySummary:
    def test_header_line_always_present(self):
        store = _mock_store()
        result = dependency_summary(store, "ADXL345", "ADXL345/FIFO")
        assert "FIFO" in result
        assert "ADXL345" in result

    def test_no_edges_shows_not_found(self):
        store = _mock_store()
        result = dependency_summary(store, "ADXL345", "ADXL345/FIFO", depth=1)
        assert "No graph edges" in result

    def test_dependency_section_shown(self):
        dep = _edge(BLOCK_DEPENDS_ON, "BLOCK", "ADXL345/FIFO",
                    "BLOCK", "ADXL345/POWER", weight=0.6)
        store = _mock_store({"ADXL345/FIFO": [dep]})
        result = dependency_summary(store, "ADXL345", "ADXL345/FIFO", depth=1)
        assert "Prerequisites" in result
        assert "POWER" in result

    def test_outgoing_dep_shows_needs(self):
        dep = _edge(BLOCK_DEPENDS_ON, "BLOCK", "ADXL345/FIFO",
                    "BLOCK", "ADXL345/POWER", weight=0.6)
        store = _mock_store({"ADXL345/FIFO": [dep]})
        result = dependency_summary(store, "ADXL345", "ADXL345/FIFO", depth=1)
        assert "→ needs" in result

    def test_incoming_dep_shows_needed_by(self):
        dep = _edge(BLOCK_DEPENDS_ON, "BLOCK", "ADXL345/FIFO",
                    "BLOCK", "ADXL345/POWER", weight=0.6)
        # POWER is the target — query from POWER's perspective
        store = _mock_store({"ADXL345/POWER": [dep]})
        result = dependency_summary(store, "ADXL345", "ADXL345/POWER", depth=1)
        assert "← needed by" in result or "FIFO" in result

    def test_registers_in_block_shown(self):
        rib = _edge(REGISTER_IN_BLOCK, "REGISTER", "ADXL345/FIFO/FIFO_CTL",
                    "BLOCK", "ADXL345/FIFO")
        store = _mock_store({"ADXL345/FIFO": [rib]})
        result = dependency_summary(store, "ADXL345", "ADXL345/FIFO", depth=1)
        assert "Registers" in result
        assert "FIFO_CTL" in result

    def test_bits_in_register_shown(self):
        bir = _edge(BIT_IN_REGISTER, "BIT", "ADXL345/FIFO/FIFO_CTL/TRIGGER",
                    "REGISTER", "ADXL345/FIFO/FIFO_CTL")
        store = _mock_store({"ADXL345/FIFO/FIFO_CTL": [bir]})
        result = dependency_summary(store, "ADXL345", "ADXL345/FIFO/FIFO_CTL", depth=1)
        assert "Bit fields" in result
        assert "TRIGGER" in result

    def test_belongs_to_block_shown(self):
        rib = _edge(REGISTER_IN_BLOCK, "REGISTER", "ADXL345/FIFO/FIFO_CTL",
                    "BLOCK", "ADXL345/FIFO")
        store = _mock_store({"ADXL345/FIFO/FIFO_CTL": [rib]})
        result = dependency_summary(store, "ADXL345", "ADXL345/FIFO/FIFO_CTL", depth=1)
        assert "Belongs to block" in result
        assert "FIFO" in result

    def test_pins_shown(self):
        pin_e = _edge(PIN_OF_BLOCK, "PIN", "ADXL345/SERIAL/PIN:SDA",
                      "BLOCK", "ADXL345/SERIAL")
        store = _mock_store({"ADXL345/SERIAL": [pin_e]})
        result = dependency_summary(store, "ADXL345", "ADXL345/SERIAL", depth=1)
        assert "Pins" in result
        assert "SDA" in result

    def test_prose_backlinks_shown(self):
        ref = _edge(PROSE_REFERENCES_REGISTER, "PROSE",
                    "ADXL345/POWER/1.0:Configuration",
                    "REGISTER", "ADXL345/FIFO/FIFO_CTL")
        store = _mock_store({"ADXL345/FIFO/FIFO_CTL": [ref]})
        result = dependency_summary(store, "ADXL345", "ADXL345/FIFO/FIFO_CTL", depth=1)
        assert "prose" in result.lower() or "Mentioned" in result

    def test_max_lines_respected(self):
        # Generate many edges to push output over max_lines
        edges = [
            _edge(REGISTER_IN_BLOCK, "REGISTER", f"ADXL345/FIFO/REG{i}",
                  "BLOCK", "ADXL345/FIFO")
            for i in range(100)
        ]
        store = _mock_store({"ADXL345/FIFO": edges})
        result = dependency_summary(store, "ADXL345", "ADXL345/FIFO", depth=1, max_lines=10)
        assert len(result.splitlines()) <= 10

    def test_conf_shown_for_low_weight(self):
        dep = _edge(BLOCK_DEPENDS_ON, "BLOCK", "ADXL345/FIFO",
                    "BLOCK", "ADXL345/POWER", weight=0.6)
        store = _mock_store({"ADXL345/FIFO": [dep]})
        result = dependency_summary(store, "ADXL345", "ADXL345/FIFO", depth=1)
        assert "conf=0.6" in result

    def test_no_conf_for_weight_one(self):
        rib = _edge(REGISTER_IN_BLOCK, "REGISTER", "ADXL345/FIFO/FIFO_CTL",
                    "BLOCK", "ADXL345/FIFO", weight=1.0)
        store = _mock_store({"ADXL345/FIFO": [rib]})
        result = dependency_summary(store, "ADXL345", "ADXL345/FIFO", depth=1)
        assert "conf=" not in result


# ── traverse ──────────────────────────────────────────────────────────────────

class TestTraverse:
    def test_single_hop(self):
        rib = _edge(REGISTER_IN_BLOCK, "REGISTER", "ADXL345/FIFO/FIFO_CTL",
                    "BLOCK", "ADXL345/FIFO")
        store = _mock_store({"ADXL345/FIFO": [rib]})
        result = traverse(store, "ADXL345", "ADXL345/FIFO", depth=1)
        assert "ADXL345/FIFO" in result

    def test_empty_graph(self):
        store = _mock_store()
        result = traverse(store, "ADXL345", "ADXL345/FIFO", depth=2)
        assert result == {}

    def test_depth_zero_returns_empty(self):
        rib = _edge(REGISTER_IN_BLOCK, "REGISTER", "ADXL345/FIFO/FIFO_CTL",
                    "BLOCK", "ADXL345/FIFO")
        store = _mock_store({"ADXL345/FIFO": [rib]})
        result = traverse(store, "ADXL345", "ADXL345/FIFO", depth=0)
        assert result == {}
