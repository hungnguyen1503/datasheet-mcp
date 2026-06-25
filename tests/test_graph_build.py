"""Tests for ds.graph.build — edge extraction logic (no Qdrant required)."""
import pytest
from unittest.mock import MagicMock, call
from ds.model import RegisterCard, BitField, Pin, ProseBlock
from ds.graph.model import (
    REGISTER_IN_BLOCK, BIT_IN_REGISTER, PIN_OF_BLOCK,
    PROSE_REFERENCES_REGISTER, PROSE_REFERENCES_BLOCK,
    BLOCK_DEPENDS_ON, REGISTER_ENABLES,
)
from ds.graph.build import (
    build_graph, block_id, reg_id, bit_id, pin_id,
    _is_op_heading,
)


# ── helpers ───────────────────────────────────────────────────────────────────

def _bf(symbol, reserved=False):
    return BitField(
        bits="D0", symbol=symbol if not reserved else "—",
        name="Reserved" if reserved else symbol,
        description="desc", access="R/W",
    )

def _card(block, register, bitfields=None):
    return RegisterCard(
        vendor="ADI", part="ADXL345", block=block, register=register,
        name=f"{register} register", section="",
        bitfields=bitfields or [],
    )

def _pin(block, pin, signal=""):
    return Pin(vendor="ADI", part="ADXL345", block=block,
               pin=pin, signal=signal or pin, type="I/O", description="")

def _prose(block, heading, text, section="1.0"):
    return ProseBlock(
        vendor="ADI", part="ADXL345", block=block,
        section=section, heading=heading,
        breadcrumb=f"ADXL345 > {block} > {heading}",
        text=text,
    )

def _mock_store():
    store = MagicMock()
    store.add_edge = MagicMock()
    store.commit = MagicMock()
    return store


# ── node ID helpers ────────────────────────────────────────────────────────────

class TestNodeIdHelpers:
    def test_block_id(self):
        assert block_id("ADXL345", "fifo") == "ADXL345/FIFO"

    def test_reg_id(self):
        assert reg_id("ADXL345", "fifo", "fifo_ctl") == "ADXL345/FIFO/FIFO_CTL"

    def test_bit_id(self):
        assert bit_id("ADXL345", "FIFO", "FIFO_CTL", "trigger") == "ADXL345/FIFO/FIFO_CTL/TRIGGER"

    def test_pin_id(self):
        assert pin_id("ADXL345", "SERIAL", "SDA") == "ADXL345/SERIAL/PIN:SDA"


# ── _is_op_heading ────────────────────────────────────────────────────────────

class TestIsOpHeading:
    def test_operation(self):
        assert _is_op_heading("Operation Mode")

    def test_initialization(self):
        assert _is_op_heading("Initialization Sequence")

    def test_configuration(self):
        assert _is_op_heading("Configuration Steps")

    def test_procedure(self):
        assert _is_op_heading("Startup Procedure")

    def test_not_operation(self):
        assert not _is_op_heading("Register Map")
        assert not _is_op_heading("Pin Description")

    def test_case_insensitive(self):
        assert _is_op_heading("OPERATION MODE")


# ── Phase 1: Structural edges ─────────────────────────────────────────────────

class TestStructuralEdges:
    def test_register_in_block_edge(self):
        card = _card("FIFO", "FIFO_CTL")
        store = _mock_store()
        build_graph("ADXL345", [card], [], [], graph_store=store, verbose=False)

        edge_types = [c.args[0].edge_type for c in store.add_edge.call_args_list]
        assert REGISTER_IN_BLOCK in edge_types

    def test_register_in_block_ids(self):
        card = _card("FIFO", "FIFO_CTL")
        store = _mock_store()
        build_graph("ADXL345", [card], [], [], graph_store=store, verbose=False)

        rib_edges = [c.args[0] for c in store.add_edge.call_args_list
                     if c.args[0].edge_type == REGISTER_IN_BLOCK]
        assert len(rib_edges) == 1
        assert rib_edges[0].source_id == "ADXL345/FIFO/FIFO_CTL"
        assert rib_edges[0].target_id == "ADXL345/FIFO"

    def test_bit_in_register_edge(self):
        bf = _bf("TRIGGER")
        card = _card("FIFO", "FIFO_CTL", bitfields=[bf])
        store = _mock_store()
        build_graph("ADXL345", [card], [], [], graph_store=store, verbose=False)

        edge_types = [c.args[0].edge_type for c in store.add_edge.call_args_list]
        assert BIT_IN_REGISTER in edge_types

    def test_reserved_bit_no_edge(self):
        bf = _bf("RESERVED", reserved=True)
        card = _card("FIFO", "FIFO_CTL", bitfields=[bf])
        store = _mock_store()
        build_graph("ADXL345", [card], [], [], graph_store=store, verbose=False)

        edge_types = [c.args[0].edge_type for c in store.add_edge.call_args_list]
        assert BIT_IN_REGISTER not in edge_types

    def test_pin_of_block_edge(self):
        pin = _pin("SERIAL", "SDA", "SDA")
        store = _mock_store()
        build_graph("ADXL345", [], [pin], [], graph_store=store, verbose=False)

        edge_types = [c.args[0].edge_type for c in store.add_edge.call_args_list]
        assert PIN_OF_BLOCK in edge_types

    def test_pin_with_empty_block_skipped(self):
        pin = _pin("", "1", "VDD")
        store = _mock_store()
        build_graph("ADXL345", [], [pin], [], graph_store=store, verbose=False)

        edge_types = [c.args[0].edge_type for c in store.add_edge.call_args_list]
        assert PIN_OF_BLOCK not in edge_types

    def test_multiple_registers_all_get_edges(self):
        cards = [_card("POWER", "POWER_CTL"), _card("FIFO", "FIFO_CTL")]
        store = _mock_store()
        build_graph("ADXL345", cards, [], [], graph_store=store, verbose=False)

        rib_edges = [c.args[0] for c in store.add_edge.call_args_list
                     if c.args[0].edge_type == REGISTER_IN_BLOCK]
        assert len(rib_edges) == 2

    def test_commit_called(self):
        store = _mock_store()
        build_graph("ADXL345", [], [], [], graph_store=store, verbose=False)
        store.commit.assert_called_once()


# ── Phase 2: Reference edges ──────────────────────────────────────────────────

class TestReferenceEdges:
    def test_prose_references_unique_register(self):
        # FIFO_CTL appears only once across all registers → unique → ref edge
        card = _card("FIFO", "FIFO_CTL")
        prose = _prose("POWER", "Overview",
                       "Set FIFO_CTL register before enabling measurement mode.")
        store = _mock_store()
        build_graph("ADXL345", [card], [], [prose], graph_store=store, verbose=False)

        edge_types = [c.args[0].edge_type for c in store.add_edge.call_args_list]
        assert PROSE_REFERENCES_REGISTER in edge_types

    def test_prose_references_block(self):
        card = _card("FIFO", "FIFO_CTL")
        # Prose in POWER block mentions FIFO block
        prose = _prose("POWER", "Overview",
                       "The FIFO buffer must be configured before data collection begins.")
        store = _mock_store()
        build_graph("ADXL345", [card], [], [prose], graph_store=store, verbose=False)

        edge_types = [c.args[0].edge_type for c in store.add_edge.call_args_list]
        assert PROSE_REFERENCES_BLOCK in edge_types

    def test_duplicate_register_not_referenced(self):
        # Same register symbol in two blocks → not unique → no ref edge
        card1 = _card("FIFO", "CTRL")
        card2 = _card("POWER", "CTRL")
        prose = _prose("TIMING", "Overview", "Set CTRL register to begin operation.")
        store = _mock_store()
        build_graph("ADXL345", [card1, card2], [], [prose], graph_store=store, verbose=False)

        edge_types = [c.args[0].edge_type for c in store.add_edge.call_args_list]
        assert PROSE_REFERENCES_REGISTER not in edge_types

    def test_self_reference_skipped(self):
        # Prose in FIFO block mentions FIFO → should NOT create PROSE_REFERENCES_BLOCK
        card = _card("FIFO", "FIFO_CTL")
        prose = _prose("FIFO", "Overview",
                       "The FIFO stores samples for later retrieval by the host.")
        store = _mock_store()
        build_graph("ADXL345", [card], [], [prose], graph_store=store, verbose=False)

        ref_block = [c.args[0] for c in store.add_edge.call_args_list
                     if c.args[0].edge_type == PROSE_REFERENCES_BLOCK]
        # FIFO referencing itself should be excluded
        self_refs = [e for e in ref_block if e.target_id == block_id("ADXL345", "FIFO")]
        assert len(self_refs) == 0


# ── Phase 3: Dependency edges ──────────────────────────────────────────────────

class TestDependencyEdges:
    def test_block_depends_on_from_operation_prose(self):
        card = _card("POWER", "POWER_CTL")
        prose = _prose("FIFO", "FIFO Operation",
                       "POWER must be enabled before configuring the FIFO buffer modes.")
        store = _mock_store()
        build_graph("ADXL345", [card], [], [prose], graph_store=store, verbose=False)

        edge_types = [c.args[0].edge_type for c in store.add_edge.call_args_list]
        assert BLOCK_DEPENDS_ON in edge_types

    def test_non_operation_heading_no_dep(self):
        card = _card("POWER", "POWER_CTL")
        # "Overview" is not an operation heading
        prose = _prose("FIFO", "Overview",
                       "POWER must be enabled before configuring the FIFO.")
        store = _mock_store()
        build_graph("ADXL345", [card], [], [prose], graph_store=store, verbose=False)

        dep_edges = [c.args[0] for c in store.add_edge.call_args_list
                     if c.args[0].edge_type == BLOCK_DEPENDS_ON]
        assert len(dep_edges) == 0

    def test_register_enables_edge(self):
        card = _card("POWER", "POWER_CTL")
        prose = _prose("POWER", "Operation",
                       "Setting the POWER_CTL register enables the measurement engine.")
        store = _mock_store()
        build_graph("ADXL345", [card], [], [prose], graph_store=store, verbose=False)

        edge_types = [c.args[0].edge_type for c in store.add_edge.call_args_list]
        assert REGISTER_ENABLES in edge_types

    def test_dep_deduped_across_occurrences(self):
        card = _card("POWER", "POWER_CTL")
        # Same dependency mentioned twice in the same prose block
        prose = _prose("FIFO", "FIFO Initialization",
                       "POWER must be enabled before FIFO. POWER must be set before FIFO reads.")
        store = _mock_store()
        build_graph("ADXL345", [card], [], [prose], graph_store=store, verbose=False)

        dep_edges = [c.args[0] for c in store.add_edge.call_args_list
                     if c.args[0].edge_type == BLOCK_DEPENDS_ON]
        # Should appear at most once despite two matches
        unique_pairs = {(e.source_id, e.target_id) for e in dep_edges}
        assert len(unique_pairs) == len(dep_edges)

    def test_return_value_is_total_edges(self):
        card = _card("FIFO", "FIFO_CTL", bitfields=[_bf("TRIGGER")])
        store = _mock_store()
        n = build_graph("ADXL345", [card], [], [], graph_store=store, verbose=False)
        # REGISTER_IN_BLOCK (1) + BIT_IN_REGISTER (1) = 2
        assert n == 2
