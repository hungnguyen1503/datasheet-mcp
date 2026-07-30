from __future__ import annotations

from dataclasses import dataclass, field

from ds.evidence.model import EvidenceItem, GraphRelation, SourceRef
from ds.evidence.service import DatasheetService

PART = "MX25LM51245G"


def src(name: str, part: str = PART) -> SourceRef:
    return SourceRef(source_id=name, part=part, section_path=["Flash", name])


def item(item_id: str, title: str, *, kind: str = "prose", semantic_type: str = "general", values=None, text="", sources=None, sequence=None, confidence="high", part=PART):
    return EvidenceItem(
        id=item_id,
        part=part,
        kind=kind,
        title=title,
        semantic_type=semantic_type,
        values=values or {},
        text=text,
        sources=[src(f"source-{item_id}", part)] if sources is None else sources,
        sequence=sequence,
        confidence=confidence,
    )


@dataclass
class FakeStore:
    search_items: list[EvidenceItem] = field(default_factory=list)
    exact: dict[str, object] = field(default_factory=dict)
    catalog_value: object = field(default_factory=dict)
    relation_items: list[GraphRelation] = field(default_factory=list)
    calls: list[tuple] = field(default_factory=list)

    def catalog(self, part, *, query="", cursor="", limit=100):
        self.calls.append(("catalog", part, query, cursor, limit))
        return self.catalog_value

    def search(self, part, *, query, focus=None, limit=5, **kwargs):
        self.calls.append(("search", part, query, focus, limit))
        return self.search_items

    def get(self, part, target, *, kind="auto", include_related=True, depth=1):
        self.calls.append(("get", part, target, kind, include_related, depth))
        return self.exact.get(target, {"candidates": []})

    def relations(self, part, node, *, depth=1, limit=100, **kwargs):
        self.calls.append(("relations", part, node, depth, limit))
        relation_calls = [call for call in self.calls if call[0] == "relations"]
        return self.relation_items if len(relation_calls) == 1 else []

    def prewarm(self):
        self.calls.append(("prewarm",))
        return {"evidence": "ready"}


def test_canonical_three_tool_surface_returns_structured_models():
    store = FakeStore(
        catalog_value={
            "summary": "indexed",
            "parts": [{"part": PART, "vendor": "Macronix", "revision": "RevA"}],
            "outline": [{"id": "spi", "kind": "section", "title": "SPI configuration"}],
            "next_cursor": "next",
        },
        exact={"REG": {"entity": item("reg", "REG", kind="register")}},
    )
    service = DatasheetService(store)

    catalog = service.catalog(PART, cursor="cursor", limit=50)
    found = service.get(PART, "REG", relation_depth=0, limit=50)

    assert catalog.parts[0].part == PART
    assert catalog.outline[0].title == "SPI configuration"
    assert catalog.next_cursor == "next"
    assert found.entity is not None and found.entity.title == "REG"
    assert found.sources[0].source_id == "source-reg"


def test_query_combines_exact_hybrid_evidence_and_bounded_prerequisite_expansion():
    cpol = item("cpol", "CPOL", kind="bitfield", values={"cpol": 0})
    spi = item("spi", "SPI mode", kind="mode", values={"spi_mode": 0, "cpha": 0, "frequency": {"value": 133, "unit": "MHz"}})
    relation = GraphRelation(id="requires-wren", part=PART, relation="REQUIRES", source_id="spi", target_id="wren", source_refs=["relation-source"])
    store = FakeStore(search_items=[spi], exact={"CPOL": {"entity": cpol}}, relation_items=[relation])

    response = DatasheetService(store).query(PART, "How do I configure SPI mode CPOL and CPHA?")

    assert response.focus == "configure"
    assert {fact.id for fact in response.facts} == {"cpol", "spi"}
    assert {setting.name for setting in response.normalized_configuration} >= {"CPOL", "CPHA", "SPI mode", "Frequency"}
    assert response.related_entities[0].relation == "REQUIRES"
    assert "relation-source" in {source.source_id for source in response.sources}
    assert response.confidence == "high"
    assert any(call[0] == "search" and call[3] == "configure" for call in store.calls)


def test_get_reports_ambiguity_and_excludes_other_parts():
    store = FakeStore(exact={"STATUS": {"candidates": [
        item("status-a", "STATUS", kind="register"),
        item("status-b", "STATUS", kind="register"),
        item("other", "STATUS", kind="register", part="OTHER-MCU"),
    ]}})

    response = DatasheetService(store).get(PART, "STATUS", relation_depth=0)

    assert response.entity is None
    assert [candidate.id for candidate in response.candidates] == ["status-a", "status-b"]
    assert "ambiguous" in response.summary.lower()
    assert all(candidate.part == PART for candidate in response.candidates)


def test_incomplete_evidence_reports_gaps_and_low_confidence():
    incomplete = item("timing", "Dummy cycle timing", semantic_type="timing", values={}, sources=[], confidence="low")
    store = FakeStore(search_items=[incomplete, item("leak", "Other MCU", part="OTHER-MCU")])

    response = DatasheetService(store).query(PART, "How do I set dummy cycles and frequency?")

    assert [fact.id for fact in response.facts] == ["timing"]
    assert response.confidence == "low"
    assert any("dummy-cycle" in gap.lower() for gap in response.gaps)
    assert any("frequency" in gap.lower() for gap in response.gaps)


def test_dummy_cycle_and_frequency_are_normalized_from_values():
    timing = item("dummy", "Read latency", kind="parameter", semantic_type="timing", values={"dummy_cycles": 8, "frequency": {"value": 133, "unit": "MHz"}})
    response = DatasheetService(FakeStore(search_items=[timing])).query(PART, "How to setup Dummy Cycle and Frequency?")

    values = {setting.name: setting.value for setting in response.normalized_configuration}
    assert values["Dummy cycles"] == 8
    assert values["Frequency"] == 133
    assert next(setting for setting in response.normalized_configuration if setting.name == "Frequency").unit == "MHz"
    assert response.focus == "timing"
    assert response.gaps == []


def test_spi_mode_text_normalizes_controller_cpol_and_cpha():
    mode = item(
        "spi-mode", "Serial interface", kind="prose",
        text="Supports Serial Peripheral Interface -- Mode 0",
    )
    response = DatasheetService(FakeStore(search_items=[mode])).query(
        PART, "How to configure SPI mode?", focus="configure")

    values = {setting.name: setting.value for setting in response.normalized_configuration}
    assert values == {"SPI mode": 0, "CPOL": 0, "CPHA": 0}
    assert all(setting.source_ids == ["source-spi-mode"] for setting in response.normalized_configuration)


def test_program_erase_flow_is_ordered_and_preserves_read_array_constraint():
    flow = item(
        "flow", "Program and erase sequence", kind="operation", semantic_type="program_erase_flow",
        values={"steps": [
            {"order": 2, "action": "Program or erase", "verification": "Poll WIP"},
            {"order": 1, "action": "Write enable", "details": "Set WEL"},
        ]},
        text="Program and erase commands operate without read array data while WIP is active.",
    )
    restriction = item("restriction", "Read array restriction", kind="constraint", values={"condition": "Array read is not allowed during program or erase"})
    response = DatasheetService(FakeStore(search_items=[flow, restriction])).query(PART, "Program/Erase flow without read array data")

    assert response.focus == "operation"
    assert [step.order for step in response.steps] == [1, 2]
    assert response.steps[0].action == "Write enable"
    assert response.constraints[0].id == "restriction"
    assert response.gaps == []
    assert response.confidence == "high"


def test_flowchart_figure_becomes_ordered_source_linked_steps():
    figure = item(
        "flow-figure",
        "Program/Erase flow without read array data",
        kind="figure",
        values={"alt": "flowchart -- WREN, RDSR, WEL, Program/erase, WIP, RDSCUR, P_FAIL/E_FAIL"},
    )
    response = DatasheetService(FakeStore(search_items=[figure])).query(
        PART, "Program/Erase flow without read array data", focus="operation")

    assert [step.order for step in response.steps] == list(range(1, 8))
    assert response.steps[0].action == "Issue Write Enable (WREN)"
    assert "WEL=1" in response.steps[2].verification
    assert "WIP clears" in response.steps[4].verification
    assert all(step.source_ids == ["source-flow-figure"] for step in response.steps)


def test_sources_are_deduplicated_and_all_references_are_valid():
    shared = src("shared")
    first = item("one", "One", values={"mode": 0}, sources=[shared])
    duplicate = item("one", "Duplicate", values={"mode": 0}, sources=[shared])
    second = item("two", "Two", values={"frequency": {"value": 50, "unit": "MHz"}}, sources=[shared])
    response = DatasheetService(FakeStore(search_items=[first, duplicate, second])).query(PART, "configure mode and frequency")

    assert [fact.id for fact in response.facts] == ["one", "two"]
    assert [source.source_id for source in response.sources] == ["shared"]
    valid = {source.source_id for source in response.sources}
    assert all(source_id in valid for setting in response.normalized_configuration for source_id in setting.source_ids)
    assert all(source.source_id in valid for fact in response.facts for source in fact.sources)


def test_token_budget_is_clamped_and_large_packet_is_marked_truncated():
    items = [item(f"item-{index}", f"Evidence {index}", text="x" * 3000, values={"mode": index}) for index in range(20)]
    store = FakeStore(search_items=items)
    response = DatasheetService(store).query(PART, "explain flash configuration", max_tokens=1)

    assert response.truncated is True
    assert len(response.facts) < len(items)
    assert next(call for call in store.calls if call[0] == "search")[4] == 32


def test_part_scope_and_optional_store_are_explicit():
    try:
        DatasheetService().query("", "anything")
    except ValueError as error:
        assert "part" in str(error)
    else:
        raise AssertionError("missing store should be diagnosed")
    try:
        DatasheetService(FakeStore()).get("", "REG")
    except ValueError as error:
        assert "part" in str(error)
    else:
        raise AssertionError("part-scoped get must reject an empty part")


def test_prewarm_is_forwarded_and_default_store_creation_is_lazy():
    store = FakeStore()
    service = DatasheetService(store)

    assert service.prewarm() == {"evidence": "ready"}
    assert ("prewarm",) in store.calls
    assert DatasheetService().store is None
