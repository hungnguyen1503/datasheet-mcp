"""Tests for ds.model — data classes and their derived properties."""
import pytest
from ds.model import BitField, RegisterCard, Pin, ProseBlock, PartMeta


# ── BitField ──────────────────────────────────────────────────────────────────

class TestBitField:
    def test_reserved_dash(self):
        assert BitField(bits="D7", symbol="—", name="Reserved", description="", access="R").reserved

    def test_reserved_empty_symbol(self):
        assert BitField(bits="D6", symbol="", name="Reserved", description="", access="R").reserved

    def test_reserved_name_keyword(self):
        assert BitField(bits="D5", symbol="RSVD", name="reserved", description="", access="R").reserved

    def test_not_reserved(self):
        bf = BitField(bits="D0", symbol="MEASURE", name="Measurement mode", description="Enable", access="R/W")
        assert not bf.reserved

    def test_frozen(self):
        bf = BitField(bits="D0", symbol="X", name="X", description="X", access="R")
        with pytest.raises((AttributeError, TypeError)):
            bf.bits = "D1"  # type: ignore[misc]


# ── RegisterCard ─────────────────────────────────────────────────────────────

class TestRegisterCard:
    def _make(self, **kw):
        defaults = dict(vendor="Analog Devices", part="ADXL345", block="POWER",
                        register="POWER_CTL", name="Power-Saving Features Control",
                        section="0x2D", addresses=[("", "0x2D")])
        defaults.update(kw)
        return RegisterCard(**defaults)

    def test_key_uppercase(self):
        card = self._make(vendor="Analog Devices", part="ADXL345",
                          block="power", register="power_ctl")
        assert card.key == "ANALOG DEVICES/ADXL345/POWER/POWER_CTL"

    def test_key_already_upper(self):
        card = self._make()
        assert card.key == "ANALOG DEVICES/ADXL345/POWER/POWER_CTL"

    def test_to_dict_roundtrip(self):
        bf = BitField(bits="D3", symbol="MEASURE", name="Measurement mode",
                      description="Set 1 to enable", access="R/W")
        card = self._make(bitfields=[bf])
        d = card.to_dict()
        assert d["register"] == "POWER_CTL"
        assert len(d["bitfields"]) == 1
        assert d["bitfields"][0]["symbol"] == "MEASURE"

    def test_to_dict_empty_bitfields(self):
        card = self._make()
        assert card.to_dict()["bitfields"] == []

    def test_addresses_default_empty(self):
        card = RegisterCard(vendor="V", part="P", block="B", register="R",
                            name="N", section="")
        assert card.addresses == []


# ── Pin ───────────────────────────────────────────────────────────────────────

class TestPin:
    def test_fields(self):
        p = Pin(vendor="ADI", part="ADXL345", block="SERIAL",
                pin="1", signal="VDD I/O", type="Power", description="Supply voltage")
        assert p.pin == "1"
        assert p.signal == "VDD I/O"
        assert p.block == "SERIAL"


# ── ProseBlock ────────────────────────────────────────────────────────────────

class TestProseBlock:
    def test_embed_text_format(self):
        b = ProseBlock(vendor="ADI", part="ADXL345", block="FIFO",
                       section="5.1", heading="Bypass Mode",
                       breadcrumb="ADXL345 > FIFO > Bypass Mode",
                       text="In bypass mode the FIFO is disabled.")
        et = b.embed_text()
        assert et.startswith("[ADXL345 > FIFO > Bypass Mode]")
        assert "bypass mode" in et.lower()

    def test_embed_text_includes_breadcrumb_and_text(self):
        b = ProseBlock(vendor="", part="X", block="B", section="",
                       heading="H", breadcrumb="X > B > H", text="body text")
        assert "[X > B > H]" in b.embed_text()
        assert "body text" in b.embed_text()


# ── PartMeta ──────────────────────────────────────────────────────────────────

class TestPartMeta:
    def test_defaults(self):
        m = PartMeta(part="ADXL345")
        assert m.vendor == ""
        assert m.blocks == []
        assert m.revision == ""

    def test_with_values(self):
        m = PartMeta(part="ADXL345", vendor="Analog Devices",
                     title="3-Axis Accelerometer", blocks=["FIFO", "POWER"],
                     revision="Rev.E")
        assert m.title == "3-Axis Accelerometer"
        assert "FIFO" in m.blocks
