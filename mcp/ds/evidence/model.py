"""Structured contracts for the canonical evidence API."""

from __future__ import annotations

import hashlib
import re
from typing import Any, Literal

from pydantic import BaseModel, Field

EntityKind = Literal[
    "document", "chapter", "section", "table", "table_row", "figure",
    "prose", "register", "bitfield", "pin", "command", "mode",
    "operation", "step", "parameter", "constraint", "warning",
    "memory_region",
]
FocusKind = Literal["auto", "configure", "exact", "operation", "timing", "explain"]
Confidence = Literal["high", "medium", "low"]
CoverageState = Literal["complete", "partial", "unavailable"]


def _slug(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")[:72] or "item"


def stable_id(part: str, revision: str, kind: str, path: str, *, discriminator: str = "") -> str:
    raw = "\x1f".join((part.upper(), revision, kind, path, discriminator))
    return f"ds://{part.upper()}/{_slug(revision or 'unknown')}/{kind}/{_slug(path)}-{hashlib.sha1(raw.encode()).hexdigest()[:10]}"


class SourceRef(BaseModel):
    source_id: str
    part: str
    revision: str = ""
    source_file: str = ""
    section_path: list[str] = Field(default_factory=list)
    heading: str = ""
    table_label: str = ""
    figure_label: str = ""
    row_index: int | None = None
    cell_refs: list[str] = Field(default_factory=list)
    text_span: str = ""
    page: int | None = None


class TableCell(BaseModel):
    row: int
    column: int
    text: str = ""
    row_span: int = 1
    column_span: int = 1
    is_header: bool = False
    source_ref: str = ""


class TableData(BaseModel):
    label: str = ""
    headers: list[str] = Field(default_factory=list)
    rows: list[list[str]] = Field(default_factory=list)
    cells: list[TableCell] = Field(default_factory=list)
    raw_format: Literal["html", "markdown", "unknown"] = "unknown"
    raw: str = ""
    notes: list[str] = Field(default_factory=list)


class EvidenceItem(BaseModel):
    id: str
    part: str
    kind: EntityKind
    title: str
    text: str = ""
    aliases: list[str] = Field(default_factory=list)
    parent_id: str = ""
    sequence: int | None = None
    semantic_type: str = "general"
    values: dict[str, Any] = Field(default_factory=dict)
    table: TableData | None = None
    sources: list[SourceRef] = Field(default_factory=list)
    confidence: Confidence = "high"
    validated: bool = True
    enrichment: Literal["deterministic", "local_ai", "none"] = "deterministic"

    def embed_text(self) -> str:
        context = " ".join([self.title, *self.aliases, self.semantic_type]).strip()
        values = " ".join(f"{key}: {value}" for key, value in self.values.items())
        return "\n".join(part for part in (context, values, self.text) if part).strip()


class GraphRelation(BaseModel):
    id: str
    part: str
    relation: Literal[
        "CONTAINS", "NEXT", "REFERENCES", "REQUIRES", "ENABLES",
        "SETS_MODE", "USES_COMMAND", "READS_REGISTER", "WRITES_REGISTER",
        "CONSTRAINED_BY", "APPLIES_TO", "CONFLICTS_WITH", "EVIDENCED_BY",
    ]
    source_id: str
    target_id: str
    label: str = ""
    confidence: Confidence = "high"
    source_refs: list[str] = Field(default_factory=list)


class CoverageDomain(BaseModel):
    state: CoverageState
    detected: int = 0
    indexed: int = 0
    validated: int = 0
    warnings: list[str] = Field(default_factory=list)


class CoverageReport(BaseModel):
    part: str
    revision: str = ""
    domains: dict[str, CoverageDomain] = Field(default_factory=dict)
    warnings: list[str] = Field(default_factory=list)
    local_ai_used: bool = False


class IndexManifest(BaseModel):
    schema_version: str = "evidence"
    extractor_version: str = "1.0"
    part: str
    revision: str = ""
    source_sha256: str = ""
    embed_model: str = "BAAI/bge-base-en-v1.5"
    embed_dimension: int = 768
    evidence_count: int = 0
    relation_count: int = 0
    enrichment_status: Literal["complete", "partial", "skipped"] = "skipped"


class NormalizedSetting(BaseModel):
    name: str
    value: Any
    unit: str = ""
    condition: str = ""
    source_ids: list[str] = Field(default_factory=list)


class ActionStep(BaseModel):
    order: int
    action: str
    details: str = ""
    prerequisites: list[str] = Field(default_factory=list)
    verification: str = ""
    source_ids: list[str] = Field(default_factory=list)


class CatalogPart(BaseModel):
    part: str
    vendor: str = ""
    title: str = ""
    revision: str = ""
    device_type: str = ""
    coverage: CoverageReport | None = None


class CatalogNode(BaseModel):
    id: str
    kind: EntityKind
    title: str
    parent_id: str = ""
    sequence: int | None = None


class CatalogResponse(BaseModel):
    summary: str
    parts: list[CatalogPart] = Field(default_factory=list)
    outline: list[CatalogNode] = Field(default_factory=list)
    coverage: CoverageReport | None = None
    warnings: list[str] = Field(default_factory=list)
    next_cursor: str = ""


class QueryResponse(BaseModel):
    part: str
    question: str
    focus: FocusKind
    summary: str
    normalized_configuration: list[NormalizedSetting] = Field(default_factory=list)
    steps: list[ActionStep] = Field(default_factory=list)
    facts: list[EvidenceItem] = Field(default_factory=list)
    constraints: list[EvidenceItem] = Field(default_factory=list)
    related_entities: list[GraphRelation] = Field(default_factory=list)
    sources: list[SourceRef] = Field(default_factory=list)
    gaps: list[str] = Field(default_factory=list)
    conflicts: list[str] = Field(default_factory=list)
    coverage: CoverageReport | None = None
    confidence: Confidence = "low"
    truncated: bool = False


class GetResponse(BaseModel):
    part: str
    target: str
    summary: str
    entity: EvidenceItem | None = None
    candidates: list[EvidenceItem] = Field(default_factory=list)
    related_entities: list[GraphRelation] = Field(default_factory=list)
    sources: list[SourceRef] = Field(default_factory=list)
    gaps: list[str] = Field(default_factory=list)
    next_cursor: str = ""


class CorpusArtifact(BaseModel):
    manifest: IndexManifest
    catalog: CatalogPart
    coverage: CoverageReport
    evidence: list[EvidenceItem] = Field(default_factory=list)
    relations: list[GraphRelation] = Field(default_factory=list)


__all__ = [
    "ActionStep", "CatalogNode", "CatalogPart", "CatalogResponse", "CorpusArtifact",
    "CoverageDomain", "CoverageReport", "EntityKind", "EvidenceItem", "GetResponse",
    "GraphRelation", "IndexManifest", "NormalizedSetting", "QueryResponse", "SourceRef",
    "TableCell", "TableData", "FocusKind", "stable_id",
]
