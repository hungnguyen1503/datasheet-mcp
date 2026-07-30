"""Lossless, deterministic ingestion for datasheet evidence.

The ingester deliberately has no network or model dependency. It keeps the
source table in ``TableData.raw``, expands merged cells for retrieval, and
anchors every derived item to stable row/cell source identifiers.  A local
enrichment hook is available for callers that have a validated local model;
the hook may only add evidence that cites already indexed source references.
"""

from __future__ import annotations

import hashlib
import html
import re
from dataclasses import dataclass, field
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence

from .. import catalog as _catalog
from .model import (
    CatalogPart,
    CorpusArtifact,
    CoverageDomain,
    CoverageReport,
    EvidenceItem,
    GraphRelation,
    IndexManifest,
    SourceRef,
    TableCell,
    TableData,
    stable_id,
)


_TOKEN_RE = re.compile(r"\w+|[^\w\s]", re.UNICODE)
_HTML_TABLE_RE = re.compile(r"<table\b[^>]*>.*?</table\s*>", re.I | re.S)
_HTML_HEADING_RE = re.compile(
    r"<h(?P<level>[1-6])\b[^>]*>.*?</h(?P=level)\s*>", re.I | re.S
)
_PAGE_MARKER_RE = re.compile(
    r"(?:<!--\s*)?(?:page|page-number|page_no|pageno)\s*[:=#]?\s*(\d+)"
    r"(?:\s*-->)?",
    re.I,
)
_IMAGE_RE = re.compile(
    r"!\[(?P<alt>[^\]]*)\]\((?P<src>[^\s)]+)(?:\s+[\"'](?P<title>.*?)[\"'])?\)"
)
_HTML_IMAGE_RE = re.compile(r"<img\b(?P<attrs>[^>]*)/?>", re.I | re.S)
_LABEL_RE = re.compile(
    r"^\s*((?:table|tab\.?|figure|fig\.?|register\s+map|memory\s+map|"
    r"command\s+table|timing\s+table)[^\n:.-]*)(?:\s*[:.\-]\s*(.*))?\s*$",
    re.I,
)
_REF_RE = re.compile(
    r"\b(table|tab\.?|figure|fig\.?|section|sec\.?)\s*([0-9]+(?:\.[0-9]+)*)",
    re.I,
)


def token_count(text: str) -> int:
    """Return a dependency-free token-ish count used for chunk budgeting."""

    return len(_TOKEN_RE.findall(text))


def normalize_text(value: str) -> str:
    """Normalize a cell or prose value without discarding source structure."""

    value = html.unescape(value).replace("\xa0", " ")
    return re.sub(r"\s+", " ", value).strip()


def _clean_markup(value: str) -> str:
    value = re.sub(r"!\[[^\]]*\]\([^)]*\)", " ", value)
    value = re.sub(r"\[([^]]+)\]\([^)]*\)", r"\1", value)
    value = re.sub(r"<[^>]+>", " ", value)
    value = re.sub(r"(^|\s)#{1,6}\s+", r"\1", value)
    value = re.sub(r"[*_`~]", "", value)
    return normalize_text(value)


@dataclass
class _RawCell:
    text: str
    row_span: int = 1
    column_span: int = 1
    is_header: bool = False


@dataclass
class _TableOccurrence:
    start: int
    end: int
    table: TableData
    label: str = ""
    ordinal: int = 0


@dataclass
class _Heading:
    start: int
    end: int
    level: int
    title: str
    item_id: str = ""
    parent_id: str = ""
    path: list[str] = field(default_factory=list)


@dataclass
class _FigureOccurrence:
    start: int
    end: int
    label: str
    title: str
    text: str
    source_path: str


@dataclass
class _Event:
    position: float
    kind: str
    item: EvidenceItem


class _HTMLTableParser(HTMLParser):
    """Small tolerant parser for MinerU's HTML table fragments."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=False)
        self.tables: list[dict[str, Any]] = []
        self._table: dict[str, Any] | None = None
        self._table_depth = 0
        self._row: list[_RawCell] | None = None
        self._cell: _RawCell | None = None
        self._caption = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        attr_map = {key.lower(): value or "" for key, value in attrs}
        if tag == "table":
            if self._table is None:
                self._table = {"rows": [], "caption": "", "notes": []}
                self._table_depth = 1
            else:
                self._table_depth += 1
            return
        if self._table is None or self._table_depth != 1:
            return
        if tag == "tr":
            self._finish_cell()
            self._finish_row()
            self._row = []
        elif tag in ("td", "th"):
            if self._row is None:
                self._row = []
            self._finish_cell()
            self._cell = _RawCell(
                text="",
                row_span=_positive_int(attr_map.get("rowspan", "1")),
                column_span=_positive_int(attr_map.get("colspan", "1")),
                is_header=tag == "th",
            )
        elif tag == "caption":
            self._caption = True
        elif tag in ("br", "p", "div", "li"):
            self._append_text("\n" if tag == "br" else " ")

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)
        self.handle_endtag(tag)

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag in ("td", "th"):
            self._finish_cell()
        elif tag == "tr" and self._table_depth == 1:
            self._finish_cell()
            self._finish_row()
        elif tag == "caption":
            self._caption = False
        elif tag == "table" and self._table is not None:
            if self._table_depth > 1:
                self._table_depth -= 1
            else:
                self._finish_cell()
                self._finish_row()
                self.tables.append(self._table)
                self._table = None
                self._table_depth = 0
                self._caption = False

    def handle_data(self, data: str) -> None:
        self._append_text(data)

    def handle_entityref(self, name: str) -> None:
        self._append_text(html.unescape(f"&{name};"))

    def handle_charref(self, name: str) -> None:
        self._append_text(html.unescape(f"&#{name};"))

    def handle_comment(self, data: str) -> None:
        # MinerU sometimes places a useful page marker in a comment.  The
        # marker is handled by the outer document parser; do not pollute cells.
        return

    def _append_text(self, data: str) -> None:
        if self._cell is not None:
            self._cell.text += data
        elif self._table is not None and self._caption:
            self._table["caption"] += data

    def _finish_cell(self) -> None:
        if self._cell is not None:
            if self._row is None:
                self._row = []
            self._row.append(self._cell)
            self._cell = None

    def _finish_row(self) -> None:
        if self._table is not None and self._row:
            self._table["rows"].append(self._row)
        self._row = None


def _positive_int(value: str) -> int:
    try:
        return max(1, int(str(value).strip()))
    except (TypeError, ValueError):
        return 1


def _model_copy(model: Any, **updates: Any) -> Any:
    """Copy a contract across supported Pydantic releases without warnings."""

    copier = getattr(model, "model_copy", None)
    if copier is not None:
        return copier(update=updates)
    return model.copy(update=updates)


def _split_markdown_row(line: str) -> list[str]:
    """Split a Markdown row while respecting escaped pipes and code spans."""

    body = line.rstrip("\r\n")
    if body.lstrip().startswith("|"):
        body = body.lstrip()[1:]
    if body.rstrip().endswith("|") and not body.rstrip().endswith("\\|"):
        body = body.rstrip()[:-1]
    values: list[str] = []
    current: list[str] = []
    escaped = False
    code = False
    for char in body:
        if escaped:
            current.append(char)
            escaped = False
        elif char == "\\":
            escaped = True
        elif char == "`":
            code = not code
            current.append(char)
        elif char == "|" and not code:
            values.append(normalize_text("".join(current)))
            current = []
        else:
            current.append(char)
    if escaped:
        current.append("\\")
    values.append(normalize_text("".join(current)))
    return values


def _is_markdown_delimiter(line: str) -> bool:
    cells = _split_markdown_row(line)
    return bool(cells) and all(bool(re.fullmatch(r":?-{3,}:?", cell.replace(" ", ""))) for cell in cells)


def _expanded_grid(raw_rows: Sequence[Sequence[_RawCell]]) -> tuple[list[list[str]], list[TableCell], list[int]]:
    """Expand merged cells while retaining one ``TableCell`` per source cell."""

    grid: list[list[str | None]] = []
    cells: list[TableCell] = []
    header_rows: list[int] = []

    def ensure_row(row: int, width: int) -> None:
        while len(grid) <= row:
            grid.append([])
        if len(grid[row]) < width:
            grid[row].extend([None] * (width - len(grid[row])))

    for row_index, raw_row in enumerate(raw_rows):
        ensure_row(row_index, 1)
        column = 0
        if any(cell.is_header for cell in raw_row):
            header_rows.append(row_index)
        for raw_cell in raw_row:
            while column < len(grid[row_index]) and grid[row_index][column] is not None:
                column += 1
            row_span = max(1, raw_cell.row_span)
            column_span = max(1, raw_cell.column_span)
            ensure_row(row_index + row_span - 1, column + column_span)
            for target_row in range(row_index, row_index + row_span):
                ensure_row(target_row, column + column_span)
                for target_column in range(column, column + column_span):
                    # Keep the first source cell on malformed overlapping HTML.
                    if grid[target_row][target_column] is None:
                        grid[target_row][target_column] = normalize_text(raw_cell.text)
            cells.append(
                TableCell(
                    row=row_index,
                    column=column,
                    text=normalize_text(raw_cell.text),
                    row_span=row_span,
                    column_span=column_span,
                    is_header=raw_cell.is_header,
                )
            )
            column += column_span

    width = max((len(row) for row in grid), default=0)
    expanded = [[str(value or "") for value in row] + [""] * (width - len(row)) for row in grid]
    if not header_rows and expanded:
        header_rows = [0]
    return expanded, cells, header_rows


def _table_data_from_raw(
    raw_rows: Sequence[Sequence[_RawCell]],
    *,
    raw_format: str,
    raw: str,
    label: str = "",
    notes: Sequence[str] = (),
) -> TableData:
    expanded, cells, header_rows = _expanded_grid(raw_rows)
    header_row = header_rows[0] if header_rows else None
    headers = list(expanded[header_row]) if header_row is not None and expanded else []
    data_rows = [row for index, row in enumerate(expanded) if index not in set(header_rows)]
    if not headers and data_rows:
        headers = [f"column_{index + 1}" for index in range(len(data_rows[0]))]
    if len(headers) < max((len(row) for row in data_rows), default=0):
        headers.extend(f"column_{index + 1}" for index in range(len(headers), max(len(row) for row in data_rows)))
    return TableData(
        label=label,
        headers=headers,
        rows=data_rows,
        cells=cells,
        raw_format=raw_format if raw_format in ("html", "markdown") else "unknown",
        raw=raw,
        notes=list(notes),
    )


def _nearby_label(source: str, start: int) -> str:
    prefix = source[max(0, start - 600):start]
    for line in reversed(prefix.splitlines()):
        candidate = normalize_text(_clean_markup(line))
        if not candidate:
            continue
        match = _LABEL_RE.match(candidate)
        if match:
            return candidate
        # Do not walk through a previous paragraph looking for a label.
        if len(candidate) > 180:
            break
    return ""


def parse_html_tables(source: str, *, part: str = "UNKNOWN", revision: str = "") -> list[_TableOccurrence]:
    """Parse HTML tables and return exact source spans plus normalized data."""

    occurrences: list[_TableOccurrence] = []
    matches = list(_HTML_TABLE_RE.finditer(source))
    for ordinal, match in enumerate(matches, 1):
        parser = _HTMLTableParser()
        parser.feed(match.group(0))
        parser.close()
        if not parser.tables:
            continue
        parsed = parser.tables[0]
        label = normalize_text(parsed.get("caption", "")) or _nearby_label(source, match.start())
        table = _table_data_from_raw(
            parsed.get("rows", []),
            raw_format="html",
            raw=match.group(0),
            label=label,
            notes=parsed.get("notes", []),
        )
        occurrences.append(_TableOccurrence(match.start(), match.end(), table, label, ordinal))
    return occurrences


def parse_markdown_tables(source: str, *, part: str = "UNKNOWN", revision: str = "") -> list[_TableOccurrence]:
    """Parse pipe tables without consuming adjacent prose or headings."""

    lines = source.splitlines(keepends=True)
    offsets: list[int] = []
    cursor = 0
    for line in lines:
        offsets.append(cursor)
        cursor += len(line)
    occurrences: list[_TableOccurrence] = []
    index = 0
    ordinal = 0
    while index + 1 < len(lines):
        if "|" not in lines[index] or not _is_markdown_delimiter(lines[index + 1]):
            index += 1
            continue
        end_index = index + 2
        while end_index < len(lines):
            candidate = lines[end_index]
            if not candidate.strip() or "|" not in candidate:
                break
            end_index += 1
        raw = "".join(lines[index:end_index])
        header_values = _split_markdown_row(lines[index])
        delimiter_values = _split_markdown_row(lines[index + 1])
        width = max(len(header_values), len(delimiter_values))
        header_values.extend([""] * (width - len(header_values)))
        data_rows = [_split_markdown_row(line) for line in lines[index + 2:end_index]]
        raw_rows: list[list[_RawCell]] = [[_RawCell(value, is_header=True) for value in header_values]]
        raw_rows.extend([[_RawCell(value) for value in row] for row in data_rows])
        label = _nearby_label(source, offsets[index])
        ordinal += 1
        table = _table_data_from_raw(raw_rows, raw_format="markdown", raw=raw, label=label)
        occurrences.append(_TableOccurrence(offsets[index], offsets[end_index - 1] + len(lines[end_index - 1]), table, label, ordinal))
        index = end_index
    return occurrences


def parse_tables(source: str, *, part: str = "UNKNOWN", revision: str = "") -> list[_TableOccurrence]:
    """Return HTML and Markdown tables in document order.

    Markdown spans inside HTML tables are ignored, preventing duplicate rows.
    """

    html_tables = parse_html_tables(source, part=part, revision=revision)
    html_ranges = [(table.start, table.end) for table in html_tables]
    markdown_tables = [
        table for table in parse_markdown_tables(source, part=part, revision=revision)
        if not any(start <= table.start < end for start, end in html_ranges)
    ]
    all_tables = sorted(html_tables + markdown_tables, key=lambda table: table.start)
    for ordinal, table in enumerate(all_tables, 1):
        table.ordinal = ordinal
    return all_tables


def _extract_headings(source: str) -> list[tuple[int, int, int, str]]:
    headings: list[tuple[int, int, int, str]] = []
    for match in re.finditer(r"(?m)^\s*(#{1,6})[ \t]+([^\n]+?)\s*$", source):
        title = _clean_markup(match.group(2).rstrip("# "))
        if title:
            headings.append((match.start(), match.end(), len(match.group(1)), title))
    for match in _HTML_HEADING_RE.finditer(source):
        title = _clean_markup(match.group(0))
        title = re.sub(r"^\s*", "", title)
        level = int(match.group("level"))
        if title:
            headings.append((match.start(), match.end(), level, title))
    return sorted(headings, key=lambda item: (item[0], item[1]))


def _attrs_from_img(value: str) -> dict[str, str]:
    attrs: dict[str, str] = {}
    for match in re.finditer(r"([:\w-]+)\s*=\s*([\"'])(.*?)\2", value, re.S):
        attrs[match.group(1).lower()] = html.unescape(match.group(3))
    return attrs


def _extract_figures(source: str, table_ranges: Sequence[tuple[int, int]]) -> list[_FigureOccurrence]:
    figures: list[_FigureOccurrence] = []
    spans: list[tuple[int, int, str, str, str]] = []
    for match in _IMAGE_RE.finditer(source):
        spans.append((match.start(), match.end(), match.group("alt") or "", match.group("title") or "", match.group("src")))
    for match in _HTML_IMAGE_RE.finditer(source):
        attrs = _attrs_from_img(match.group("attrs"))
        if attrs.get("src"):
            spans.append((match.start(), match.end(), attrs.get("alt", ""), attrs.get("title", ""), attrs["src"]))
    for index, (start, end, alt, title, image_path) in enumerate(sorted(spans), 1):
        if any(left <= start < right for left, right in table_ranges):
            continue
        nearby = source[max(0, start - 250):min(len(source), end + 250)]
        label_match = re.search(r"\b(fig(?:ure)?\.?\s*[0-9]+(?:\.[0-9]+)?)\s*[:.\-]?\s*([^\n]*)", nearby, re.I)
        label = normalize_text(label_match.group(0)) if label_match else (title or alt or f"Figure {index}")
        figures.append(_FigureOccurrence(start, end, label, title or alt or label, image_path, image_path))
    return figures


def _page_for(source: str, offset: int, page_metadata: Any = None) -> int | None:
    """Resolve optional page metadata from markers, maps, ranges, or callbacks."""

    marker_page: int | None = None
    for marker in _PAGE_MARKER_RE.finditer(source, 0, offset):
        marker_page = int(marker.group(1))
    if marker_page is not None:
        return marker_page
    if callable(page_metadata):
        try:
            result = page_metadata(offset)
            return int(result) if result is not None else None
        except (TypeError, ValueError):
            return None
    if isinstance(page_metadata, Mapping):
        candidates: list[tuple[int, int]] = []
        for key, value in page_metadata.items():
            try:
                key_int, value_int = int(key), int(value)
            except (TypeError, ValueError):
                continue
            if key_int <= offset:
                candidates.append((key_int, value_int))
        return max(candidates)[1] if candidates else None
    if isinstance(page_metadata, Sequence) and not isinstance(page_metadata, (str, bytes)):
        for entry in page_metadata:
            if isinstance(entry, Mapping):
                try:
                    start = int(entry.get("start", entry.get("offset", 0)))
                    end = int(entry.get("end", len(source)))
                    page = int(entry["page"])
                except (KeyError, TypeError, ValueError):
                    continue
                if start <= offset < end:
                    return page
            elif isinstance(entry, Sequence) and len(entry) >= 3:
                try:
                    start, end, page = int(entry[0]), int(entry[1]), int(entry[2])
                except (TypeError, ValueError):
                    continue
                if start <= offset < end:
                    return page
    return None


def _source_ref(
    part: str,
    revision: str,
    kind: str,
    path: str,
    *,
    source_file: str,
    section_path: Sequence[str] = (),
    heading: str = "",
    table_label: str = "",
    figure_label: str = "",
    row_index: int | None = None,
    cell_refs: Sequence[str] = (),
    text_span: str = "",
    page: int | None = None,
) -> SourceRef:
    source_id = stable_id(part, revision, "source", path, discriminator=kind)
    return SourceRef(
        source_id=source_id,
        part=part,
        revision=revision,
        source_file=source_file,
        section_path=list(section_path),
        heading=heading,
        table_label=table_label,
        figure_label=figure_label,
        row_index=row_index,
        cell_refs=list(cell_refs),
        text_span=text_span,
        page=page,
    )


def _source_path(section_path: Sequence[str], kind: str, ordinal: int | str) -> str:
    return "/".join([*(section_path or ["document"]), kind, str(ordinal)])


def _attach_table_provenance(
    table: TableData,
    *,
    part: str,
    revision: str,
    table_path: str,
) -> tuple[TableData, dict[int, list[str]]]:
    row_cells: dict[int, list[str]] = {}
    updated_cells: list[TableCell] = []
    for cell in table.cells:
        cell_id = stable_id(
            part,
            revision,
            "source",
            f"{table_path}/row-{cell.row}/column-{cell.column}",
            discriminator=f"cell:{cell.text}:{cell.row_span}:{cell.column_span}",
        )
        updated_cells.append(_model_copy(cell, source_ref=cell_id))
        for row in range(cell.row, cell.row + cell.row_span):
            row_cells.setdefault(row, []).append(cell_id)
    return _model_copy(table, cells=updated_cells), row_cells


def _header_keys(headers: Sequence[str], width: int) -> list[str]:
    keys: list[str] = []
    seen: dict[str, int] = {}
    for index in range(width):
        value = normalize_text(headers[index]) if index < len(headers) else ""
        key = value or f"column_{index + 1}"
        count = seen.get(key.lower(), 0)
        seen[key.lower()] = count + 1
        if count:
            key = f"{key}_{count + 1}"
        keys.append(key)
    return keys


def _classify_row(label: str, headers: Sequence[str], row: Sequence[str]) -> tuple[str, list[str]]:
    header_text = " ".join(headers).lower()
    row_text = " ".join(row).lower()
    combined = f"{label} {header_text} {row_text}"
    archetypes: list[str] = []

    def add(name: str) -> None:
        if name not in archetypes:
            archetypes.append(name)

    if re.search(r"\b(mode|modes|operating mode)\b", combined):
        add("mode")
    if re.search(r"\b(pin|pad|signal|gpio|io\d*)\b", combined):
        add("pin")
    if re.search(r"\b(memory map|memory region|address range|start address|end address|block size|offset)\b", combined):
        add("memory_map")
    if re.search(r"\b(command|opcode|instruction|mnemonic|program|erase|read id|write enable|wren|dummy cycle)\b", combined):
        add("command")
    if re.search(r"\b(timing|frequency|freq|clock|cycle|dummy|latency|min\.?|max\.?|typ\.?|ns|us|ms|mhz|khz)\b", combined):
        add("timing")
    if re.search(r"\b(bitfield|bit field|register bit|bits?|field|reset|default|access|r/?w|register)\b", combined):
        if re.search(r"\b(bits?|bitfield|bit field|field)\b", combined) or re.search(r"\d+\s*:\s*\d+", row_text):
            add("bitfield")
        else:
            add("register")
    if not archetypes:
        add("generic")
    # The first type is intentionally deterministic; all matching archetypes
    # remain available in values["archetypes"] for multi-purpose rows.
    # A command row with an address column is still a command.  Likewise,
    # explicit timing terms outrank a mode condition such as "Quad I/O".
    priority = ["command", "timing", "memory_map", "mode", "pin", "bitfield", "register", "generic"]
    archetypes.sort(key=lambda value: priority.index(value) if value in priority else len(priority))
    return archetypes[0], archetypes


def _row_values(headers: Sequence[str], row: Sequence[str], label: str, row_index: int) -> dict[str, Any]:
    width = max(len(headers), len(row))
    keys = _header_keys(headers, width)
    values: dict[str, Any] = {key: normalize_text(row[index]) if index < len(row) else "" for index, key in enumerate(keys)}
    values["row_index"] = row_index
    if label:
        values["table_label"] = label
    for key, value in list(values.items()):
        key_lower = key.lower()
        if not isinstance(value, str) or not value:
            continue
        if "opcode" in key_lower or "command code" in key_lower:
            values.setdefault("opcode", value)
        elif "dummy" in key_lower:
            values.setdefault("dummy_cycles", value)
        elif "frequency" in key_lower or key_lower in {"freq", "clock"}:
            values.setdefault("frequency", value)
        elif re.search(r"\b(?:mhz|khz|hz)\b", key_lower):
            unit_match = re.search(r"\b(mhz|khz|hz)\b", key_lower)
            scalar: Any = int(value) if value.isdigit() else value
            values.setdefault(
                "frequency",
                {
                    "value": scalar,
                    "unit": {"mhz": "MHz", "khz": "kHz", "hz": "Hz"}.get(
                        unit_match.group(1).lower(), "") if unit_match else "",
                    "condition": re.sub(r"\s*\([^)]*\)\s*", "", key).strip(),
                },
            )
        elif "address" in key_lower:
            values.setdefault("address", value)
        elif "reset" in key_lower or "default" in key_lower:
            values.setdefault("reset_or_default", value)
        elif "access" in key_lower:
            values.setdefault("access", value)
    # Datasheet specification tables often use a generic ``Parameter``
    # header, with the actual semantic name in the first cell.  Promote those
    # row labels so an agent can retrieve "dummy cycles" as an exact fact.
    row_label = normalize_text(row[0]).lower() if row else ""
    row_value = normalize_text(row[1]) if len(row) > 1 else (normalize_text(row[0]) if row else "")
    if "dummy" in row_label:
        values.setdefault("dummy_cycles", row_value)
    if "frequency" in row_label or row_label in {"freq", "clock", "clock frequency"}:
        values.setdefault("frequency", row_value)
    if "opcode" in row_label or "command" in row_label:
        values.setdefault("opcode", row_value)
    return values


def _row_text(headers: Sequence[str], row: Sequence[str]) -> str:
    keys = _header_keys(headers, max(len(headers), len(row)))
    return "; ".join(f"{key}: {normalize_text(row[index])}" for index, key in enumerate(keys) if index < len(row) and normalize_text(row[index]))


def _split_prose_chunks(text: str, *, min_tokens: int = 150, max_tokens: int = 350) -> list[tuple[str, int, int]]:
    """Split one non-table source interval into bounded, source-local chunks."""

    paragraphs = list(re.finditer(r"\S(?:.*?\S)?(?=\n\s*\n|\Z)", text, re.S))
    if not paragraphs and text.strip():
        paragraphs = [re.search(r"\S.*\S|\S", text, re.S)]  # type: ignore[list-item]
    chunks: list[tuple[str, int, int]] = []
    pending: list[tuple[str, int, int]] = []
    pending_tokens = 0

    def flush() -> None:
        nonlocal pending, pending_tokens
        if pending:
            raw_start = pending[0][1]
            raw_end = pending[-1][2]
            chunks.append(("\n\n".join(item[0] for item in pending), raw_start, raw_end))
            pending = []
            pending_tokens = 0

    for match in paragraphs:
        raw = text[match.start():match.end()]
        cleaned = _clean_markup(raw)
        if not cleaned:
            continue
        count = token_count(cleaned)
        if count > max_tokens:
            flush()
            words = cleaned.split()
            for start in range(0, len(words), max(1, max_tokens)):
                piece = " ".join(words[start:start + max_tokens])
                chunks.append((piece, match.start(), match.end()))
            continue
        if pending and pending_tokens + count > max_tokens:
            flush()
        pending.append((cleaned, match.start(), match.end()))
        pending_tokens += count
        if pending_tokens >= min_tokens:
            flush()
    flush()
    return chunks


def _content_type(text: str) -> str:
    lower = text.lower()
    if re.search(r"\b(program|erase|write enable|read command|sequence|initiali[sz]e)\b", lower):
        return "operation"
    if re.search(r"\b(timing|frequency|dummy cycle|latency|ns|mhz|khz)\b", lower):
        return "timing"
    if re.search(r"\b(mode|quad|dual|str|dtr)\b", lower):
        return "mode"
    if re.search(r"\b(command|opcode|instruction)\b", lower):
        return "command"
    return "general"


def _section_context(headings: Sequence[_Heading], position: int, document_id: str) -> tuple[str, list[str], str]:
    stack: list[_Heading] = []
    for heading in headings:
        if heading.start >= position:
            break
        while stack and stack[-1].level >= heading.level:
            stack.pop()
        stack.append(heading)
    if not stack:
        return document_id, [], ""
    current = stack[-1]
    return current.item_id, [heading.title for heading in stack], current.title


def _relation_id(part: str, revision: str, relation: str, source_id: str, target_id: str) -> str:
    return stable_id(part, revision, "relation", f"{source_id}->{target_id}", discriminator=relation)


def _make_relation(
    part: str,
    revision: str,
    relation: str,
    source_id: str,
    target_id: str,
    *,
    label: str = "",
    source_refs: Sequence[str] = (),
) -> GraphRelation:
    return GraphRelation(
        id=_relation_id(part, revision, relation, source_id, target_id),
        part=part,
        relation=relation,  # type: ignore[arg-type]
        source_id=source_id,
        target_id=target_id,
        label=label,
        source_refs=list(source_refs),
    )


def _build_relations(part: str, revision: str, evidence: Sequence[EvidenceItem], document_id: str) -> list[GraphRelation]:
    relations: list[GraphRelation] = []
    by_id = {item.id: item for item in evidence}
    for item in evidence:
        parent_id = item.parent_id or document_id
        if parent_id != item.id and parent_id in by_id:
            relations.append(_make_relation(part, revision, "CONTAINS", parent_id, item.id))
        for source in item.sources:
            relations.append(_make_relation(part, revision, "EVIDENCED_BY", item.id, source.source_id, source_refs=[source.source_id]))

    ordered = [item for item in evidence if item.id != document_id]
    for previous, current in zip(ordered, ordered[1:]):
        relations.append(_make_relation(part, revision, "NEXT", previous.id, current.id))

    label_targets: dict[str, str] = {}
    for item in evidence:
        if item.kind == "table":
            for key in _label_keys(item.title):
                label_targets.setdefault(key, item.id)
        elif item.kind == "figure":
            for key in _label_keys(item.title):
                label_targets.setdefault(key, item.id)
        elif item.kind in ("chapter", "section"):
            for key in _label_keys(item.title):
                label_targets.setdefault(key, item.id)
    for item in evidence:
        text = f"{item.title} {item.text}"
        for match in _REF_RE.finditer(text):
            prefix, number = match.group(1).lower().rstrip("."), match.group(2)
            kind = "table" if prefix in {"table", "tab"} else "figure" if prefix in {"figure", "fig"} else "section"
            target = label_targets.get(_label_key(f"{kind} {number}"))
            if target and target != item.id:
                relations.append(_make_relation(part, revision, "REFERENCES", item.id, target, label=match.group(0)))
    unique: dict[str, GraphRelation] = {}
    for relation in relations:
        unique[relation.id] = relation
    return list(unique.values())


def _label_key(value: str) -> str:
    value = normalize_text(value).lower()
    value = re.sub(r"^(?:table|tab\.?|figure|fig\.?|section|sec\.?)\s*", "", value)
    return re.sub(r"[^a-z0-9]+", " ", value).strip()


def _label_keys(value: str) -> set[str]:
    """Return full and numbered keys so ``See Table 1`` resolves captions."""

    keys = {_label_key(value)}
    match = re.match(r"\s*(?:table|tab\.?|figure|fig\.?|section|sec\.?)\s*([0-9]+(?:\.[0-9]+)?)", value, re.I)
    if match:
        keys.add(match.group(1).lower())
    return {key for key in keys if key}


def _coverage(part: str, revision: str, evidence: Sequence[EvidenceItem], warnings: list[str], local_ai_used: bool) -> CoverageReport:
    domains = ["tables", "figures", "prose", "register", "bitfield", "command", "timing", "memory_map", "mode", "pin", "generic"]
    counts: dict[str, int] = {domain: 0 for domain in domains}
    indexed: dict[str, int] = {domain: 0 for domain in domains}
    validated: dict[str, int] = {domain: 0 for domain in domains}
    for item in evidence:
        domain = "tables" if item.kind == "table" else "figures" if item.kind == "figure" else "prose" if item.kind == "prose" else item.semantic_type
        if domain not in counts:
            domain = "generic"
        counts[domain] += 1
        indexed[domain] += 1
        if item.validated:
            validated[domain] += 1
    report_domains: dict[str, CoverageDomain] = {}
    for domain in domains:
        detected = counts[domain]
        state = "complete" if detected else "unavailable"
        domain_warnings: list[str] = []
        if not detected:
            domain_warnings.append(f"No {domain.replace('_', ' ')} evidence detected")
        report_domains[domain] = CoverageDomain(
            state=state, detected=detected, indexed=indexed[domain], validated=validated[domain], warnings=domain_warnings
        )
    return CoverageReport(part=part, revision=revision, domains=report_domains, warnings=warnings, local_ai_used=local_ai_used)


def _apply_local_enrichment(
    evidence: list[EvidenceItem],
    hook: Callable[[list[EvidenceItem]], Iterable[EvidenceItem]] | None,
    valid_source_ids: set[str],
) -> tuple[list[EvidenceItem], bool, list[str]]:
    """Apply only anchored, schema-valid local enrichment returned by ``hook``."""

    if hook is None:
        return evidence, False, []
    warnings: list[str] = []
    known_ids = {item.id for item in evidence}
    additions: list[EvidenceItem] = []
    try:
        candidates = list(hook(list(evidence)))
    except Exception as exc:  # hooks are optional and must not break deterministic ingestion
        return evidence, False, [f"Local enrichment hook failed: {type(exc).__name__}"]
    for candidate in candidates:
        if not isinstance(candidate, EvidenceItem):
            warnings.append("Rejected local enrichment item with invalid schema")
            continue
        if candidate.id in known_ids or not candidate.sources:
            warnings.append(f"Rejected unanchored or duplicate local enrichment item: {candidate.id}")
            continue
        if not all(source.source_id in valid_source_ids for source in candidate.sources):
            warnings.append(f"Rejected local enrichment item without known source: {candidate.id}")
            continue
        candidate = _model_copy(candidate, validated=True, enrichment="local_ai")
        additions.append(candidate)
        known_ids.add(candidate.id)
    return evidence + additions, bool(additions), warnings


def ingest_document(
    source: str | Path,
    part: str = "",
    *,
    revision: str = "",
    source_file: str = "",
    document_title: str = "",
    vendor: str = "",
    device_type: str = "",
    page_metadata: Any = None,
    enrichment_hook: Callable[[list[EvidenceItem]], Iterable[EvidenceItem]] | None = None,
) -> CorpusArtifact:
    """Build a deterministic ``CorpusArtifact`` from MinerU text.

    ``source`` may be text or a local Markdown/HTML path.  ``enrichment_hook``
    is intentionally narrow: it receives deterministic evidence and may
    return additional ``EvidenceItem`` objects only when every source is
    anchored to an existing source reference.  No model or network call is
    made here.
    """

    if isinstance(source, Path):
        if not source_file:
            source_file = str(source)
        source = source.read_text(encoding="utf-8", errors="replace")
    if not isinstance(source, str):
        raise TypeError("source must be text or a pathlib.Path")
    if not part.strip():
        raise ValueError("part is required for part-scoped ingestion")
    part = part.strip()
    source_file = source_file or "<memory>"
    document_id = stable_id(part, revision, "document", "document")
    document_source = _source_ref(part, revision, "document", "document", source_file=source_file, text_span=source, page=_page_for(source, 0, page_metadata))
    document = EvidenceItem(
        id=document_id,
        part=part,
        kind="document",
        title=document_title or part,
        text=document_title or part,
        parent_id="",
        sources=[document_source],
        values={"source_file": source_file},
    )

    table_occurrences = parse_tables(source, part=part, revision=revision)
    table_ranges = [(table.start, table.end) for table in table_occurrences]
    figures = _extract_figures(source, table_ranges)
    raw_headings = _extract_headings(source)
    headings: list[_Heading] = []
    heading_stack: list[_Heading] = []
    for index, (start, end, level, title) in enumerate(raw_headings, 1):
        while heading_stack and heading_stack[-1].level >= level:
            heading_stack.pop()
        parent_id = heading_stack[-1].item_id if heading_stack else document_id
        path = [heading.title for heading in heading_stack] + [title]
        kind = "chapter" if level == 1 else "section"
        item_id = stable_id(part, revision, kind, "/".join(path), discriminator=f"heading:{index}")
        heading = _Heading(start, end, level, title, item_id, parent_id, path)
        headings.append(heading)
        heading_stack.append(heading)

    events: list[_Event] = []
    all_source_ids: set[str] = {document_source.source_id}
    for heading in headings:
        parent_id, path, current_heading = _section_context(headings, heading.start + 1, document_id)
        kind = "chapter" if heading.level == 1 else "section"
        ref = _source_ref(
            part, revision, kind, _source_path(path[:-1], kind, heading.title), source_file=source_file,
            section_path=path, heading=heading.title, text_span=source[heading.start:heading.end],
            page=_page_for(source, heading.start, page_metadata),
        )
        all_source_ids.add(ref.source_id)
        item = EvidenceItem(
            id=heading.item_id,
            part=part,
            kind=kind,
            title=heading.title,
            text=heading.title,
            parent_id=heading.parent_id,
            semantic_type="general",
            sources=[ref],
        )
        events.append(_Event(float(heading.start), "heading", item))

    table_items: list[EvidenceItem] = []
    for table_index, occurrence in enumerate(table_occurrences, 1):
        parent_id, path, current_heading = _section_context(headings, occurrence.start, document_id)
        label = occurrence.label or occurrence.table.label or f"Table {table_index}"
        table_path = _source_path(path, "table", label)
        table_data, row_cells = _attach_table_provenance(occurrence.table, part=part, revision=revision, table_path=table_path)
        table_id = stable_id(part, revision, "table", table_path, discriminator=f"table:{table_index}:{hashlib.sha1(table_data.raw.encode('utf-8')).hexdigest()[:8]}")
        table_ref = _source_ref(
            part, revision, "table", table_path, source_file=source_file, section_path=path,
            heading=current_heading, table_label=label, text_span=table_data.raw,
            page=_page_for(source, occurrence.start, page_metadata),
        )
        all_source_ids.add(table_ref.source_id)
        table_data = _model_copy(table_data, label=label)
        table_item = EvidenceItem(
            id=table_id,
            part=part,
            kind="table",
            title=label,
            text=_table_summary(table_data),
            parent_id=parent_id,
            semantic_type="table",
            values={"raw_format": table_data.raw_format, "row_count": len(table_data.rows), "column_count": len(table_data.headers)},
            table=table_data,
            sources=[table_ref],
        )
        table_items.append(table_item)
        events.append(_Event(float(occurrence.start), "table", table_item))
        for row_offset, row in enumerate(table_data.rows):
            physical_row = row_offset + 1 if table_data.headers else row_offset
            archetype, archetypes = _classify_row(label, table_data.headers, row)
            row_id = stable_id(part, revision, "table_row", table_path, discriminator=f"row:{physical_row}:{'|'.join(row)}")
            cell_refs = row_cells.get(physical_row, [])
            row_ref = _source_ref(
                part, revision, "table_row", f"{table_path}/row-{physical_row}", source_file=source_file,
                section_path=path, heading=current_heading, table_label=label, row_index=physical_row,
                cell_refs=cell_refs, text_span=" | ".join(row), page=_page_for(source, occurrence.start, page_metadata),
            )
            all_source_ids.add(row_ref.source_id)
            values = _row_values(table_data.headers, row, label, physical_row)
            values["archetype"] = archetype
            values["archetypes"] = archetypes
            row_item = EvidenceItem(
                id=row_id,
                part=part,
                kind="table_row",
                title=normalize_text(row[0]) if row and normalize_text(row[0]) else f"{label} row {physical_row}",
                text=_row_text(table_data.headers, row),
                aliases=[value for value in row if value],
                parent_id=table_id,
                sequence=physical_row,
                semantic_type=archetype,
                values=values,
                sources=[row_ref],
            )
            events.append(_Event(float(occurrence.start) + 0.001 + row_offset / 100000.0, "row", row_item))

    table_end = {occurrence.end for occurrence in table_occurrences}
    for index, figure in enumerate(figures, 1):
        parent_id, path, current_heading = _section_context(headings, figure.start, document_id)
        figure_path = _source_path(path, "figure", figure.label or index)
        figure_id = stable_id(part, revision, "figure", figure_path, discriminator=f"figure:{figure.source_path}")
        ref = _source_ref(
            part, revision, "figure", figure_path, source_file=source_file, section_path=path,
            heading=current_heading, figure_label=figure.label, text_span=figure.text,
            page=_page_for(source, figure.start, page_metadata),
        )
        all_source_ids.add(ref.source_id)
        item = EvidenceItem(
            id=figure_id, part=part, kind="figure", title=figure.label or f"Figure {index}",
            text=f"{figure.title} ({figure.source_path})", parent_id=parent_id,
            semantic_type="figure", values={"path": figure.source_path, "alt": figure.title}, sources=[ref],
        )
        events.append(_Event(float(figure.start), "figure", item))

    structural_spans = sorted([(heading.start, heading.end) for heading in headings] + table_ranges + [(figure.start, figure.end) for figure in figures])
    boundaries = [0]
    for start, end in structural_spans:
        if start > boundaries[-1]:
            boundaries.extend([start, end])
        elif end > boundaries[-1]:
            boundaries[-1] = end
    if boundaries[-1] < len(source):
        boundaries.append(len(source))
    for left, right in zip(boundaries[::2], boundaries[1::2]):
        if right <= left:
            continue
        raw_interval = source[left:right]
        # This interval must not cross a table, heading, or figure.  A table
        # label remains text evidence, while the table itself is structured.
        for chunk_index, (chunk, rel_start, rel_end) in enumerate(_split_prose_chunks(raw_interval), 1):
            absolute_start = left + rel_start
            parent_id, path, current_heading = _section_context(headings, absolute_start, document_id)
            prose_path = _source_path(path, "prose", f"{absolute_start}-{chunk_index}")
            ref = _source_ref(
                part, revision, "prose", prose_path, source_file=source_file, section_path=path,
                heading=current_heading, text_span=raw_interval[rel_start:rel_end],
                page=_page_for(source, absolute_start, page_metadata),
            )
            all_source_ids.add(ref.source_id)
            item_id = stable_id(part, revision, "prose", prose_path, discriminator=f"chunk:{chunk_index}")
            item = EvidenceItem(
                id=item_id, part=part, kind="prose", title=current_heading or "Prose", text=chunk,
                parent_id=parent_id, semantic_type=_content_type(chunk),
                values={"token_count": token_count(chunk)}, sources=[ref],
            )
            events.append(_Event(float(absolute_start) + chunk_index / 1000000.0, "prose", item))

    events.sort(key=lambda event: (event.position, {"heading": 0, "table": 1, "row": 2, "figure": 1, "prose": 3}.get(event.kind, 4)))
    evidence: list[EvidenceItem] = [document]
    for sequence, event in enumerate(events, 1):
        evidence.append(_model_copy(event.item, sequence=sequence))

    evidence, local_ai_used, enrichment_warnings = _apply_local_enrichment(evidence, enrichment_hook, all_source_ids)
    warnings = list(enrichment_warnings)
    if not table_occurrences:
        warnings.append("No HTML or Markdown tables detected")
    if not figures:
        warnings.append("No figures detected")
    coverage = _coverage(part, revision, evidence, warnings, local_ai_used)
    relations = _build_relations(part, revision, evidence, document_id)
    title = document_title or next((heading.title for heading in headings if heading.level == 1), part)
    catalog = CatalogPart(part=part, vendor=vendor, title=title, revision=revision, device_type=device_type, coverage=coverage)
    manifest = IndexManifest(
        part=part, revision=revision, source_sha256=hashlib.sha256(source.encode("utf-8")).hexdigest(),
        evidence_count=len(evidence), relation_count=len(relations),
        enrichment_status="complete" if local_ai_used else "skipped",
    )
    return CorpusArtifact(manifest=manifest, catalog=catalog, coverage=coverage, evidence=evidence, relations=relations)


def _table_summary(table: TableData) -> str:
    lines = []
    if table.headers:
        lines.append(" | ".join(table.headers))
    lines.extend(" | ".join(row) for row in table.rows)
    return "\n".join(lines)


def parse_markdown_document(
    markdown: str | Path,
    part: str = "",
    *,
    vendor: str = "",
    revision: str = "",
    source_file: str = "",
    device_type: str = "",
    page_metadata: Any = None,
) -> CorpusArtifact:
    """Parse a Markdown/HTML MinerU document without writing an artifact.

    The returned object is intentionally an in-memory ``CorpusArtifact``.  A
    parent pipeline can serialize it, run validated enrichment, or index it.
    This helper accepts HTML tables embedded in Markdown because that is the
    common MinerU output shape.
    """

    return ingest_document(
        markdown,
        part,
        revision=revision,
        source_file=source_file,
        vendor=vendor,
        device_type=device_type,
        page_metadata=page_metadata,
    )


def build_corpus(
    part: str,
    *,
    vendor: str = "",
    revision: str = "",
) -> CorpusArtifact:
    """Build one part-scoped corpus from every MinerU section document.

    Each file is parsed independently so row, cell, and prose provenance keeps
    the actual Markdown path. A synthetic top-level chapter makes entity IDs
    unique across files; original Markdown headings are demoted one level so
    their hierarchy remains nested beneath that chapter.
    """

    sections = _catalog.iter_sections(part)
    if not sections:
        raise FileNotFoundError(
            f"No MinerU Markdown found for {part!r} under {_catalog.md_dir(part)}"
        )

    evidence: list[EvidenceItem] = []
    source_documents: list[SourceRef] = []
    source_texts: list[str] = []
    document_id = stable_id(part, revision, "document", "document")
    for section in sections:
        raw = section.path.read_text(encoding="utf-8", errors="replace")
        source_texts.append(raw)
        chapter = _catalog.block_title(section.section_name)
        nested = re.sub(r"(?m)^(#{1,5})(?=\s)", r"#\1", raw)
        parsed = ingest_document(
            f"# {chapter}\n\n{nested}",
            part,
            revision=revision,
            source_file=str(section.path),
            document_title=part,
            vendor=vendor,
        )
        source_documents.extend(parsed.evidence[0].sources)
        for item in parsed.evidence[1:]:
            evidence.append(_model_copy(item, sequence=len(evidence) + 1))

    document = EvidenceItem(
        id=document_id,
        part=part,
        kind="document",
        title=part,
        text=part,
        sources=source_documents,
        values={"source_files": [str(section.path) for section in sections]},
    )
    evidence.insert(0, document)
    warnings = []
    if not any(item.kind == "table" for item in evidence):
        warnings.append("No HTML or Markdown tables detected")
    if not any(item.kind == "figure" for item in evidence):
        warnings.append("No figures detected")
    coverage = _coverage(part, revision, evidence, warnings, False)
    relations = _build_relations(part, revision, evidence, document_id)
    catalog = CatalogPart(
        part=part,
        vendor=vendor,
        title=part,
        revision=revision,
        coverage=coverage,
    )
    manifest = IndexManifest(
        part=part,
        revision=revision,
        source_sha256=hashlib.sha256("\n".join(source_texts).encode("utf-8")).hexdigest(),
        evidence_count=len(evidence),
        relation_count=len(relations),
    )
    return CorpusArtifact(
        manifest=manifest,
        catalog=catalog,
        coverage=coverage,
        evidence=evidence,
        relations=relations,
    )


# Kept as a neutral compatibility name for callers that already use a generic
# document parser.  It does not create a second ingestion implementation.
parse_document = ingest_document


__all__ = [
    "build_corpus",
    "ingest_document",
    "normalize_text",
    "parse_document",
    "parse_markdown_document",
    "parse_html_tables",
    "parse_markdown_tables",
    "parse_tables",
    "token_count",
]
