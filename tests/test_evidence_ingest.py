"""Focused deterministic tests for lossless evidence ingestion."""

from __future__ import annotations

import pytest

from ds.evidence.ingest import (
    build_corpus,
    ingest_document,
    parse_html_tables,
    parse_markdown_document,
    parse_markdown_tables,
    token_count,
)


def test_build_corpus_reads_all_part_sections_with_file_provenance(tmp_path, monkeypatch):
    from ds.evidence import ingest

    first = tmp_path / "01_First.md"
    second = tmp_path / "02_Second.md"
    first.write_text("# Commands\n\n| Command | Opcode |\n|---|---|\n| WREN | 06h |", encoding="utf-8")
    second.write_text("# Timing\n\nDummy cycle frequency is 133 MHz.", encoding="utf-8")
    docs = [
        ingest._catalog.SectionDoc("FLASH", "01_First", first),
        ingest._catalog.SectionDoc("FLASH", "02_Second", second),
    ]
    monkeypatch.setattr(ingest._catalog, "iter_sections", lambda part: docs)

    artifact = ingest.build_corpus("FLASH", vendor="Vendor")

    assert len(artifact.evidence) > 5
    assert {source.source_file for item in artifact.evidence for source in item.sources} >= {
        str(first), str(second),
    }
    assert any(item.kind == "table" for item in artifact.evidence)
    assert any(item.title == "First" for item in artifact.evidence)
    assert any(item.title == "Second" for item in artifact.evidence)
    assert artifact.manifest.evidence_count == len(artifact.evidence)


def test_html_tables_expand_rowspan_colspan_and_keep_raw_structure() -> None:
    source = """# SPI flash\n\nTable 1: Register map\n<table>\n<caption>Table 2: Status register</caption>\n<tr><th>Register</th><th colspan='2'>Access</th></tr>\n<tr><td rowspan='2'>SR1</td><td>R</td><td>0x00</td></tr>\n<tr><td>W</td><td>0x01</td></tr>\n</table>\n"""

    tables = parse_html_tables(source, part="MX25LM51245G")

    assert len(tables) == 1
    table = tables[0].table
    assert table.raw_format == "html"
    assert "rowspan='2'" in table.raw
    assert table.label == "Table 2: Status register"
    assert table.headers == ["Register", "Access", "Access"]
    assert table.rows == [["SR1", "R", "0x00"], ["SR1", "W", "0x01"]]
    assert [(cell.row, cell.column, cell.row_span, cell.column_span) for cell in table.cells] == [
        (0, 0, 1, 1), (0, 1, 1, 2), (1, 0, 2, 1),
        (1, 1, 1, 1), (1, 2, 1, 1), (2, 1, 1, 1), (2, 2, 1, 1)
    ]


def test_markdown_table_preserves_escaped_pipe_and_data_rows() -> None:
    source = """## Commands\n\nTable 3: SPI commands\n| Command | Opcode | Description |\n| :--- | ---: | :--- |\n| Read | 0x03 | Read \\| array |\n| WREN | 06h | Write enable |\n\nThe command table is followed by prose.\n"""

    tables = parse_markdown_tables(source, part="MX25LM51245G")

    assert len(tables) == 1
    table = tables[0].table
    assert table.raw_format == "markdown"
    assert table.label == "Table 3: SPI commands"
    assert table.headers == ["Command", "Opcode", "Description"]
    assert table.rows == [["Read", "0x03", "Read | array"], ["WREN", "06h", "Write enable"]]
    assert len(table.cells) == 9
    assert all(cell.source_ref == "" for cell in table.cells)  # attached at corpus build time


def test_mx25_like_rows_produce_command_and_dummy_cycle_facts() -> None:
    source = """# MX25LM51245G\n## SPI configuration\n\n| Parameter | Value | Condition |\n| --- | --- | --- |\n| SPI mode | Mode 0 (CPOL=0, CPHA=0) | STR |\n| Frequency | 133 MHz | Quad I/O |\n| Dummy cycles | 8 cycles | Frequency > 80 MHz |\n\n## Commands\n| Operation | Opcode | Address |\n| --- | --- | --- |\n| WREN | 06h | none |\n| Page Program | 02h | 24-bit |\n| Sector Erase | 20h | 24-bit |\n"""

    artifact = ingest_document(source, "MX25LM51245G", revision="RevA", source_file="mx25.md", page_metadata={0: 1})
    rows = [item for item in artifact.evidence if item.kind == "table_row"]

    assert any(row.semantic_type == "mode" and "CPOL=0" in row.text for row in rows)
    assert any(row.semantic_type == "timing" and row.values.get("dummy_cycles") == "8 cycles" for row in rows)
    command_rows = [row for row in rows if row.semantic_type == "command"]
    assert {row.values.get("opcode") for row in command_rows} >= {"06h", "02h", "20h"}
    assert all(row.sources[0].page == 1 for row in rows)


def test_provenance_relations_and_layered_structure_are_stable() -> None:
    source = """# Flash\n\n## Program/Erase\n\nThe device requires write enable before program. See Table 1.\n\nTable 1: Commands\n| Operation | Opcode |\n| --- | --- |\n| WREN | 06h |\n\n![Figure 1: flow](flow.png)\n"""

    first = ingest_document(source, "MX25LM51245G", source_file="flash.md")
    second = ingest_document(source, "MX25LM51245G", source_file="flash.md")
    first_ids = [item.id for item in first.evidence]
    second_ids = [item.id for item in second.evidence]

    assert first_ids == second_ids
    table = next(item for item in first.evidence if item.kind == "table")
    row = next(item for item in first.evidence if item.kind == "table_row")
    assert table.table is not None
    assert table.table.cells[0].source_ref
    assert row.sources[0].row_index == 1
    assert row.sources[0].cell_refs
    assert any(relation.relation == "CONTAINS" and relation.target_id == row.id for relation in first.relations)
    assert any(relation.relation == "EVIDENCED_BY" and relation.source_id == row.id for relation in first.relations)
    assert any(relation.relation == "REFERENCES" and relation.target_id == table.id for relation in first.relations)
    assert any(item.kind == "chapter" for item in first.evidence)
    assert any(item.kind == "section" for item in first.evidence)
    assert any(item.kind == "figure" for item in first.evidence)


def test_prose_chunks_are_bounded_and_do_not_include_table_cells() -> None:
    paragraph = " ".join(f"word{i}" for i in range(700))
    source = f"# Long section\n\n{paragraph}\n\n| Register | Reset |\n| --- | --- |\n| CTRL | 00h |\n\nAfter table prose is separate."

    artifact = ingest_document(source, "TESTMCU")
    prose = [item for item in artifact.evidence if item.kind == "prose"]

    assert len(prose) >= 3
    assert all(token_count(item.text) <= 350 for item in prose)
    assert all("CTRL" not in item.text and "00h" not in item.text for item in prose)
    assert any("After table prose" in item.text for item in prose)


def test_coverage_reports_detected_domains_and_missing_domains() -> None:
    source = """# Minimal\n\n| Command | Opcode |\n| --- | --- |\n| Read | 03h |\n\n![block](block.png)\n"""

    artifact = ingest_document(source, "TESTMCU")
    coverage = artifact.coverage

    assert coverage.domains["tables"].state == "complete"
    assert coverage.domains["command"].detected == 1
    assert coverage.domains["figures"].state == "complete"
    assert coverage.domains["timing"].state == "unavailable"
    assert artifact.manifest.evidence_count == len(artifact.evidence)
    assert artifact.manifest.relation_count == len(artifact.relations)


def test_local_enrichment_hook_must_cite_existing_sources() -> None:
    source = "# Flash\n\nThe flash uses serial commands."

    def hook(evidence):
        prose = next(item for item in evidence if item.kind == "prose")
        return [
            prose.model_copy(update={"id": prose.id, "sources": prose.sources}),
            prose.model_copy(update={"id": "unanchored", "sources": []}),
        ]

    artifact = ingest_document(source, "TESTMCU", enrichment_hook=hook)

    assert not any(item.id in {"duplicate", "unanchored"} for item in artifact.evidence)
    assert artifact.coverage.local_ai_used is False
    assert any("Rejected" in warning for warning in artifact.coverage.warnings)


def test_public_parser_is_in_memory_and_build_requires_part_markdown() -> None:
    source = "# MX25\n\n| Command | Opcode |\n| --- | --- |\n| Read | 03h |\n"

    parsed = parse_markdown_document(source, "MX25", revision="RevA")
    assert parsed.manifest.part == "MX25"
    assert parsed.catalog.revision == "RevA"
    with pytest.raises(FileNotFoundError, match="No MinerU Markdown"):
        build_corpus("DOES_NOT_EXIST", vendor="Macronix", revision="RevA")
