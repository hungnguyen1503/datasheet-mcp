"""Tests for ds.cards — register card and bit rendering."""
import pytest
from ds.model import BitField, RegisterCard
from ds.cards import (
    render_card, render_bit,
    _collapse_addresses, _clean_desc, _group_bitfields, _generalize_symbol,
)


def _bf(bits, symbol, name="", desc="", access="R/W"):
    return BitField(bits=bits, symbol=symbol, name=name, description=desc, access=access)


def _card(register="POWER_CTL", name="Power Control", block="POWER", bitfields=None, addresses=None, notes=""):
    return RegisterCard(
        vendor="Analog Devices", part="ADXL345",
        block=block, register=register, name=name, section="0x2D",
        addresses=addresses or [("", "0x2D")],
        bitfields=bitfields or [],
        notes=notes,
    )


# ── _collapse_addresses ────────────────────────────────────────────────────────

class TestCollapseAddresses:
    def test_empty(self):
        assert _collapse_addresses([]) == ""

    def test_single_no_label(self):
        assert _collapse_addresses([("", "0x2D")]) == "@0x2D"

    def test_single_with_label(self):
        assert _collapse_addresses([("REG", "0x2D")]) == "@REG:0x2D"

    def test_same_label_addr(self):
        # When label == addr just show @addr
        assert _collapse_addresses([("0x2D", "0x2D")]) == "@0x2D"

    def test_two_addresses(self):
        result = _collapse_addresses([("", "0x00"), ("", "0xFF")])
        assert "0x00" in result and "0xFF" in result

    def test_many_addresses_shows_extra(self):
        addrs = [("", f"0x{i:02X}") for i in range(5)]
        result = _collapse_addresses(addrs)
        assert "+3 more" in result


# ── _clean_desc ────────────────────────────────────────────────────────────────

class TestCleanDesc:
    def test_collapses_whitespace(self):
        assert _clean_desc("a  b   c") == "a b c"

    def test_replaces_reading(self):
        assert "rd:" in _clean_desc("When reading this register")

    def test_replaces_writing(self):
        assert "wr:" in _clean_desc("When writing 1 to this bit")

    def test_strips_markdown_escape(self):
        assert "\\_" not in _clean_desc("some\\_name")


# ── _group_bitfields ──────────────────────────────────────────────────────────

class TestGroupBitfields:
    def test_single_bit(self):
        bfs = [_bf("D0", "MEASURE", desc="Enable", access="R/W")]
        groups = _group_bitfields(bfs)
        assert len(groups) == 1
        assert groups[0] == bfs

    def test_consecutive_same_stem_collapsed(self):
        bfs = [
            _bf("D0", "TAP0", desc="Tap channel", access="R/W"),
            _bf("D1", "TAP1", desc="Tap channel", access="R/W"),
            _bf("D2", "TAP2", desc="Tap channel", access="R/W"),
        ]
        groups = _group_bitfields(bfs)
        assert len(groups) == 1
        assert len(groups[0]) == 3

    def test_different_desc_not_collapsed(self):
        bfs = [
            _bf("D0", "A0", desc="Alpha", access="R/W"),
            _bf("D1", "A1", desc="Beta", access="R/W"),
        ]
        groups = _group_bitfields(bfs)
        assert len(groups) == 2

    def test_different_access_not_collapsed(self):
        bfs = [
            _bf("D0", "A0", desc="Same", access="R"),
            _bf("D1", "A1", desc="Same", access="R/W"),
        ]
        groups = _group_bitfields(bfs)
        assert len(groups) == 2

    def test_mixed_groups(self):
        bfs = [
            _bf("D7", "X", desc="Unique", access="R"),
            _bf("D0", "TAP0", desc="Tap", access="R/W"),
            _bf("D1", "TAP1", desc="Tap", access="R/W"),
        ]
        groups = _group_bitfields(bfs)
        assert len(groups) == 2


# ── _generalize_symbol ────────────────────────────────────────────────────────

class TestGeneralizeSymbol:
    def test_trailing_digit(self):
        assert _generalize_symbol("TAP0") == "TAPn"
        assert _generalize_symbol("CSTRT15") == "CSTRTn"

    def test_no_trailing_digit(self):
        assert _generalize_symbol("MEASURE") == "MEASURE"


# ── render_card ───────────────────────────────────────────────────────────────

class TestRenderCard:
    def test_header_only(self):
        card = _card(bitfields=[])
        text = render_card(card, bits=False)
        assert "POWER_CTL" in text
        assert "Power Control" in text
        assert "ADXL345/POWER" in text

    def test_bits_true_shows_bitfields(self):
        bfs = [_bf("D3", "MEASURE", "Measurement mode", "Set 1 to enable", "R/W")]
        card = _card(bitfields=bfs)
        text = render_card(card)
        assert "MEASURE" in text
        assert "D3" in text
        assert "R/W" in text

    def test_reserved_shows_dash(self):
        bfs = [_bf("D7", "—", "Reserved", "", "R")]
        card = _card(bitfields=bfs)
        text = render_card(card)
        assert "—" in text

    def test_notes_appended(self):
        card = _card(notes="See Table 22 for timing.")
        text = render_card(card)
        assert "note: See Table 22" in text

    def test_notes_empty_not_shown(self):
        card = _card(notes="")
        assert "note:" not in render_card(card)

    def test_no_address(self):
        card = _card(addresses=[])
        text = render_card(card, bits=False)
        assert "POWER_CTL" in text  # still renders without address

    def test_collapsed_groups_in_output(self):
        bfs = [
            _bf("D0", "TAP0", "Tap X", "Enable tap X", "R/W"),
            _bf("D1", "TAP1", "Tap Y", "Enable tap X", "R/W"),  # same desc → collapsed
        ]
        card = _card(bitfields=bfs)
        text = render_card(card)
        # collapsed group uses generalised symbol TAPn
        assert "TAPn" in text


# ── render_bit ────────────────────────────────────────────────────────────────

class TestRenderBit:
    def test_format(self):
        bf = _bf("D3", "MEASURE", "Measurement mode", "Set 1 to enable measurement", "R/W")
        card = _card()
        text = render_bit(card, bf)
        assert "ADXL345/POWER/POWER_CTL.MEASURE" in text
        assert "D3" in text
        assert "R/W" in text
        assert "measurement" in text.lower()

    def test_cleans_description(self):
        bf = _bf("D0", "X", desc="When reading returns  1", access="R")
        card = _card()
        text = render_bit(card, bf)
        assert "rd:" in text
