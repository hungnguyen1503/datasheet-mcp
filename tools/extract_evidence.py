#!/usr/bin/env python3
"""Build the lossless evidence corpus artifact for one datasheet part."""

from __future__ import annotations

import argparse
import json

import _bootstrap  # noqa: F401

from ds import catalog
from ds.evidence.artifacts import artifact_dir, save_artifact, source_sha256
from ds.evidence.enrich import enrich_artifact
from ds.evidence.ingest import build_corpus


def _metadata(part: str) -> tuple[str, str]:
    path = catalog.catalog_json(part)
    if not path.is_file():
        return "", ""
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return "", ""
    return str(raw.get("vendor", "")), str(raw.get("revision", ""))


def main() -> None:
    parser = argparse.ArgumentParser(description="Extract a lossless Datasheet MCP corpus")
    parser.add_argument("--part", required=True, help="Part name, e.g. MX25LM51245G")
    parser.add_argument(
        "--no-enrich", action="store_true",
        help="Skip optional local LLM/VLM semantic enrichment",
    )
    args = parser.parse_args()

    vendor, revision = _metadata(args.part)
    artifact = build_corpus(args.part, vendor=vendor, revision=revision)
    artifact.manifest.source_sha256 = source_sha256(args.part)
    if not args.no_enrich:
        artifact = enrich_artifact(
            artifact,
            cache_path=artifact_dir(args.part) / "enrichment_cache.json",
        )
    path = save_artifact(artifact)
    print(
        f"evidence corpus: {len(artifact.evidence)} evidence items, "
        f"{len(artifact.relations)} relations -> {path}"
    )


if __name__ == "__main__":
    main()
