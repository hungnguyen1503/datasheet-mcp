"""Tests for ds.catalog — path helpers and section discovery."""
import pytest
from pathlib import Path
from ds.catalog import block_title, section_number, iter_sections, list_parts, SectionDoc
import ds.catalog as _catalog


# ── block_title ───────────────────────────────────────────────────────────────

class TestBlockTitle:
    def test_strips_numeric_prefix(self):
        assert block_title("05_FIFO") == "FIFO"

    def test_replaces_underscores(self):
        assert block_title("03_Power_Management") == "Power Management"

    def test_multi_level_prefix(self):
        assert block_title("03.1_Output_Data_Rate") == "Output Data Rate"

    def test_no_prefix_returned_as_is(self):
        assert block_title("FIFO") == "FIFO"

    def test_empty_after_strip_returns_original(self):
        # purely numeric name should fallback to original
        result = block_title("05_")
        # after stripping prefix and underscores we get "" → return original
        assert result  # must not be empty

    def test_complex_prefix(self):
        assert block_title("01_2_SPI_Interface") == "SPI Interface"


# ── section_number ────────────────────────────────────────────────────────────

class TestSectionNumber:
    def test_simple(self):
        assert section_number("05_FIFO") == "05"

    def test_multi_level(self):
        assert section_number("03.1_Output") == "03.1"

    def test_underscore_separator(self):
        # _SECNUM_RE treats _ as a separator between number segments, same as .
        # so "03_1" becomes "03.1" in the output
        assert section_number("03_1_Output") == "03.1"

    def test_no_number(self):
        assert section_number("FIFO") == ""


# ── iter_sections (filesystem, uses tmp_path) ─────────────────────────────────

class TestIterSections:
    def test_finds_md_files(self, tmp_path, monkeypatch):
        # Create fake data/<PART>/MD/ structure
        part = "TESTPART"
        md_root = tmp_path / part / "MD"
        sec = md_root / "01_Power"
        sec.mkdir(parents=True)
        (sec / "01_Power.md").write_text("# Power\ncontent", encoding="utf-8")

        monkeypatch.setattr(_catalog, "DATA_ROOT", tmp_path)
        docs = iter_sections(part)
        assert len(docs) == 1
        assert docs[0].section_name == "01_Power"
        assert docs[0].part == part

    def test_skips_underscore_prefix_dirs(self, tmp_path, monkeypatch):
        part = "TESTPART"
        md_root = tmp_path / part / "MD"
        (md_root / "_mineru_raw").mkdir(parents=True)
        (md_root / "01_Section").mkdir(parents=True)
        (md_root / "01_Section" / "01_Section.md").write_text("# S\nbody", encoding="utf-8")

        monkeypatch.setattr(_catalog, "DATA_ROOT", tmp_path)
        docs = iter_sections(part)
        assert all("_mineru_raw" not in d.section_name for d in docs)

    def test_fallback_to_any_md(self, tmp_path, monkeypatch):
        part = "TESTPART"
        md_root = tmp_path / part / "MD"
        sec = md_root / "02_FIFO"
        sec.mkdir(parents=True)
        # No exact-name match — fallback to any .md in folder
        (sec / "different_name.md").write_text("# FIFO\ncontent", encoding="utf-8")

        monkeypatch.setattr(_catalog, "DATA_ROOT", tmp_path)
        docs = iter_sections(part)
        assert len(docs) == 1

    def test_empty_when_no_md_dir(self, tmp_path, monkeypatch):
        monkeypatch.setattr(_catalog, "DATA_ROOT", tmp_path)
        docs = iter_sections("NONEXISTENT")
        assert docs == []

    def test_sorted_order(self, tmp_path, monkeypatch):
        part = "TESTPART"
        md_root = tmp_path / part / "MD"
        for name in ("03_Timing", "01_Power", "02_FIFO"):
            d = md_root / name
            d.mkdir(parents=True)
            (d / f"{name}.md").write_text(f"# {name}", encoding="utf-8")

        monkeypatch.setattr(_catalog, "DATA_ROOT", tmp_path)
        docs = iter_sections(part)
        names = [d.section_name for d in docs]
        assert names == sorted(names)


# ── list_parts ────────────────────────────────────────────────────────────────

class TestListParts:
    def test_finds_parts_with_md(self, tmp_path, monkeypatch):
        (tmp_path / "ADXL345" / "MD").mkdir(parents=True)
        (tmp_path / "OV7670" / "MD").mkdir(parents=True)
        monkeypatch.setattr(_catalog, "DATA_ROOT", tmp_path)
        parts = list_parts()
        assert "ADXL345" in parts
        assert "OV7670" in parts

    def test_finds_parts_with_registers_json(self, tmp_path, monkeypatch):
        (tmp_path / "FLASH").mkdir(parents=True)
        (tmp_path / "FLASH" / "registers.json").write_text("[]", encoding="utf-8")
        monkeypatch.setattr(_catalog, "DATA_ROOT", tmp_path)
        assert "FLASH" in list_parts()

    def test_ignores_dirs_without_data(self, tmp_path, monkeypatch):
        (tmp_path / "EMPTY").mkdir(parents=True)
        monkeypatch.setattr(_catalog, "DATA_ROOT", tmp_path)
        assert "EMPTY" not in list_parts()

    def test_empty_data_root(self, tmp_path, monkeypatch):
        monkeypatch.setattr(_catalog, "DATA_ROOT", tmp_path / "nonexistent")
        assert list_parts() == []

    def test_sorted_output(self, tmp_path, monkeypatch):
        for p in ("ZPART", "APART", "MPART"):
            (tmp_path / p / "MD").mkdir(parents=True)
        monkeypatch.setattr(_catalog, "DATA_ROOT", tmp_path)
        parts = list_parts()
        assert parts == sorted(parts)
