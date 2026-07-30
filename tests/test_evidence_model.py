from __future__ import annotations

import pytest

from ds.evidence.enrich import LocalAIClient, enrich_artifact
from ds.evidence.model import (
    CatalogPart,
    CorpusArtifact,
    CoverageReport,
    EvidenceItem,
    IndexManifest,
    SourceRef,
    stable_id,
)


def _artifact() -> CorpusArtifact:
    source = SourceRef(
        source_id="src-1", part="FLASH1", revision="1.0",
        heading="SPI mode", text_span="Supports Serial Peripheral Interface Mode 0",
    )
    evidence = EvidenceItem(
        id="evidence-1", part="FLASH1", kind="prose", title="SPI mode",
        text="Supports Serial Peripheral Interface Mode 0", sources=[source],
    )
    coverage = CoverageReport(part="FLASH1", revision="1.0")
    return CorpusArtifact(
        manifest=IndexManifest(part="FLASH1", revision="1.0"),
        catalog=CatalogPart(part="FLASH1", revision="1.0", coverage=coverage),
        coverage=coverage,
        evidence=[evidence],
    )


def test_stable_id_is_deterministic_and_part_scoped():
    first = stable_id("flash1", "1.0", "table", "SPI commands")
    second = stable_id("FLASH1", "1.0", "table", "SPI commands")
    other = stable_id("flash2", "1.0", "table", "SPI commands")
    assert first == second
    assert first.startswith("ds://FLASH1/1-0/table/")
    assert first != other


def test_evidence_embed_text_keeps_aliases_values_and_source_text():
    item = EvidenceItem(
        id="x", part="P", kind="parameter", title="Clock frequency",
        aliases=["fSCLK"], semantic_type="timing",
        values={"max": 133, "unit": "MHz"}, text="SPI command clock",
    )
    rendered = item.embed_text()
    assert "fSCLK" in rendered
    assert "max: 133" in rendered
    assert "SPI command clock" in rendered


def test_local_ai_rejects_cloud_url():
    with pytest.raises(ValueError, match="local"):
        LocalAIClient("https://api.example.com/v1", "model")


def test_enrichment_accepts_literal_source_anchored_fact(tmp_path):
    class FakeClient:
        enabled = True
        model = "local-test"

        def extract(self, evidence):
            return {"entities": [{
                "kind": "mode",
                "title": "SPI Mode 0",
                "quote": "Serial Peripheral Interface Mode 0",
                "text": "The device supports SPI Mode 0.",
                "semantic_type": "mode",
                "aliases": ["CPOL=0 CPHA=0"],
                "values": {"mode": "Mode 0"},
            }], "relations": []}

    artifact = enrich_artifact(
        _artifact(), client=FakeClient(), cache_path=tmp_path / "cache.json"
    )
    modes = [item for item in artifact.evidence if item.kind == "mode"]
    assert len(modes) == 1
    assert modes[0].enrichment == "local_ai"
    assert artifact.coverage.local_ai_used
    assert any(rel.relation == "EVIDENCED_BY" for rel in artifact.relations)


def test_enrichment_rejects_unanchored_value():
    class FakeClient:
        enabled = True
        model = "local-test"

        def extract(self, evidence):
            return {"entities": [{
                "kind": "parameter", "title": "Clock", "quote": "Mode 0",
                "text": "Clock is 200 MHz", "values": {"max": 200},
            }]}

    artifact = enrich_artifact(_artifact(), client=FakeClient())
    assert not [item for item in artifact.evidence if item.kind == "parameter"]
