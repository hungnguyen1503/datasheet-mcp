"""Tests for ds.ingest.prose — markdown → ProseBlock splitting."""
import pytest
from pathlib import Path
import ds.catalog as _catalog
from ds.ingest.prose import extract_prose, _clean


# ── _clean ────────────────────────────────────────────────────────────────────

class TestClean:
    def test_removes_images(self):
        assert "![" not in _clean("Some text ![alt](image.png) more text")

    def test_removes_html_tags(self):
        result = _clean("<table><tr><td>data</td></tr></table>")
        assert "<" not in result

    def test_collapses_excess_newlines(self):
        text = "line1\n\n\n\n\nline2"
        result = _clean(text)
        assert "\n\n\n" not in result

    def test_collapses_spaces(self):
        result = _clean("word1   word2\t\tword3")
        assert "  " not in result

    def test_strips_whitespace(self):
        assert _clean("  hello  ") == "hello"

    def test_preserves_content(self):
        text = "The ADXL345 uses SPI interface."
        assert "ADXL345" in _clean(text)


# ── extract_prose ─────────────────────────────────────────────────────────────

def _make_md_tree(tmp_path, part: str, sections: dict[str, str]) -> None:
    """Create data/<part>/MD/<section>/<section>.md for each entry."""
    for section_name, content in sections.items():
        d = tmp_path / part / "MD" / section_name
        d.mkdir(parents=True)
        (d / f"{section_name}.md").write_text(content, encoding="utf-8")


class TestExtractProse:
    def test_basic_extraction(self, tmp_path, monkeypatch):
        # Body must be >= 60 chars to pass the filter
        md = "# FIFO Overview\n\nThe FIFO buffer stores up to 32 samples of accelerometer data per axis.\n"
        _make_md_tree(tmp_path, "ADXL345", {"01_FIFO": md})
        monkeypatch.setattr(_catalog, "DATA_ROOT", tmp_path)

        blocks = extract_prose("ADXL345", vendor="Analog Devices")
        assert len(blocks) >= 1
        assert any("FIFO" in b.text or "FIFO" in b.heading for b in blocks)

    def test_block_title_set_correctly(self, tmp_path, monkeypatch):
        md = "# Overview\n\nSome content about power management.\n"
        _make_md_tree(tmp_path, "ADXL345", {"03_Power_Management": md})
        monkeypatch.setattr(_catalog, "DATA_ROOT", tmp_path)

        blocks = extract_prose("ADXL345")
        assert all(b.block == "Power Management" for b in blocks)

    def test_part_name_in_blocks(self, tmp_path, monkeypatch):
        md = "# Intro\n\nSome text about this sensor.\n"
        _make_md_tree(tmp_path, "OV7670", {"01_Intro": md})
        monkeypatch.setattr(_catalog, "DATA_ROOT", tmp_path)

        blocks = extract_prose("OV7670", vendor="OmniVision")
        assert all(b.part == "OV7670" for b in blocks)
        assert all(b.vendor == "OmniVision" for b in blocks)

    def test_breadcrumb_format(self, tmp_path, monkeypatch):
        md = "# SPI Interface\n\nThe sensor supports 4-wire SPI.\n"
        _make_md_tree(tmp_path, "ADXL345", {"02_Serial_Interface": md})
        monkeypatch.setattr(_catalog, "DATA_ROOT", tmp_path)

        blocks = extract_prose("ADXL345")
        for b in blocks:
            assert b.part in b.breadcrumb
            assert b.block in b.breadcrumb

    def test_heading_splits_into_multiple_blocks(self, tmp_path, monkeypatch):
        # Both bodies must be >= 60 chars; use explicit long strings
        md = (
            "# Chapter One\n\nFirst section body with enough content to pass the 60-char filter.\n\n"
            "## Subsection\n\nSubsection body with enough content to pass the 60-character filter.\n"
        )
        _make_md_tree(tmp_path, "ADXL345", {"01_Intro": md})
        monkeypatch.setattr(_catalog, "DATA_ROOT", tmp_path)

        blocks = extract_prose("ADXL345")
        assert len(blocks) >= 2

    def test_short_blocks_filtered(self, tmp_path, monkeypatch):
        # Very short text (<60 chars) should be dropped
        md = "# Header\n\nTiny.\n"
        _make_md_tree(tmp_path, "ADXL345", {"01_Short": md})
        monkeypatch.setattr(_catalog, "DATA_ROOT", tmp_path)

        blocks = extract_prose("ADXL345")
        # All returned blocks have text >= 60 chars
        assert all(len(b.text) >= 60 for b in blocks)

    def test_images_stripped_from_text(self, tmp_path, monkeypatch):
        md = "# FIFO\n\nSee figure below. ![diagram](images/fig1.png) The FIFO stores 32 samples.\n"
        _make_md_tree(tmp_path, "ADXL345", {"01_FIFO": md})
        monkeypatch.setattr(_catalog, "DATA_ROOT", tmp_path)

        blocks = extract_prose("ADXL345")
        for b in blocks:
            assert "![" not in b.text

    def test_empty_md_dir_returns_empty(self, tmp_path, monkeypatch):
        (tmp_path / "EMPTY" / "MD").mkdir(parents=True)
        monkeypatch.setattr(_catalog, "DATA_ROOT", tmp_path)
        assert extract_prose("EMPTY") == []

    def test_revision_propagated(self, tmp_path, monkeypatch):
        md = "# Section\n\nEnough content here to pass the sixty character filter for blocks.\n"
        _make_md_tree(tmp_path, "ADXL345", {"01_Sec": md})
        monkeypatch.setattr(_catalog, "DATA_ROOT", tmp_path)

        blocks = extract_prose("ADXL345", revision="Rev.E")
        assert all(b.revision == "Rev.E" for b in blocks)

    def test_multiple_sections(self, tmp_path, monkeypatch):
        sections = {
            "01_Power":       "# Power\n\nPower management content with enough text to pass the filter.\n",
            "02_FIFO":        "# FIFO\n\nFIFO buffer content with enough text to pass sixty character limit.\n",
            "03_Interrupts":  "# Interrupts\n\nInterrupt routing content with enough chars to pass the filter.\n",
        }
        _make_md_tree(tmp_path, "ADXL345", sections)
        monkeypatch.setattr(_catalog, "DATA_ROOT", tmp_path)

        blocks = extract_prose("ADXL345")
        block_names = {b.block for b in blocks}
        assert "Power" in block_names
        assert "FIFO" in block_names
        assert "Interrupts" in block_names
