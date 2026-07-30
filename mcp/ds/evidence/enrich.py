"""Optional, local-only semantic enrichment for datasheet evidence.

The deterministic corpus is always publishable.  This module may add semantic
entities when a local OpenAI-compatible model is configured, but it rejects any
model output that cannot be anchored to literal source text.
"""

from __future__ import annotations

import hashlib
import ipaddress
import json
import os
import re
from pathlib import Path
from typing import Any
from urllib.parse import urlparse
from urllib.request import Request, urlopen

from .model import CorpusArtifact, EvidenceItem, GraphRelation, stable_id


_ALLOWED_KINDS = {
    "command", "mode", "operation", "step", "parameter", "constraint",
    "warning", "memory_region", "register", "bitfield", "pin",
}
_ALLOWED_RELATIONS = {
    "REFERENCES", "REQUIRES", "ENABLES", "SETS_MODE", "USES_COMMAND",
    "READS_REGISTER", "WRITES_REGISTER", "CONSTRAINED_BY", "APPLIES_TO",
    "CONFLICTS_WITH", "EVIDENCED_BY",
}


def _normalized(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip().casefold()


def _is_local_url(url: str) -> bool:
    host = (urlparse(url).hostname or "").lower()
    if host in {"localhost", "127.0.0.1", "::1"}:
        return True
    if os.environ.get("DS_LOCAL_AI_ALLOW_LAN", "").lower() in {"1", "true", "yes"}:
        try:
            return ipaddress.ip_address(host).is_private
        except ValueError:
            return False
    return False


class LocalAIClient:
    def __init__(self, url: str | None = None, model: str | None = None):
        self.url = (url or os.environ.get("DS_LOCAL_AI_URL", "")).rstrip("/")
        self.model = model or os.environ.get("DS_LOCAL_AI_MODEL", "")
        if self.url and not _is_local_url(self.url):
            raise ValueError(
                "DS_LOCAL_AI_URL must be localhost/private LAN; cloud enrichment is disabled"
            )

    @property
    def enabled(self) -> bool:
        return bool(self.url and self.model)

    def extract(self, evidence: EvidenceItem) -> dict[str, Any]:
        prompt = (
            "Extract only literal datasheet facts from the SOURCE below. Return JSON with "
            "an 'entities' array and a 'relations' array. Each entity requires kind, title, "
            "quote, text, semantic_type, aliases, and values. quote must be an exact source "
            "substring. Allowed entity kinds: " + ", ".join(sorted(_ALLOWED_KINDS)) + ". "
            "Do not infer unstated values.\n\nSOURCE:\n" + evidence.embed_text()
        )
        body = json.dumps({
            "model": self.model,
            "temperature": 0,
            "response_format": {"type": "json_object"},
            "messages": [{"role": "user", "content": prompt}],
        }).encode("utf-8")
        request = Request(
            f"{self.url}/chat/completions",
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urlopen(request, timeout=120) as response:  # noqa: S310 - URL is local-only
            payload = json.loads(response.read().decode("utf-8"))
        content = payload["choices"][0]["message"]["content"]
        return json.loads(content)


def _validate_entity(raw: dict[str, Any], source: EvidenceItem) -> EvidenceItem | None:
    kind = str(raw.get("kind", ""))
    quote = str(raw.get("quote", "")).strip()
    title = str(raw.get("title", "")).strip()
    if kind not in _ALLOWED_KINDS or not quote or not title:
        return None
    source_text = _normalized(source.embed_text())
    if _normalized(quote) not in source_text:
        return None
    values = raw.get("values") if isinstance(raw.get("values"), dict) else {}
    # Exact scalar values must appear in the cited quote or the full source.
    for value in values.values():
        if isinstance(value, (str, int, float)) and _normalized(str(value)) not in source_text:
            return None
    entity_id = stable_id(
        source.part,
        source.sources[0].revision if source.sources else "",
        kind,
        f"{source.id}/{title}",
        discriminator=quote,
    )
    return EvidenceItem(
        id=entity_id,
        part=source.part,
        kind=kind,  # type: ignore[arg-type]
        title=title,
        text=str(raw.get("text", quote)).strip() or quote,
        aliases=[str(x) for x in raw.get("aliases", []) if str(x).strip()],
        parent_id=source.id,
        semantic_type=str(raw.get("semantic_type", kind)),
        values=values,
        sources=source.sources,
        confidence="medium",
        validated=True,
        enrichment="local_ai",
    )


def enrich_artifact(
    artifact: CorpusArtifact,
    *,
    client: LocalAIClient | None = None,
    cache_path: Path | None = None,
) -> CorpusArtifact:
    client = client or LocalAIClient()
    if not client.enabled:
        artifact.coverage.warnings.append(
            "Local semantic AI is not configured; deterministic evidence remains available."
        )
        artifact.manifest.enrichment_status = "skipped"
        return artifact

    cache: dict[str, Any] = {}
    if cache_path and cache_path.is_file():
        try:
            cache = json.loads(cache_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            cache = {}

    existing_ids = {item.id for item in artifact.evidence}
    added = 0
    failures = 0
    source_items = [
        item for item in artifact.evidence
        if item.kind in {"table", "table_row", "prose", "figure"}
        and len(item.embed_text()) >= 40
    ]
    for source in source_items:
        key = hashlib.sha256(
            f"{client.model}\x1f{source.embed_text()}".encode("utf-8")
        ).hexdigest()
        try:
            raw = cache.get(key)
            if raw is None:
                raw = client.extract(source)
                cache[key] = raw
            for proposal in raw.get("entities", []):
                if not isinstance(proposal, dict):
                    continue
                entity = _validate_entity(proposal, source)
                if entity is None or entity.id in existing_ids:
                    continue
                artifact.evidence.append(entity)
                existing_ids.add(entity.id)
                relation_id = stable_id(
                    source.part, "", "relation", f"{entity.id}/EVIDENCED_BY/{source.id}"
                )
                artifact.relations.append(GraphRelation(
                    id=relation_id,
                    part=source.part,
                    relation="EVIDENCED_BY",
                    source_id=entity.id,
                    target_id=source.id,
                    label=f"{entity.title} evidenced by {source.title}",
                    confidence="medium",
                    source_refs=[ref.source_id for ref in source.sources],
                ))
                added += 1
        except Exception as exc:  # keep deterministic ingestion publishable
            failures += 1
            artifact.coverage.warnings.append(
                f"Local AI enrichment failed for {source.id}: {type(exc).__name__}"
            )

    if cache_path:
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        temp = cache_path.with_suffix(".tmp")
        temp.write_text(json.dumps(cache, indent=2, ensure_ascii=False), encoding="utf-8")
        temp.replace(cache_path)

    artifact.coverage.local_ai_used = added > 0
    artifact.manifest.evidence_count = len(artifact.evidence)
    artifact.manifest.relation_count = len(artifact.relations)
    artifact.manifest.enrichment_status = (
        "complete" if added and not failures else "partial" if added or failures else "skipped"
    )
    return artifact
