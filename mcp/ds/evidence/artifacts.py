"""Persistence helpers for canonical corpus artifacts."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from ..catalog import part_dir, source_pdf
from .model import CorpusArtifact


ARTIFACT_DIRECTORY = "evidence"


def artifact_dir(part: str) -> Path:
    return part_dir(part) / ARTIFACT_DIRECTORY


def corpus_path(part: str) -> Path:
    return artifact_dir(part) / "corpus.json"


def source_sha256(part: str) -> str:
    path = source_pdf(part)
    if not path.is_file():
        return ""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def save_artifact(artifact: CorpusArtifact) -> Path:
    path = corpus_path(artifact.manifest.part)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(".json.tmp")
    temp.write_text(
        json.dumps(artifact.model_dump(mode="json"), indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    temp.replace(path)
    return path


def load_artifact(part: str) -> CorpusArtifact:
    path = corpus_path(part)
    if not path.is_file():
        raise FileNotFoundError(
            f"No evidence corpus artifact for '{part}'. Run the extraction stage first: "
            f"python tools/extract_evidence.py --part {part}"
        )
    return CorpusArtifact.model_validate_json(path.read_text(encoding="utf-8"))
