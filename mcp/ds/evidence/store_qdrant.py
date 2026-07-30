"""Qdrant storage for the canonical evidence-first datasheet corpus."""

from __future__ import annotations

import json
import os
import re
import uuid
from typing import Any, Iterable, Literal

from .model import (
    CatalogNode,
    CatalogPart,
    CatalogResponse,
    CorpusArtifact,
    CoverageReport,
    EntityKind,
    EvidenceItem,
    FocusKind,
    GraphRelation,
    GetResponse,
    SourceRef,
    stable_id,
)


DEFAULT_EMBED_MODEL = "BAAI/bge-base-en-v1.5"
DEFAULT_EMBED_DIMENSION = 768
SCHEMA_VERSION = "evidence"
_BATCH = 256
_MAX_GRAPH_DEPTH = 3

_STRUCTURAL_KINDS = {"document", "chapter", "section", "table", "figure"}
_FOCUS_KINDS: dict[str, set[str]] = {
    "configure": {
        "register", "bitfield", "command", "mode", "operation", "step",
        "parameter", "constraint", "memory_region",
    },
    "exact": {
        "register", "bitfield", "pin", "command", "parameter", "memory_region",
    },
    "operation": {"operation", "step", "command", "mode", "constraint"},
    "timing": {"parameter", "table", "table_row", "constraint", "operation"},
    "explain": set(EntityKind.__args__) if hasattr(EntityKind, "__args__") else set(),
}


def _collection_names(prefix: str) -> dict[str, str]:
    return {
        "catalog": f"{prefix}ds_catalog",
        "evidence": f"{prefix}ds_evidence",
        "graph": f"{prefix}ds_graph",
    }


def _runtime_prefix() -> str:
    try:
        from ..collections import get_prefix

        return get_prefix()
    except ImportError:
        return os.environ.get("DS_COLLECTION_PREFIX", "")


def _normalise(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "").strip()).casefold()


def _upper_part(part: str) -> str:
    value = str(part or "").strip()
    if not value:
        raise ValueError("part is required")
    return value.upper()


def _point_id(stable_identifier: str) -> str:
    """Map a stable identifier to a deterministic Qdrant UUID."""
    return str(uuid.uuid5(uuid.NAMESPACE_URL, str(stable_identifier)))


def _json_value(value: Any) -> Any:
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    if isinstance(value, list):
        return [_json_value(item) for item in value]
    if isinstance(value, tuple):
        return [_json_value(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _json_value(item) for key, item in value.items()}
    return value


def _as_dict(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except (TypeError, ValueError):
            return {}
        return parsed if isinstance(parsed, dict) else {}
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    return {}


def _point_payload(point: Any) -> dict[str, Any]:
    payload = getattr(point, "payload", None)
    if payload is None and isinstance(point, dict):
        payload = point.get("payload", point)
    return payload if isinstance(payload, dict) else {}


def _points(response: Any) -> list[Any]:
    if response is None:
        return []
    points = getattr(response, "points", None)
    if points is not None:
        return list(points)
    if isinstance(response, tuple):
        return list(response[0])
    if isinstance(response, list):
        return response
    return []


def _scroll_result(response: Any) -> tuple[list[Any], Any]:
    if isinstance(response, tuple):
        return list(response[0] or []), response[1] if len(response) > 1 else None
    return _points(response), None


def _embed_dimension(embedder: Any) -> int:
    for name in ("dim", "dimension"):
        value = getattr(embedder, name, None)
        if value is not None:
            return int(value)
    for name in ("get_embedding_dimension", "get_sentence_embedding_dimension"):
        method = getattr(embedder, name, None)
        if callable(method):
            return int(method())
    raise ValueError("Injected embedder must expose dim or an embedding dimension method")


def _vector_list(vector: Any) -> list[float]:
    if hasattr(vector, "tolist"):
        vector = vector.tolist()
    return [float(value) for value in vector]


def _sparse_payload(vector: Any) -> dict[str, list[Any]]:
    if isinstance(vector, dict):
        indices = vector.get("indices", [])
        values = vector.get("values", [])
    else:
        indices = getattr(vector, "indices", [])
        values = getattr(vector, "values", [])
    if hasattr(indices, "tolist"):
        indices = indices.tolist()
    if hasattr(values, "tolist"):
        values = values.tolist()
    return {
        "indices": [int(value) for value in indices],
        "values": [float(value) for value in values],
    }


def _sparse_result(encoder: Any, text: str) -> dict[str, list[Any]]:
    """Accept fastembed, simple test doubles, and callable encoders."""
    if encoder is None:
        raise RuntimeError("A sparse encoder is required for hybrid search")

    if callable(getattr(encoder, "encode", None)):
        result = encoder.encode([text])
    elif callable(getattr(encoder, "embed", None)):
        result = encoder.embed([text])
    elif callable(encoder):
        try:
            result = encoder([text])
        except TypeError:
            result = encoder(text)
    else:
        raise TypeError("Injected sparse encoder must expose embed/encode or be callable")

    if isinstance(result, dict) or hasattr(result, "indices"):
        return _sparse_payload(result)
    values = list(result)
    if not values:
        return {"indices": [], "values": []}
    return _sparse_payload(values[0])


class EvidenceStoreQdrant:
    """Qdrant adapter for :class:`CorpusArtifact`.

    ``client``, ``embedder``, and ``sparse_encoder`` are injectable.  When
    omitted, they are created lazily, which keeps imports and unit tests
    offline while preserving the normal Qdrant/model-backed runtime.
    """

    def __init__(
        self,
        url: str | None = None,
        api_key: str | None = None,
        *,
        client: Any | None = None,
        embedder: Any | None = None,
        sparse_encoder: Any | None = None,
        prefix: str | None = None,
        embed_model: str | None = None,
    ) -> None:
        self._url = url or os.environ.get("QDRANT_URL", "http://localhost:6333")
        self._api_key = api_key or os.environ.get("QDRANT_API_KEY") or None
        self._client = client
        self._embedder = embedder
        self._sparse_encoder = sparse_encoder
        self._prefix = _runtime_prefix() if prefix is None else prefix
        self._collections = _collection_names(self._prefix)
        self.embed_model = embed_model or os.environ.get(
            "DS_EMBED_MODEL", DEFAULT_EMBED_MODEL
        )
        self._dimension = (
            _embed_dimension(embedder) if embedder is not None else DEFAULT_EMBED_DIMENSION
        )
        self._ready = False

    def collection_names(self) -> dict[str, str]:
        """Return the canonical evidence collection namespace."""
        return dict(self._collections)

    def _get_client(self) -> Any:
        if self._client is None:
            from qdrant_client import QdrantClient

            self._client = QdrantClient(
                url=self._url, api_key=self._api_key, timeout=120
            )
        return self._client

    def _get_embedder(self) -> Any:
        if self._embedder is None:
            from ..embed import Embedder

            self._embedder = Embedder(model_name=self.embed_model)
            self._dimension = _embed_dimension(self._embedder)
        return self._embedder

    def _get_sparse_encoder(self) -> Any:
        if self._sparse_encoder is None:
            from fastembed import SparseTextEmbedding

            self._sparse_encoder = SparseTextEmbedding(model_name="Qdrant/bm25")
        return self._sparse_encoder

    def _ensure_collections(self) -> None:
        if self._ready:
            return
        client = self._get_client()
        existing_response = client.get_collections()
        existing = set()
        for collection in getattr(existing_response, "collections", existing_response or []):
            if isinstance(collection, str):
                existing.add(collection)
            elif isinstance(collection, dict):
                existing.add(collection.get("name", ""))
            else:
                existing.add(getattr(collection, "name", ""))

        if self._collections["catalog"] not in existing:
            client.create_collection(
                collection_name=self._collections["catalog"], vectors_config={}
            )
        else:
            self._validate_payload_only(self._collections["catalog"])

        if self._collections["graph"] not in existing:
            client.create_collection(
                collection_name=self._collections["graph"], vectors_config={}
            )
        else:
            self._validate_payload_only(self._collections["graph"])

        if self._collections["evidence"] not in existing:
            from qdrant_client.models import (
                Distance,
                SparseIndexParams,
                SparseVectorParams,
                VectorParams,
            )

            client.create_collection(
                collection_name=self._collections["evidence"],
                vectors_config={
                    "dense": VectorParams(
                        size=self._dimension, distance=Distance.COSINE
                    )
                },
                sparse_vectors_config={
                    "sparse": SparseVectorParams(
                        index=SparseIndexParams(on_disk=False)
                    )
                },
            )
        else:
            self._validate_evidence_schema()

        self._ensure_payload_indexes()
        self._ready = True

    def _collection_info(self, name: str) -> Any:
        return self._get_client().get_collection(name)

    @staticmethod
    def _points_count(info: Any) -> int:
        value = getattr(info, "points_count", None)
        if value is None and isinstance(info, dict):
            value = info.get("points_count", 0)
        return int(value or 0)

    @staticmethod
    def _vector_config(info: Any) -> tuple[Any, Any]:
        config = getattr(info, "config", None)
        if config is None and isinstance(info, dict):
            config = info.get("config", info)
        params = getattr(config, "params", None)
        if params is None and isinstance(config, dict):
            params = config.get("params", config)
        vectors = getattr(params, "vectors", None)
        if vectors is None and isinstance(params, dict):
            vectors = params.get("vectors")
        sparse = getattr(params, "sparse_vectors", None)
        if sparse is None:
            sparse = getattr(params, "sparse_vectors_config", None)
        if sparse is None and isinstance(params, dict):
            sparse = params.get("sparse_vectors", params.get("sparse_vectors_config"))
        return vectors, sparse

    def _validate_payload_only(self, collection: str) -> None:
        info = self._collection_info(collection)
        vectors, sparse = self._vector_config(info)
        if vectors or sparse:
            count = self._points_count(info)
            raise RuntimeError(
                f"{collection} has an incompatible vector schema ({count} points); "
                "refusing to delete or reset it"
            )

    def _validate_evidence_schema(self) -> None:
        info = self._collection_info(self._collections["evidence"])
        vectors, sparse = self._vector_config(info)
        dense = vectors.get("dense") if isinstance(vectors, dict) else None
        stored_dim = getattr(dense, "size", None)
        if stored_dim is None and isinstance(dense, dict):
            stored_dim = dense.get("size")
        sparse_present = isinstance(sparse, dict) and "sparse" in sparse
        if sparse_present is False and sparse is not None:
            sparse_present = hasattr(sparse, "get") and bool(sparse.get("sparse"))
        compatible = (
            isinstance(vectors, dict)
            and "dense" in vectors
            and int(stored_dim or -1) == self._dimension
            and sparse_present
        )
        if not compatible:
            count = self._points_count(info)
            raise RuntimeError(
                f"{self._collections['evidence']} schema mismatch: stored dimension="
                f"{stored_dim!r}, sparse={sparse_present}, expected dimension={self._dimension}; "
                f"refusing to delete/reset ({count} existing points)"
            )

    def _ensure_payload_indexes(self) -> None:
        from qdrant_client.models import PayloadSchemaType

        client = self._get_client()
        if not hasattr(client, "create_payload_index"):
            return
        keyword_fields = {
            "catalog": ("part", "revision", "vendor", "device_type"),
            "evidence": (
                "id", "part", "revision", "kind", "semantic_type", "parent_id",
                "aliases", "focuses",
            ),
            "graph": (
                "id", "part", "relation", "source_id", "target_id",
            ),
        }
        text_fields = {
            "catalog": ("title",),
            "evidence": ("title", "text", "embed_text"),
            "graph": ("label",),
        }
        for role, fields in keyword_fields.items():
            for field in fields:
                client.create_payload_index(
                    collection_name=self._collections[role],
                    field_name=field,
                    field_schema=PayloadSchemaType.KEYWORD,
                )
        for role, fields in text_fields.items():
            for field in fields:
                client.create_payload_index(
                    collection_name=self._collections[role],
                    field_name=field,
                    field_schema=PayloadSchemaType.TEXT,
                )

    def _validate_artifact(self, artifact: CorpusArtifact) -> str:
        manifest = artifact.manifest
        part = _upper_part(manifest.part)
        if manifest.schema_version != SCHEMA_VERSION:
            raise ValueError(
                f"Unsupported corpus schema {manifest.schema_version!r}; expected evidence"
            )
        if _upper_part(artifact.catalog.part) != part:
            raise ValueError("artifact catalog part does not match its manifest part")
        if _upper_part(artifact.coverage.part) != part:
            raise ValueError("artifact coverage part does not match its manifest part")
        if manifest.embed_model != self.embed_model:
            raise ValueError(
                f"Artifact embed model {manifest.embed_model!r} does not match "
                f"DS_EMBED_MODEL {self.embed_model!r}"
            )
        if int(manifest.embed_dimension) != self._dimension:
            raise ValueError(
                f"Artifact embedding dimension {manifest.embed_dimension} does not match "
                f"the configured embedder dimension {self._dimension}"
            )
        for item in artifact.evidence:
            if _upper_part(item.part) != part:
                raise ValueError(f"evidence item {item.id!r} crosses the artifact part boundary")
        for relation in artifact.relations:
            if _upper_part(relation.part) != part:
                raise ValueError(f"relation {relation.id!r} crosses the artifact part boundary")
        return part

    def prewarm(self) -> dict[str, str]:
        """Create or validate collections and initialize local encoders."""
        self._get_embedder()
        self._get_sparse_encoder()
        self._ensure_collections()
        return self.collection_names()

    def clear_part(self, part: str) -> None:
        """Delete only points carrying the selected part from all stores."""
        self._ensure_collections()
        from qdrant_client.models import FieldCondition, Filter, MatchValue

        selector = Filter(
            must=[FieldCondition(key="part", match=MatchValue(value=_upper_part(part)))]
        )
        for collection in self._collections.values():
            self._get_client().delete(
                collection_name=collection,
                points_selector=selector,
            )

    def upsert_artifact(self, artifact: CorpusArtifact) -> int:
        """Validate, part-clear, and upsert one complete corpus artifact."""
        embedder = self._get_embedder()
        part = self._validate_artifact(artifact)
        self._ensure_collections()
        self._get_sparse_encoder()
        if _embed_dimension(embedder) != int(artifact.manifest.embed_dimension):
            raise ValueError("runtime embedder dimension does not match artifact manifest")

        self.clear_part(part)
        points: list[Any] = []
        catalog_id = stable_id(
            part,
            artifact.manifest.revision,
            "document",
            "catalog",
            discriminator=part,
        )
        outline = self._outline(artifact.evidence)
        catalog_payload = {
            "id": catalog_id,
            "part": part,
            "revision": artifact.manifest.revision,
            "vendor": artifact.catalog.vendor,
            "title": artifact.catalog.title,
            "device_type": artifact.catalog.device_type,
            "kind": "document",
            "catalog": artifact.catalog.model_dump(mode="json"),
            "coverage": artifact.coverage.model_dump(mode="json"),
            "outline": [node.model_dump(mode="json") for node in outline],
            "schema_version": SCHEMA_VERSION,
        }
        points.append(self._point(catalog_id, {}, catalog_payload))
        self._upsert(self._collections["catalog"], points)

        evidence_points: list[Any] = []
        for item in artifact.evidence:
            text = item.embed_text()
            dense = _vector_list(embedder.embed_documents([text])[0])
            sparse = _sparse_result(self._get_sparse_encoder(), text)
            payload = self._evidence_payload(item, artifact.manifest.revision)
            evidence_points.append(
                self._point(
                    item.id,
                    {"dense": dense, "sparse": self._qdrant_sparse(sparse)},
                    payload,
                )
            )
        self._upsert(self._collections["evidence"], evidence_points)

        graph_points = [
            self._point(
                relation.id,
                {},
                self._relation_payload(relation),
            )
            for relation in artifact.relations
        ]
        self._upsert(self._collections["graph"], graph_points)
        return 1 + len(evidence_points) + len(graph_points)

    @staticmethod
    def _outline(evidence: Iterable[EvidenceItem]) -> list[CatalogNode]:
        nodes = [
            CatalogNode(
                id=item.id,
                kind=item.kind,
                title=item.title,
                parent_id=item.parent_id,
                sequence=item.sequence,
            )
            for item in evidence
            if item.kind in _STRUCTURAL_KINDS
        ]
        return sorted(nodes, key=lambda node: (node.sequence is None, node.sequence or 0, node.title))

    @staticmethod
    def _evidence_payload(item: EvidenceItem, revision: str) -> dict[str, Any]:
        focuses = EvidenceStoreQdrant._focuses(item)
        return {
            "id": item.id,
            "part": item.part.upper(),
            "revision": revision,
            "kind": item.kind,
            "title": item.title,
            "text": item.text,
            "embed_text": item.embed_text(),
            "aliases": list(item.aliases),
            "parent_id": item.parent_id,
            "sequence": item.sequence,
            "semantic_type": item.semantic_type,
            "focuses": focuses,
            "values": _json_value(item.values),
            "table": _json_value(item.table),
            "sources": _json_value(item.sources),
            "confidence": item.confidence,
            "validated": item.validated,
            "enrichment": item.enrichment,
            "item": item.model_dump(mode="json"),
            "schema_version": SCHEMA_VERSION,
        }

    @staticmethod
    def _relation_payload(relation: GraphRelation) -> dict[str, Any]:
        return {
            "id": relation.id,
            "part": relation.part.upper(),
            "relation": relation.relation,
            "source_id": relation.source_id,
            "target_id": relation.target_id,
            "label": relation.label,
            "confidence": relation.confidence,
            "source_refs": list(relation.source_refs),
            "relation_item": relation.model_dump(mode="json"),
            "schema_version": SCHEMA_VERSION,
        }

    @staticmethod
    def _focuses(item: EvidenceItem) -> list[str]:
        result = {"auto"}
        kind = item.kind
        for focus, kinds in _FOCUS_KINDS.items():
            if kind in kinds:
                result.add(focus)
        text = _normalise(" ".join((item.title, item.text, item.semantic_type)))
        if any(word in text for word in ("timing", "frequency", "cycle", "latency", "ns", "mhz")):
            result.add("timing")
        if any(word in text for word in ("operation", "procedure", "sequence", "before", "after")):
            result.update(("operation", "configure"))
        return sorted(result)

    @staticmethod
    def _qdrant_sparse(sparse: dict[str, list[Any]]) -> Any:
        from qdrant_client.models import SparseVector

        return SparseVector(indices=sparse["indices"], values=sparse["values"])

    @staticmethod
    def _point(identifier: str, vector: dict[str, Any], payload: dict[str, Any]) -> Any:
        from qdrant_client.models import PointStruct

        return PointStruct(id=_point_id(identifier), vector=vector, payload=payload)

    def _upsert(self, collection: str, points: list[Any]) -> None:
        if not points:
            return
        client = self._get_client()
        for start in range(0, len(points), _BATCH):
            client.upsert(
                collection_name=collection,
                points=points[start : start + _BATCH],
            )

    def _filter(
        self,
        part: str,
        *,
        kind: str | None = None,
        focus: str | None = None,
        field: str | None = None,
        value: str | None = None,
    ) -> Any:
        from qdrant_client.models import FieldCondition, Filter, MatchValue

        must = [FieldCondition(key="part", match=MatchValue(value=_upper_part(part)))]
        if kind:
            must.append(FieldCondition(key="kind", match=MatchValue(value=kind)))
        if focus and focus != "auto":
            must.append(FieldCondition(key="focuses", match=MatchValue(value=focus)))
        if field and value is not None:
            must.append(FieldCondition(key=field, match=MatchValue(value=value)))
        return Filter(must=must)

    def _scroll(
        self,
        collection: str,
        query_filter: Any | None = None,
        *,
        limit: int = 10000,
    ) -> list[Any]:
        response = self._get_client().scroll(
            collection_name=collection,
            scroll_filter=query_filter,
            limit=limit,
            with_payload=True,
            with_vectors=False,
        )
        rows, _ = _scroll_result(response)
        return rows

    @staticmethod
    def _item_from_payload(payload: dict[str, Any]) -> EvidenceItem | None:
        raw = payload.get("item")
        data = _as_dict(raw) if raw is not None else payload
        if not data.get("id"):
            return None
        try:
            return EvidenceItem.model_validate(data)
        except Exception:
            return None

    @staticmethod
    def _relation_from_payload(payload: dict[str, Any]) -> GraphRelation | None:
        raw = payload.get("relation_item")
        data = _as_dict(raw) if raw is not None else payload
        if not data.get("id"):
            return None
        try:
            return GraphRelation.model_validate(data)
        except Exception:
            return None

    @staticmethod
    def _catalog_from_payload(payload: dict[str, Any]) -> CatalogPart | None:
        data = _as_dict(payload.get("catalog"))
        if not data.get("part"):
            data = {
                "part": payload.get("part", ""),
                "vendor": payload.get("vendor", ""),
                "title": payload.get("title", ""),
                "revision": payload.get("revision", ""),
                "device_type": payload.get("device_type", ""),
            }
        coverage = _as_dict(payload.get("coverage"))
        if coverage:
            data["coverage"] = coverage
        try:
            return CatalogPart.model_validate(data)
        except Exception:
            return None

    def get(
        self,
        part: str,
        target: str,
        *,
        kind: EntityKind | Literal["auto"] = "auto",
        include_related: bool = True,
        depth: int = 1,
    ) -> GetResponse:
        """Resolve an exact stable id, title, or alias within one part."""
        part = _upper_part(part)
        self._ensure_collections()
        query_filter = self._filter(part, kind=None if kind == "auto" else kind)
        candidates: list[EvidenceItem] = []
        wanted = _normalise(target)
        for point in self._scroll(self._collections["evidence"], query_filter):
            payload = _point_payload(point)
            if _upper_part(payload.get("part", "")) != part:
                continue
            item = self._item_from_payload(payload)
            if item is None:
                continue
            names = {_normalise(item.id), _normalise(item.title)}
            names.update(_normalise(alias) for alias in item.aliases)
            if wanted in names:
                candidates.append(item)
        candidates.sort(key=lambda item: item.id)
        if len(candidates) != 1:
            summary = (
                f"No exact target {target!r} found in {part}."
                if not candidates
                else f"Ambiguous target {target!r} in {part}; choose a stable id."
            )
            return self._get_response(
                part,
                target,
                summary=summary,
                candidates=candidates,
            )

        item = candidates[0]
        related = self.relations(part, item.id, depth=depth) if include_related else []
        return self._get_response(
            part,
            target,
            summary=f"Exact evidence target: {item.title}",
            entity=item,
            related=related,
        )

    @staticmethod
    def _get_response(
        part: str,
        target: str,
        *,
        summary: str,
        entity: EvidenceItem | None = None,
        candidates: list[EvidenceItem] | None = None,
        related: list[GraphRelation] | None = None,
    ) -> GetResponse:
        sources = list(entity.sources) if entity else []
        return GetResponse(
            part=part,
            target=target,
            summary=summary,
            entity=entity,
            candidates=candidates or [],
            related_entities=related or [],
            sources=sources,
        )

    def search(
        self,
        part: str,
        query: str,
        *,
        kind: EntityKind | None = None,
        focus: FocusKind | None = None,
        limit: int = 5,
        k: int | None = None,
    ) -> list[EvidenceItem]:
        """Hybrid dense/BM25 search with mandatory part isolation."""
        part = _upper_part(part)
        limit = max(1, min(int(k if k is not None else limit), 100))
        self._ensure_collections()
        embedder = self._get_embedder()
        dense = _vector_list(embedder.embed_query(query))
        sparse = _sparse_result(self._get_sparse_encoder(), query)
        query_filter = self._filter(part, kind=kind, focus=focus)
        from qdrant_client.models import Prefetch, Fusion, FusionQuery

        sparse_vector = self._qdrant_sparse(sparse)
        client = self._get_client()
        fetch_limit = max(limit * 4, 20)
        try:
            response = client.query_points(
                collection_name=self._collections["evidence"],
                prefetch=[
                    Prefetch(
                        query=dense,
                        using="dense",
                        limit=fetch_limit,
                        filter=query_filter,
                    ),
                    Prefetch(
                        query=sparse_vector,
                        using="sparse",
                        limit=fetch_limit,
                        filter=query_filter,
                    ),
                ],
                query=FusionQuery(fusion=Fusion.RRF),
                query_filter=query_filter,
                limit=fetch_limit,
                with_payload=True,
            )
            rows = _points(response)
        except (AttributeError, NotImplementedError, TypeError):
            rows = self._scroll(self._collections["evidence"], query_filter)

        result: list[EvidenceItem] = []
        seen: set[str] = set()
        allowed_kinds = _FOCUS_KINDS.get(focus or "auto")
        for point in rows:
            payload = _point_payload(point)
            if _upper_part(payload.get("part", "")) != part:
                continue
            if kind and payload.get("kind") != kind:
                continue
            if focus and focus != "auto":
                focuses = payload.get("focuses") or []
                if focus not in focuses and (allowed_kinds and payload.get("kind") not in allowed_kinds):
                    continue
            item = self._item_from_payload(payload)
            if item is not None and item.id not in seen:
                seen.add(item.id)
                result.append(item)
            if len(result) >= limit:
                break
        return result

    def relations(
        self,
        part: str,
        node: str,
        *,
        relation: str | list[str] | None = None,
        depth: int = 1,
        direction: Literal["in", "out", "both"] = "both",
        limit: int = 100,
        relation_types: list[str] | None = None,
    ) -> list[GraphRelation]:
        """Return bounded graph neighbors for one part and node."""
        part = _upper_part(part)
        self._ensure_collections()
        max_depth = max(0, min(int(depth), _MAX_GRAPH_DEPTH))
        if max_depth == 0:
            return []
        relation_values = relation_types or (
            [relation] if isinstance(relation, str) else relation
        )
        frontier = {node}
        visited_nodes = {node}
        seen_edges: set[str] = set()
        output: list[GraphRelation] = []
        for _ in range(max_depth):
            next_frontier: set[str] = set()
            for current in frontier:
                rows = self._graph_rows(part, current, direction, relation_values)
                for point in rows:
                    payload = _point_payload(point)
                    if _upper_part(payload.get("part", "")) != part:
                        continue
                    edge = self._relation_from_payload(payload)
                    if edge is None or edge.id in seen_edges:
                        continue
                    seen_edges.add(edge.id)
                    output.append(edge)
                    if len(output) >= max(1, min(int(limit), 1000)):
                        return output
                    for candidate in (edge.source_id, edge.target_id):
                        if candidate not in visited_nodes:
                            visited_nodes.add(candidate)
                            next_frontier.add(candidate)
            frontier = next_frontier
            if not frontier:
                break
        return output

    def _graph_rows(
        self,
        part: str,
        node: str,
        direction: str,
        relation_values: list[str] | None,
    ) -> list[Any]:
        from qdrant_client.models import FieldCondition, Filter, MatchValue

        fields = []
        if direction in ("out", "both"):
            fields.append("source_id")
        if direction in ("in", "both"):
            fields.append("target_id")
        rows: list[Any] = []
        for field in fields:
            must = [
                FieldCondition(key="part", match=MatchValue(value=part)),
                FieldCondition(key=field, match=MatchValue(value=node)),
            ]
            if relation_values and len(relation_values) == 1:
                must.append(
                    FieldCondition(
                        key="relation", match=MatchValue(value=relation_values[0])
                    )
                )
            query_filter = Filter(must=must)
            if relation_values and len(relation_values) > 1:
                query_filter = Filter(
                    must=must,
                    should=[
                        FieldCondition(key="relation", match=MatchValue(value=value))
                        for value in relation_values
                    ],
                )
            rows.extend(self._scroll(self._collections["graph"], query_filter, limit=1000))
        return rows

    def catalog(
        self,
        part: str | None = None,
        *,
        query: str = "",
        cursor: str = "",
        limit: int = 100,
    ) -> CatalogResponse:
        """Return indexed parts, outline, and extraction coverage."""
        requested = _upper_part(part) if part else None
        self._ensure_collections()
        rows = self._scroll(self._collections["catalog"], limit=max(1, min(limit, 1000)))
        parts: list[CatalogPart] = []
        outlines: list[CatalogNode] = []
        coverage: CoverageReport | None = None
        needle = _normalise(query)
        for point in rows:
            payload = _point_payload(point)
            row_part = _upper_part(payload.get("part", "")) if payload.get("part") else ""
            if requested and row_part != requested:
                continue
            catalog = self._catalog_from_payload(payload)
            if catalog is None:
                continue
            if needle and needle not in _normalise(
                " ".join((catalog.part, catalog.title, catalog.vendor, catalog.device_type))
            ):
                continue
            parts.append(catalog)
            coverage = catalog.coverage
            for raw_node in payload.get("outline", []) or []:
                try:
                    outlines.append(CatalogNode.model_validate(raw_node))
                except Exception:
                    continue
        parts.sort(key=lambda item: item.part)
        outlines.sort(key=lambda item: (item.sequence is None, item.sequence or 0, item.title))
        summary = (
            f"{len(parts)} indexed part(s)"
            if not requested
            else f"{len(parts)} catalog record(s) for {requested}"
        )
        return CatalogResponse(
            summary=summary,
            parts=parts,
            outline=outlines,
            coverage=coverage if requested else None,
            warnings=list(coverage.warnings) if coverage else [],
            next_cursor="",
        )


__all__ = [
    "DEFAULT_EMBED_MODEL",
    "DEFAULT_EMBED_DIMENSION",
    "EvidenceStoreQdrant",
    "_collection_names",
    "_point_id",
]
