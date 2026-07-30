"""Build and publish the canonical evidence corpus for one part."""

from __future__ import annotations

import json

from .. import catalog
from .artifacts import artifact_dir, save_artifact, source_sha256
from .enrich import enrich_artifact
from .ingest import build_corpus
from .store_qdrant import EvidenceStoreQdrant


def _metadata(part: str) -> tuple[str, str]:
    path = catalog.catalog_json(part)
    if not path.is_file():
        return "", ""
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return "", ""
    return str(raw.get("vendor", "")), str(raw.get("revision", ""))


def build_part(
    part: str,
    *,
    with_enrichment: bool = True,
    artifact_only: bool = False,
) -> dict[str, int | str | bool]:
    vendor, revision = _metadata(part)
    artifact = build_corpus(part, vendor=vendor, revision=revision)
    artifact.manifest.source_sha256 = source_sha256(part)
    if with_enrichment:
        artifact = enrich_artifact(
            artifact,
            cache_path=artifact_dir(part) / "enrichment_cache.json",
        )
    path = save_artifact(artifact)

    if not artifact_only:
        store = EvidenceStoreQdrant()
        store.upsert_artifact(artifact)

    stats: dict[str, int | str | bool] = {
        "part": part,
        "evidence": len(artifact.evidence),
        "relations": len(artifact.relations),
        "artifact": str(path),
        "published": not artifact_only,
    }
    print(f"  evidence done: {stats}")
    return stats
