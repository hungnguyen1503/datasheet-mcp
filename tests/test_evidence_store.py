"""Offline contract tests for the canonical evidence Qdrant store."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from ds.evidence.model import (
    CatalogPart,
    CorpusArtifact,
    CoverageReport,
    EvidenceItem,
    GraphRelation,
    IndexManifest,
    stable_id,
)
from ds.evidence.store_qdrant import EvidenceStoreQdrant, _point_id


class FakeEmbedder:
    dim = 3

    def embed_documents(self, texts):
        return [[1.0, 0.0, 0.0] for _ in texts]

    def embed_query(self, text):
        return [1.0, 0.0, 0.0]


class FakeSparseEncoder:
    def embed(self, texts):
        return [{"indices": [7], "values": [1.0]} for _ in texts]


class FakeClient:
    """Small Qdrant-shaped fake; it intentionally keeps all points in memory."""

    def __init__(self, *, existing=None):
        self.collections = {}
        self.points = {}
        self.indexes = []
        for name, info in (existing or {}).items():
            self.collections[name] = info
            self.points[name] = []

    def get_collections(self):
        return SimpleNamespace(
            collections=[SimpleNamespace(name=name) for name in self.collections]
        )

    def create_collection(self, collection_name, vectors_config, sparse_vectors_config=None):
        self.collections[collection_name] = SimpleNamespace(
            config=SimpleNamespace(
                params=SimpleNamespace(
                    vectors=vectors_config,
                    sparse_vectors=sparse_vectors_config,
                )
            ),
            points_count=0,
        )
        self.points.setdefault(collection_name, [])

    def get_collection(self, collection_name):
        info = self.collections[collection_name]
        info.points_count = len(self.points.get(collection_name, []))
        return info

    def create_payload_index(self, collection_name, field_name, field_schema):
        self.indexes.append((collection_name, field_name, field_schema))

    @staticmethod
    def _condition_matches(payload, condition):
        expected = condition.match.value
        actual = payload.get(condition.key)
        if isinstance(actual, list):
            return expected in actual
        return actual == expected

    @classmethod
    def _matches(cls, payload, query_filter):
        if query_filter is None:
            return True
        if any(not cls._condition_matches(payload, condition) for condition in (query_filter.must or [])):
            return False
        should = query_filter.should or []
        return not should or any(cls._condition_matches(payload, condition) for condition in should)

    def delete(self, collection_name, points_selector):
        self.points[collection_name] = [
            point
            for point in self.points.get(collection_name, [])
            if not self._matches(point.payload, points_selector)
        ]

    def upsert(self, collection_name, points):
        existing = {str(point.id): point for point in self.points.setdefault(collection_name, [])}
        existing.update({str(point.id): point for point in points})
        self.points[collection_name] = list(existing.values())

    def scroll(self, collection_name, scroll_filter=None, limit=10000, **kwargs):
        rows = [
            point
            for point in self.points.get(collection_name, [])
            if self._matches(point.payload, scroll_filter)
        ]
        return rows[:limit], None

    def query_points(self, collection_name, query_filter=None, limit=10, **kwargs):
        rows = [
            point
            for point in self.points.get(collection_name, [])
            if self._matches(point.payload, query_filter)
        ]
        return SimpleNamespace(points=rows[:limit])


def _artifact(part, *, titles=("FLASH",), relations=()):
    revision = "Rev-A"
    items = []
    for index, title in enumerate(titles):
        item_id = stable_id(part, revision, "register", title, discriminator=str(index))
        items.append(
            EvidenceItem(
                id=item_id,
                part=part,
                kind="register",
                title=title,
                aliases=[title.lower(), f"{title}_ALIAS"],
                text=f"{title} controls flash operation timing and mode.",
                values={"reset": 0},
            )
        )
    graph = [
        GraphRelation(
            id=stable_id(part, revision, "relation", f"{source}-{target}"),
            part=part,
            relation="REQUIRES",
            source_id=source,
            target_id=target,
        )
        for source, target in relations
    ]
    coverage = CoverageReport(part=part)
    return CorpusArtifact(
        manifest=IndexManifest(part=part, revision=revision, embed_dimension=3),
        catalog=CatalogPart(part=part, vendor="Vendor", title=f"{part} flash"),
        coverage=coverage,
        evidence=items,
        relations=graph,
    )


def _store(client=None):
    return EvidenceStoreQdrant(
        client=client or FakeClient(),
        embedder=FakeEmbedder(),
        sparse_encoder=FakeSparseEncoder(),
        prefix="nightly_",
    )


def test_collection_names_are_canonical_and_point_ids_are_deterministic():
    store = _store()

    assert store.collection_names() == {
        "catalog": "nightly_ds_catalog",
        "evidence": "nightly_ds_evidence",
        "graph": "nightly_ds_graph",
    }
    assert _point_id("ds://MCU/rev/register/flash-abc") == _point_id(
        "ds://MCU/rev/register/flash-abc"
    )
    assert _point_id("ds://MCU/rev/register/flash-abc") != _point_id(
        "ds://MCU/rev/register/flash-def"
    )


def test_default_embed_contract_is_canonical_768_dimensions(monkeypatch):
    monkeypatch.delenv("DS_EMBED_MODEL", raising=False)
    store = EvidenceStoreQdrant(client=FakeClient(), prefix="")

    assert store.embed_model == "BAAI/bge-base-en-v1.5"
    assert store._dimension == 768


def test_upsert_creates_named_hybrid_evidence_and_catalog_outline():
    client = FakeClient()
    store = _store(client)
    artifact = _artifact("MCU_A")

    count = store.upsert_artifact(artifact)

    assert count == 2
    evidence = client.points["nightly_ds_evidence"]
    assert len(evidence) == 1
    assert set(evidence[0].vector) == {"dense", "sparse"}
    assert any(
        name == "nightly_ds_evidence" and field == "kind"
        for name, field, _schema in client.indexes
    )
    catalog = store.catalog("MCU_A")
    assert [part.part for part in catalog.parts] == ["MCU_A"]
    assert catalog.coverage is not None


def test_search_get_and_relations_are_strictly_part_scoped():
    client = FakeClient()
    store = _store(client)
    a = _artifact("MCU_A", relations=(("A", "B"),))
    b = _artifact("MCU_B", relations=(("A", "B"),))
    store.upsert_artifact(a)
    store.upsert_artifact(b)

    search = store.search("MCU_A", "flash", limit=10)
    assert search and {item.part for item in search} == {"MCU_A"}
    assert not store.get("MCU_A", "MCU_B flash").entity
    assert {edge.part for edge in store.relations("MCU_A", "A", relation="REQUIRES")} == {"MCU_A"}

    # Relation IDs are stable, but these node names are not canonical IDs; this
    # assertion exercises the same-part relation lookup without leakage.
    relation = a.relations[0]
    assert store.relations("MCU_A", relation.source_id)[0].part == "MCU_A"


def test_exact_lookup_returns_candidates_when_name_is_ambiguous():
    client = FakeClient()
    store = _store(client)
    store.upsert_artifact(_artifact("MCU_A", titles=("STATUS", "STATUS")))

    result = store.get("MCU_A", "status")

    assert result.entity is None
    assert len(result.candidates) == 2
    assert all(candidate.part == "MCU_A" for candidate in result.candidates)


def test_manifest_dimension_mismatch_is_rejected_before_part_clear():
    client = FakeClient()
    store = _store(client)
    store.upsert_artifact(_artifact("MCU_A"))
    evidence_name = store.collection_names()["evidence"]
    info = client.collections[evidence_name]
    info.config.params.vectors["dense"].size = 4
    store._ready = False

    with pytest.raises(RuntimeError, match="schema mismatch"):
        store.prewarm()

    # Existing data remains present because schema validation happens before
    # any selected-part deletion.
    assert len(client.points[evidence_name]) == 1


def test_manifest_dimension_guard_uses_canonical_embedder_setting():
    store = _store()
    artifact = _artifact("MCU_A")
    artifact.manifest.embed_dimension = 768

    with pytest.raises(ValueError, match="does not match"):
        store.upsert_artifact(artifact)
