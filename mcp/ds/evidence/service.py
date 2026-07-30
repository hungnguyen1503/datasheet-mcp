"""Deterministic three-tool evidence service.

The service is intentionally model-free at request time.  Search and exact
lookup evidence are merged, graph context is expanded with bounded rules, and
the implementation packet is assembled from validated source-backed fields.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from typing import Any

from .model import (
    ActionStep,
    CatalogNode,
    CatalogPart,
    CatalogResponse,
    CoverageReport,
    EvidenceItem,
    GetResponse,
    GraphRelation,
    NormalizedSetting,
    QueryResponse,
    SourceRef,
    TableData,
    stable_id,
)

_KINDS = {
    "document", "chapter", "section", "table", "table_row", "figure", "prose",
    "register", "bitfield", "pin", "command", "mode", "operation", "step",
    "parameter", "constraint", "warning", "memory_region",
}
_FOCUSES = {"auto", "configure", "exact", "operation", "timing", "explain"}
_RELATIONS = {
    "CONTAINS", "NEXT", "REFERENCES", "REQUIRES", "ENABLES", "SETS_MODE",
    "USES_COMMAND", "READS_REGISTER", "WRITES_REGISTER", "CONSTRAINED_BY",
    "APPLIES_TO", "CONFLICTS_WITH", "EVIDENCED_BY",
}
_PREREQUISITES = {"REQUIRES", "CONSTRAINED_BY"}


class DatasheetService:
    """Canonical evidence API: ``catalog``, ``query``, and ``get``."""

    def __init__(self, store: Any | None = None):
        self.store = store

    def catalog(self, part: str = "", cursor: str = "", limit: int = 200) -> CatalogResponse:
        raw = self._store().catalog(_part(part), cursor=str(cursor or ""), limit=_limit(limit, 200))
        if isinstance(raw, CatalogResponse):
            if part:
                return raw.model_copy(update={"parts": [p for p in raw.parts if _same_part(p.part, _part(part))]})
            return raw
        data = _mapping(raw) or {}
        if not data and isinstance(raw, (list, tuple, set)):
            data = {"parts": list(raw)}
        parts = []
        raw_parts = data.get("parts") or data.get("items") or []
        if isinstance(raw_parts, (Mapping, CatalogPart)):
            raw_parts = [raw_parts]
        for value in raw_parts:
            try:
                item = value if isinstance(value, CatalogPart) else CatalogPart.model_validate(value)
                if not part or _same_part(item.part, _part(part)):
                    parts.append(item)
            except Exception:
                continue
        outline = []
        raw_nodes = data.get("outline") or data.get("nodes") or []
        if isinstance(raw_nodes, (Mapping, CatalogNode)):
            raw_nodes = [raw_nodes]
        for value in raw_nodes:
            try:
                outline.append(value if isinstance(value, CatalogNode) else CatalogNode.model_validate(value))
            except Exception:
                continue
        coverage = _coverage(data.get("coverage"))
        return CatalogResponse(
            summary=str(data.get("summary") or f"{_part(part) or 'Datasheet'} catalog: {len(parts)} part(s)."),
            parts=parts,
            outline=outline,
            coverage=coverage,
            warnings=_strings(data.get("warnings", [])),
            next_cursor=str(data.get("next_cursor") or data.get("cursor") or ""),
        )

    def query(self, part: str, question: str, focus: str = "auto", max_tokens: int = 3000) -> QueryResponse:
        part = _required_part(part)
        question = str(question or "").strip()
        if not question:
            raise ValueError("question is required")
        focus = _focus(focus, question)
        budget = _budget(max_tokens)
        store = self._store()

        exact_values = [store.get(part, question, kind="auto", include_related=False, depth=0)]
        exact = _evidence(exact_values, part)
        if not exact:
            for term in _target_terms(question):
                exact_values.append(store.get(part, term, kind="auto", include_related=False, depth=0))
                exact = _evidence(exact_values, part)
                if exact:
                    break
        hybrid_raw = store.search(part, query=question, focus=focus, limit=32)
        items = _dedupe([*exact, *_evidence([hybrid_raw], part)])
        relations = _expand(store, part, [item.id for item in items], depth=2)
        metadata = _metadata([*exact_values, hybrid_raw], part)
        settings, conflicts = _settings(items)
        steps = _steps(items)
        constraints = [item for item in items if _constraint(item)]
        gaps = _gaps(question, items, settings, steps, metadata["gaps"])
        conflicts = _unique([*metadata["conflicts"], *conflicts])
        sources = _sources(part, items, settings, steps, relations, metadata["sources"])
        response = QueryResponse(
            part=part,
            question=question,
            focus=focus,
            summary=_summary(part, question, focus, items, settings, steps),
            normalized_configuration=settings,
            steps=steps,
            facts=items,
            constraints=constraints,
            related_entities=relations,
            sources=sources,
            gaps=_unique(gaps),
            conflicts=conflicts,
            coverage=metadata["coverage"],
            confidence=_confidence(items, sources, gaps, conflicts),
        )
        return _truncate(response, budget)

    def get(
        self,
        part: str,
        target: str,
        relation_depth: int = 1,
        cursor: str = "",
        limit: int = 100,
    ) -> GetResponse:
        part = _required_part(part)
        target = str(target or "").strip()
        if not target:
            raise ValueError("target is required")
        raw = self._store().get(
            part,
            target,
            kind="auto",
            include_related=False,
            depth=0,
        )
        entity, candidates, relations, metadata = _get_parts(raw, part)
        if relation_depth > 0:
            relations = _dedupe_relations([
                *relations,
                *_expand(
                    self._store(),
                    part,
                    [entity.id] if entity else [item.id for item in candidates],
                    depth=_number(relation_depth, 1),
                ),
            ])
        selected = ([entity] if entity else []) + candidates
        sources = _sources(part, [item for item in selected if item], [], [], relations, metadata["sources"])
        gaps = list(metadata["gaps"])
        if not entity and not candidates:
            gaps.append(f"No indexed evidence matched '{target}'.")
        elif not entity and len(candidates) > 1:
            gaps.append("Target is ambiguous; inspect candidates before configuring the device.")
        response = GetResponse(
            part=part,
            target=target,
            summary=_get_summary(target, entity, candidates),
            entity=entity,
            candidates=candidates[:_limit(limit, 100)],
            related_entities=relations[:_limit(limit, 100)],
            sources=sources,
            gaps=_unique(gaps),
            next_cursor=metadata["next_cursor"],
        )
        return response

    def prewarm(self) -> dict[str, str]:
        return self._store().prewarm()

    def _store(self) -> Any:
        if self.store is None:
            from .store_qdrant import EvidenceStoreQdrant

            self.store = EvidenceStoreQdrant()
        return self.store


def _part(value: Any) -> str:
    return str(value or "").strip().upper()


def _required_part(value: Any) -> str:
    result = _part(value)
    if not result:
        raise ValueError("part is required for a part-scoped evidence operation")
    return result


def _same_part(value: Any, part: str) -> bool:
    return not value or _part(value) == part


def _mapping(value: Any) -> Mapping[str, Any] | None:
    if isinstance(value, Mapping):
        return value
    if hasattr(value, "model_dump"):
        return value.model_dump()
    if hasattr(value, "__dict__"):
        return vars(value)
    return None


def _focus(value: str, question: str) -> str:
    requested = str(value or "auto").strip().lower()
    if requested not in _FOCUSES:
        raise ValueError(f"unsupported focus: {value}")
    if requested != "auto":
        return requested
    text = question.casefold()
    scores = {name: 0 for name in _FOCUSES if name != "auto"}
    groups = {
        "configure": {"configure", "configuration", "config", "setup", "initialize", "enable", "select", "set"},
        "exact": {"register", "registers", "bit", "bitfield", "address", "opcode", "field", "reset", "default"},
        "operation": {"program", "erase", "flow", "sequence", "operation", "write", "read", "resume", "suspend", "command"},
        "timing": {"timing", "cycle", "cycles", "frequency", "freq", "latency", "delay", "setup", "hold", "clock"},
        "explain": {"why", "what", "explain", "meaning", "background", "difference"},
    }
    for token in re.findall(r"[a-z0-9_]+", text):
        for name, words in groups.items():
            if token in words:
                scores[name] += 2
    for phrase, name, weight in (
        ("spi mode", "configure", 4), ("cpol", "configure", 3), ("cpha", "configure", 3),
        ("dummy cycle", "timing", 5), ("dummy cycles", "timing", 5),
        ("program/erase", "operation", 5), ("program erase", "operation", 5),
    ):
        if phrase in text:
            scores[name] += weight
    order = ("operation", "timing", "configure", "exact", "explain")
    return max(order, key=lambda name: (scores[name], -order.index(name))) if any(scores.values()) else "explain"


def _budget(value: Any) -> int:
    try:
        value = int(value)
    except (TypeError, ValueError):
        value = 3000
    return max(1000, min(6000, value))


def _limit(value: Any, default: int) -> int:
    try:
        value = int(value)
    except (TypeError, ValueError):
        value = default
    return max(1, min(value, 1000))


def _target_terms(question: str) -> list[str]:
    found = []
    for pattern in (r"\b[A-Za-z][A-Za-z0-9_]*\[[0-9:]+\]", r"\b0[xX][0-9A-Fa-f]+\b", r"\b[0-9A-Fa-f]{2,8}[hH]\b", r"\b[A-Z][A-Z0-9_]{1,}\b"):
        for match in re.findall(pattern, question):
            if match.casefold() not in {item.casefold() for item in found}:
                found.append(match)
            if len(found) == 6:
                return found
    return found


def _evidence(values: Iterable[Any], part: str) -> list[EvidenceItem]:
    result = []
    for value in values:
        result.extend(_evidence_value(value, part))
    return _dedupe(result)


def _evidence_value(value: Any, part: str) -> list[EvidenceItem]:
    if value is None:
        return []
    if isinstance(value, EvidenceItem):
        return [_normalize_item(value, part)] if _same_part(value.part, part) else []
    if isinstance(value, (list, tuple, set)):
        result = []
        for item in value:
            result.extend(_evidence_value(item, part))
        return result
    if isinstance(value, QueryResponse):
        return _evidence([*value.facts, *value.constraints], part)
    if isinstance(value, GetResponse):
        return _evidence(([value.entity] if value.entity else []) + value.candidates, part)
    data = _mapping(value)
    if not data:
        return []
    nested = []
    container_seen = False
    for key in ("facts", "items", "results", "evidence", "hits", "entities", "candidates"):
        if key in data:
            container_seen = True
        if isinstance(data.get(key), (list, tuple, set)):
            nested.extend(_evidence_value(data[key], part))
    if "entity" in data:
        container_seen = True
    if isinstance(data.get("entity"), (Mapping, EvidenceItem)):
        nested.extend(_evidence_value(data["entity"], part))
    if nested or container_seen:
        return nested
    raw_part = _part(data.get("part") or data.get("component") or part)
    if raw_part != part:
        return []
    kind = str(data.get("kind") or data.get("entity_kind") or data.get("type") or "prose").lower()
    kind = kind if kind in _KINDS else "prose"
    title = str(data.get("title") or data.get("name") or data.get("register") or data.get("symbol") or data.get("target") or kind)
    text = str(data.get("text") or data.get("description") or data.get("content") or "")
    item_id = str(data.get("id") or data.get("entity_id") or stable_id(part, str(data.get("revision") or ""), kind, title, discriminator=text[:100]))
    raw_sources = data.get("sources") or data.get("source_refs") or data.get("provenance") or []
    if isinstance(raw_sources, (str, Mapping, SourceRef)):
        raw_sources = [raw_sources]
    sources = [item for item in (_source(value, part) for value in raw_sources) if item]
    table = data.get("table")
    if table is not None and not isinstance(table, TableData):
        try:
            table = TableData.model_validate(table)
        except Exception:
            table = None
    try:
        confidence = str(data.get("confidence") or "high").lower()
        confidence = confidence if confidence in {"high", "medium", "low"} else "low"
        sequence = data.get("sequence")
        return [EvidenceItem(
            id=item_id, part=part, kind=kind, title=title, text=text,
            aliases=[str(item) for item in data.get("aliases", []) or []],
            parent_id=str(data.get("parent_id") or data.get("parent") or ""),
            sequence=int(sequence) if sequence is not None else None,
            semantic_type=str(data.get("semantic_type") or data.get("content_type") or "general"),
            values=dict(data.get("values") or {}) if isinstance(data.get("values") or {}, Mapping) else {},
            table=table, sources=sources, confidence=confidence,
            validated=bool(data.get("validated", True)),
            enrichment=str(data.get("enrichment") or "deterministic") if str(data.get("enrichment") or "deterministic") in {"deterministic", "local_ai", "none"} else "none",
        )]
    except Exception:
        return []


def _normalize_item(item: EvidenceItem, part: str) -> EvidenceItem:
    return item.model_copy(update={"part": part, "sources": [source for source in (_source(value, part) for value in item.sources) if source]})


def _source(value: Any, part: str) -> SourceRef | None:
    if isinstance(value, SourceRef):
        return value.model_copy(update={"part": part}) if _same_part(value.part, part) else None
    if isinstance(value, str):
        return SourceRef(source_id=value, part=part)
    data = _mapping(value)
    if not data:
        return None
    source_id = str(data.get("source_id") or data.get("id") or data.get("source") or "")
    if not source_id or not _same_part(data.get("part"), part):
        return None
    try:
        payload = dict(data)
        payload.update(source_id=source_id, part=part)
        return SourceRef.model_validate(payload)
    except Exception:
        return SourceRef(source_id=source_id, part=part)


def _dedupe(items: Iterable[EvidenceItem]) -> list[EvidenceItem]:
    result = []
    by_id = {}
    for item in items:
        if item.id not in by_id:
            by_id[item.id] = item
            result.append(item)
        else:
            current = by_id[item.id]
            merged = current.model_copy(update={
                "sources": _dedupe_sources([*current.sources, *item.sources]),
                "values": {**item.values, **current.values},
                "text": current.text or item.text,
                "aliases": list(dict.fromkeys([*current.aliases, *item.aliases])),
            })
            by_id[item.id] = merged
            result[result.index(current)] = merged
    return result


def _relations(value: Any, part: str) -> list[GraphRelation]:
    if value is None:
        return []
    if isinstance(value, GraphRelation):
        return [value] if _same_part(value.part, part) else []
    if isinstance(value, (list, tuple, set)):
        result = []
        for item in value:
            result.extend(_relations(item, part))
        return result
    data = _mapping(value)
    if not data:
        return []
    for key in ("relations", "related_entities", "edges"):
        if isinstance(data.get(key), (list, tuple, set)):
            return _relations(data[key], part)
    relation = str(data.get("relation") or data.get("type") or data.get("edge_type") or "REFERENCES").upper()
    source_id = str(data.get("source_id") or data.get("source") or "")
    target_id = str(data.get("target_id") or data.get("target") or "")
    if _part(data.get("part") or part) != part or relation not in _RELATIONS or not source_id or not target_id:
        return []
    raw_refs = data.get("source_refs") or data.get("sources") or []
    if isinstance(raw_refs, str):
        raw_refs = [raw_refs]
    try:
        return [GraphRelation(
            id=str(data.get("id") or stable_id(part, "", "relation", f"{relation}:{source_id}:{target_id}")),
            part=part, relation=relation, source_id=source_id, target_id=target_id,
            label=str(data.get("label") or ""), confidence=str(data.get("confidence") or "high"),
            source_refs=[str(item.source_id if isinstance(item, SourceRef) else item) for item in raw_refs],
        )]
    except Exception:
        return []


def _dedupe_relations(items: Iterable[GraphRelation]) -> list[GraphRelation]:
    result = []
    seen = set()
    for item in items:
        if item.id not in seen:
            seen.add(item.id)
            result.append(item)
    return result


def _expand(store: Any, part: str, ids: list[str], *, depth: int = 2) -> list[GraphRelation]:
    ids = list(dict.fromkeys(item for item in ids if item))
    if not ids or depth <= 0:
        return []
    first_items: list[GraphRelation] = []
    for node in ids:
        first_items.extend(store.relations(part, node, depth=1, limit=48))
    first = _dedupe_relations(_relations(first_items, part))
    second_ids = []
    for relation in first:
        if relation.relation not in _PREREQUISITES:
            continue
        for entity_id in (relation.source_id, relation.target_id):
            if entity_id not in ids and entity_id not in second_ids:
                second_ids.append(entity_id)
            if len(second_ids) >= 8:
                break
        if len(second_ids) >= 8:
            break
    second_items: list[GraphRelation] = []
    if depth > 1:
        for node in second_ids:
            second_items.extend(store.relations(part, node, depth=1, limit=16))
    second = _dedupe_relations(_relations(second_items, part))
    return _dedupe_relations([*first, *second])


def _metadata(values: Iterable[Any], part: str) -> dict[str, Any]:
    gaps, conflicts, sources = [], [], []
    coverage = None
    next_cursor = ""
    for value in values:
        data = _mapping(value)
        if not data:
            continue
        gaps.extend(_strings(data.get("gaps", []))); gaps.extend(_strings(data.get("warnings", [])))
        conflicts.extend(_strings(data.get("conflicts", [])))
        next_cursor = str(data.get("next_cursor") or next_cursor)
        coverage = _coverage(data.get("coverage")) or coverage
        raw_sources = data.get("sources") or []
        if isinstance(raw_sources, (str, Mapping, SourceRef)):
            raw_sources = [raw_sources]
        sources.extend(item for item in (_source(value, part) for value in raw_sources) if item)
    return {"gaps": _unique(gaps), "conflicts": _unique(conflicts), "sources": _dedupe_sources(sources), "coverage": coverage, "next_cursor": next_cursor}


def _get_parts(value: Any, part: str):
    metadata = _metadata([value], part)
    relations = _relations(value, part)
    if isinstance(value, GetResponse):
        return (_evidence([value.entity], part)[0] if value.entity else None, _evidence(value.candidates, part), _dedupe_relations([*relations, *value.related_entities]), {**metadata, "gaps": [*metadata["gaps"], *value.gaps], "next_cursor": value.next_cursor})
    data = _mapping(value) or {}
    entity_items = _evidence([data.get("entity")], part) if data.get("entity") is not None else []
    candidates = _evidence([data.get("candidates", [])], part)
    if not entity_items:
        all_items = _evidence([value], part)
        if len(all_items) == 1:
            entity_items = all_items
        elif all_items:
            candidates = _dedupe([*candidates, *all_items])
    entity = entity_items[0] if entity_items else None
    return entity, [item for item in candidates if not entity or item.id != entity.id], relations, metadata


def _flatten(values: Mapping[str, Any], prefix: str = "") -> list[tuple[str, Any, str, str]]:
    result = []
    for key, value in values.items():
        name = f"{prefix}.{key}" if prefix else str(key)
        if isinstance(value, Mapping):
            if "value" in value:
                result.append((name, value.get("value"), str(value.get("unit") or ""), str(value.get("condition") or "")))
            else:
                result.extend(_flatten(value, name))
        else:
            result.append((name, value, "", ""))
    return result


def _setting_name(key: str) -> str:
    raw = re.sub(r"\[[^]]+\]", "", key.split(".")[-1]).lower().replace("-", "_").replace(" ", "_")
    if not any(word in raw for word in ("cpol", "cpha", "spi", "mode", "dummy", "cycle", "freq", "clock", "opcode", "command", "address", "latency", "timing", "access", "reset", "default", "width", "length", "wait", "voltage", "size")):
        return ""
    return {
        "cpol": "CPOL", "cpha": "CPHA", "spi_mode": "SPI mode", "dummy_cycle": "Dummy cycles", "dummy_cycles": "Dummy cycles",
        "frequency": "Frequency", "freq": "Frequency", "clock_frequency": "Clock frequency", "max_frequency": "Maximum frequency",
        "opcode": "Opcode", "command": "Command", "address_length": "Address length", "access": "Access", "reset": "Reset/default",
        "default": "Reset/default", "mode": "Mode", "latency": "Latency", "clock": "Clock", "data_width": "Data width", "width": "Width",
    }.get(raw, raw.replace("_", " ").title())


def _settings(items: list[EvidenceItem]):
    result, conflicts, values_by_scope = [], [], {}
    for item in items:
        row_conditions = []
        for raw_key, raw_value in item.values.items():
            key_text = str(raw_key)
            if re.search(r"\b(?:dc|latency|mode)\s*(?:\[|code|bits?)", key_text, re.I):
                if not isinstance(raw_value, Mapping) and str(raw_value).strip():
                    row_conditions.append(f"{key_text}={raw_value}")
        for key, value, unit, condition in _flatten(item.values):
            name = _setting_name(key)
            if not name:
                continue
            if row_conditions and name in {"Dummy cycles", "Frequency", "Clock frequency", "Maximum frequency"}:
                condition = "; ".join([*row_conditions, condition] if condition else row_conditions)
            setting = NormalizedSetting(name=name, value=value, unit=unit, condition=condition, source_ids=[source.source_id for source in item.sources])
            identity = (name.casefold(), repr(value), unit, condition)
            if any((existing.name.casefold(), repr(existing.value), existing.unit, existing.condition) == identity for existing in result):
                continue
            result.append(setting); values_by_scope.setdefault((name, condition), []).append(setting)
        combined_text = f"{item.title}\n{item.text}"
        mode_match = re.search(r"\b(?:SPI(?:\s+interface)?|Serial\s+Peripheral\s+Interface)\s*(?:--|[-:]|is)?\s*Mode\s*([0-3])\b", combined_text, re.I)
        if mode_match:
            mode = int(mode_match.group(1))
            source_ids = [source.source_id for source in item.sources]
            derived = (
                NormalizedSetting(name="SPI mode", value=mode, source_ids=source_ids),
                NormalizedSetting(name="CPOL", value=1 if mode in {2, 3} else 0, condition=f"SPI Mode {mode}", source_ids=source_ids),
                NormalizedSetting(name="CPHA", value=1 if mode in {1, 3} else 0, condition=f"SPI Mode {mode}", source_ids=source_ids),
            )
            for setting in derived:
                identity = (setting.name.casefold(), repr(setting.value), setting.unit, setting.condition)
                if not any((existing.name.casefold(), repr(existing.value), existing.unit, existing.condition) == identity for existing in result):
                    result.append(setting)
                    values_by_scope.setdefault((setting.name, setting.condition), []).append(setting)
    for (name, condition), values in values_by_scope.items():
        if len({repr(item.value) for item in values}) > 1:
            scope = f" under {condition}" if condition else ""
            conflicts.append(f"Conflicting evidence for {values[0].name}{scope}: " + "; ".join(repr(item.value) for item in values))
    return result, conflicts


def _steps(items: list[EvidenceItem]) -> list[ActionStep]:
    rows = []
    flowchart_added = False
    for index, item in enumerate(items):
        semantic = f"{item.kind} {item.semantic_type} {item.title} {item.text}".lower()
        flow = str(item.values.get("alt") or "") if isinstance(item.values, Mapping) else ""
        if "flowchart" in flow.casefold():
            if flowchart_added:
                continue
            flowchart_added = True
            labels = re.split(r"\s*(?:,|→|->)\s*", flow.split("--", 1)[-1])
            labels = [label.strip(" .") for label in labels if label.strip(" .")]
            for flow_index, label in enumerate(labels):
                upper = label.upper()
                action = label
                verification = ""
                prerequisites = []
                if upper == "WREN":
                    action = "Issue Write Enable (WREN)"
                elif upper == "WEL":
                    action = "Verify Write Enable Latch (WEL) is set"
                    verification = "Read status and confirm WEL=1"
                elif upper == "WIP":
                    action = "Poll Write In Progress (WIP)"
                    verification = "Continue only after WIP clears"
                elif upper == "RDSR":
                    action = "Read Status Register (RDSR)"
                elif upper == "RDSCUR":
                    action = "Read Security Register (RDSCUR)"
                elif "P_FAIL" in upper or "E_FAIL" in upper:
                    action = "Check P_FAIL/E_FAIL status"
                    verification = "Confirm the failure flag is clear"
                elif "PROGRAM" in upper or "ERASE" in upper:
                    action = "Issue the selected Program or Erase command"
                    prerequisites = ["WEL must be set"]
                rows.append((flow_index + 1, index * 1000 + flow_index, ActionStep(
                    order=flow_index + 1,
                    action=action,
                    details=f"Source flow label: {label}",
                    prerequisites=prerequisites,
                    verification=verification,
                    source_ids=[source.source_id for source in item.sources],
                )))
            continue
        if not (item.kind in {"operation", "step", "command"} or any(word in semantic for word in ("operation", "sequence", "flow", "procedure")) or isinstance(item.values.get("steps"), list)):
            continue
        nested = item.values.get("steps") if isinstance(item.values, Mapping) else None
        if isinstance(nested, list):
            for nested_index, value in enumerate(nested):
                data = _mapping(value) or {"action": str(value)}
                order = _number(data.get("order") or data.get("sequence"), (item.sequence or 0) + nested_index + 1)
                rows.append((order, index * 1000 + nested_index, _step(data, item)))
        else:
            order = _number(item.values.get("order"), item.sequence or index + 1)
            rows.append((order, index, _step(item.values, item)))
    rows.sort(key=lambda row: (row[0], row[1]))
    return [step.model_copy(update={"order": order}) for order, _, step in rows]


def _step(data: Mapping[str, Any], item: EvidenceItem) -> ActionStep:
    prerequisites = data.get("prerequisites") or data.get("requires") or []
    if isinstance(prerequisites, str):
        prerequisites = [prerequisites]
    return ActionStep(
        order=0,
        action=str(data.get("action") or data.get("operation") or data.get("command") or item.title),
        details=str(data.get("details") or data.get("description") or item.text),
        prerequisites=[str(value) for value in prerequisites],
        verification=str(data.get("verification") or data.get("verify") or ""),
        source_ids=[source.source_id for source in item.sources],
    )


def _constraint(item: EvidenceItem) -> bool:
    text = f"{item.kind} {item.semantic_type} {item.title}".lower()
    return item.kind in {"constraint", "warning"} or any(word in text for word in ("constraint", "warning", "restriction", "limit")) or any(word in str(key).lower() for key in item.values for word in ("condition", "restriction", "prohibited", "minimum", "maximum"))


def _gaps(question: str, items: list[EvidenceItem], settings: list[NormalizedSetting], steps: list[ActionStep], inherited: list[str]) -> list[str]:
    gaps = list(inherited); text = question.lower(); names = {setting.name.lower() for setting in settings}
    if not items:
        return [*gaps, "No supporting evidence was indexed for this question."]
    if any(not item.sources for item in items):
        gaps.append("Some returned evidence has no source anchor.")
    if any(not item.validated or item.confidence == "low" for item in items):
        gaps.append("Some returned evidence is incomplete or low confidence.")
    if any(term in text for term in ("spi mode", "cpol", "cpha")) and not ({"cpol", "cpha"} <= names or "spi mode" in names):
        gaps.append("SPI mode evidence does not fully specify CPOL/CPHA or an equivalent mode setting.")
    if "dummy" in text and not any("dummy" in name or "cycle" in name for name in names):
        gaps.append("Dummy-cycle evidence is missing a normalized cycle setting.")
    if "frequency" in text and not any("frequency" in name or "clock" in name for name in names):
        gaps.append("Frequency evidence is not normalized from the indexed values.")
    if any(term in text for term in ("program", "erase")) and not steps:
        gaps.append("Program/erase evidence does not contain ordered operation steps.")
    if any(term in text for term in ("without read array", "no read array", "blocked array")) and not any("read" in f"{item.title} {item.text}".lower() for item in items):
        gaps.append("The evidence does not state the array-read restriction for this flow.")
    return gaps


def _sources(part: str, items: list[EvidenceItem], settings: list[NormalizedSetting], steps: list[ActionStep], relations: list[GraphRelation], inherited: list[SourceRef]) -> list[SourceRef]:
    result = list(inherited) + [source for item in items for source in item.sources]
    ids = {source.source_id for source in result}
    for source_id in [source_id for setting in settings for source_id in setting.source_ids] + [source_id for step in steps for source_id in step.source_ids] + [source_id for relation in relations for source_id in relation.source_refs]:
        if source_id not in ids:
            result.append(SourceRef(source_id=source_id, part=part)); ids.add(source_id)
    return _dedupe_sources(result)


def _confidence(items: list[EvidenceItem], sources: list[SourceRef], gaps: list[str], conflicts: list[str]) -> str:
    if not items or not sources or conflicts:
        return "low"
    return "medium" if gaps or any(item.confidence != "high" or not item.validated for item in items) else "high"


def _summary(part: str, question: str, focus: str, items: list[EvidenceItem], settings: list[NormalizedSetting], steps: list[ActionStep]) -> str:
    values = ", ".join(f"{setting.name}={setting.value}" for setting in settings[:4])
    base = f"{part}: {focus} evidence for {question}. {len(items)} evidence item(s), {len(steps)} ordered step(s)."
    return f"{base} Settings: {values}." if values else base


def _truncate(response: QueryResponse, budget: int) -> QueryResponse:
    def tokens(value: QueryResponse) -> int:
        return len(value.model_dump_json()) // 4
    if tokens(response) <= budget:
        return response
    result = response.model_copy(deep=True, update={"truncated": True})
    while tokens(result) > budget and len(result.facts) > 1:
        result.facts.pop()
    while tokens(result) > budget and len(result.related_entities) > 1:
        result.related_entities.pop()
    while tokens(result) > budget and len(result.constraints) > 1:
        result.constraints.pop()
    while tokens(result) > budget and len(result.steps) > 1:
        result.steps.pop()
    return result.model_copy(update={
        "summary": result.summary[:640],
        "facts": [item.model_copy(update={"text": item.text[:320], "table": None}) for item in result.facts],
        "constraints": [item.model_copy(update={"text": item.text[:240], "table": None}) for item in result.constraints],
        "steps": [item.model_copy(update={"details": item.details[:240]}) for item in result.steps],
    })


def _truncate_get(response: GetResponse, budget: int) -> GetResponse:
    if len(response.model_dump_json()) // 4 <= budget:
        return response
    result = response.model_copy(deep=True, update={"candidates": response.candidates[:8], "related_entities": response.related_entities[:16], "summary": response.summary[:640]})
    if result.entity:
        result.entity = result.entity.model_copy(update={"text": result.entity.text[:320], "table": None})
    return result


def _get_summary(target: str, entity: EvidenceItem | None, candidates: list[EvidenceItem]) -> str:
    if entity:
        return f"Found {entity.kind} '{entity.title}' for '{target}'."
    if candidates:
        return f"Target '{target}' is ambiguous; {len(candidates)} candidate(s) returned."
    return f"No indexed evidence matched '{target}'."


def _coverage(value: Any) -> CoverageReport | None:
    if value is None:
        return None
    if isinstance(value, CoverageReport):
        return value
    try:
        return CoverageReport.model_validate(value)
    except Exception:
        return None


def _dedupe_sources(items: Iterable[SourceRef]) -> list[SourceRef]:
    result = []; seen = set()
    for item in items:
        if item.source_id and item.source_id not in seen:
            seen.add(item.source_id); result.append(item)
    return result


def _number(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return int(default)


def _strings(values: Any) -> list[str]:
    if isinstance(values, str):
        return [values]
    return [str(value) for value in values or []]


def _unique(values: Iterable[Any]) -> list[str]:
    result = []; seen = set()
    for value in values:
        value = str(value).strip()
        if value and value not in seen:
            seen.add(value); result.append(value)
    return result


__all__ = ["DatasheetService"]
