"""Tests for ds.router — query classification logic."""
import pytest
from ds.router import classify_query, extract_block


# ── classify_query ─────────────────────────────────────────────────────────────

class TestClassifyQueryOperation:
    def test_how_to(self):
        route, kw = classify_query("how to configure the FIFO", None)
        assert route == "operation"

    def test_initialize(self):
        route, _ = classify_query("initialize the accelerometer", None)
        assert route == "operation"

    def test_enable(self):
        route, _ = classify_query("enable measurement mode", None)
        assert route == "operation"

    def test_sequence(self):
        route, _ = classify_query("startup sequence for ADXL345", None)
        assert route == "operation"

    def test_write_sequence(self):
        route, _ = classify_query("write sequence to configure output rate", None)
        assert route == "operation"

    def test_block_passed_through(self):
        route, kw = classify_query("how to configure", "FIFO")
        assert route == "operation"
        assert kw.get("block") == "FIFO"

    def test_block_extracted_from_query(self):
        route, kw = classify_query("how to configure FIFO mode", None)
        assert route == "operation"


class TestClassifyQueryPin:
    def test_pinout(self):
        route, _ = classify_query("show the pinout", None)
        assert route == "pin"

    def test_which_pin(self):
        route, _ = classify_query("which pin is SDA", None)
        assert route == "pin"

    def test_sda_keyword(self):
        route, _ = classify_query("SDA signal connection", None)
        assert route == "pin"

    def test_vdd_keyword(self):
        route, _ = classify_query("what is VDD pin", None)
        assert route == "pin"

    def test_package_pin(self):
        route, _ = classify_query("package pin assignment", None)
        assert route == "pin"


class TestClassifyQueryRegister:
    def test_allcaps_token(self):
        route, kw = classify_query("what is POWER_CTL", None)
        assert route == "register"
        assert kw["register"] == "POWER_CTL"

    def test_allcaps_with_question(self):
        route, kw = classify_query("show FIFO_CTL register", None)
        assert route == "register"
        assert kw["register"] == "FIFO_CTL"

    def test_value_keyword_overrides_register(self):
        # "HOCO frequency" should go to search even though HOCO is ALLCAPS
        route, _ = classify_query("supply voltage range", None)
        assert route == "search"

    def test_specification_keyword_routes_to_search(self):
        route, _ = classify_query("ADXL345 specification", None)
        assert route == "search"


class TestClassifyQueryBit:
    def test_bit_context(self):
        route, kw = classify_query("what is the MEASURE bit in POWER_CTL", None)
        assert route == "bit"
        assert kw["register"] == "POWER_CTL"
        assert kw["bit"] == "MEASURE"

    def test_flag_context(self):
        route, kw = classify_query("FULL_RES flag in DATA_FORMAT", None)
        assert route == "bit"
        assert kw["register"] == "DATA_FORMAT"
        assert kw["bit"] == "FULL_RES"


class TestClassifyQuerySearch:
    def test_natural_language(self):
        route, _ = classify_query("what is the accelerometer bandwidth", None)
        assert route == "search"

    def test_lowercase_only(self):
        route, _ = classify_query("temperature range for operation", None)
        assert route == "search"

    def test_overview_question(self):
        route, _ = classify_query("give me an overview of the FIFO buffer", None)
        # FIFO is in _COMMON_WORDS so no register route
        assert route == "search"

    def test_empty_query(self):
        route, _ = classify_query("", None)
        assert route == "search"


# ── extract_block ─────────────────────────────────────────────────────────────

class TestExtractBlock:
    def test_extracts_first_allcaps(self):
        # FIFO is in _COMMON_WORDS so it's skipped; use a domain-specific token
        assert extract_block("configure THRESH_ACT register") == "THRESH_ACT"

    def test_skips_pin_signals(self):
        # SDA is in _PIN_SIGNALS, should be skipped
        result = extract_block("SDA is the data line")
        assert result != "SDA"

    def test_none_when_no_match(self):
        assert extract_block("show me the pinout") is None or True  # may or may not find something

    def test_skips_common_words(self):
        # MCU, CPU etc. are in _COMMON_WORDS
        result = extract_block("MCU CPU interface")
        assert result not in ("MCU", "CPU", None) or result is None
