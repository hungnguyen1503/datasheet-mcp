"""Local, offline embedder — GPU/CPU adaptive.

Default model: BAAI/bge-small-en-v1.5 (384-dim, ~130 MB, CPU-friendly).
Batch size is auto-tuned: 256 on CUDA, 32 on CPU to avoid memory pressure.

Override via env vars (set in mcp/.env):
  DS_EMBED_MODEL        — HuggingFace Hub model name
  DS_EMBED_QUERY_PREFIX — custom query-side prefix
  DS_EMBED_DEVICE       — "cuda" or "cpu" (auto-detected if unset)
  DS_EMBED_BATCH_SIZE   — override batch size (integer)
"""

from __future__ import annotations

import os
from functools import lru_cache

DEFAULT_MODEL = os.environ.get("DS_EMBED_MODEL", "BAAI/bge-small-en-v1.5")
_QUERY_PREFIX = os.environ.get(
    "DS_EMBED_QUERY_PREFIX",
    "Represent this sentence for searching relevant passages: ",
)


def _detect_device() -> str:
    override = os.environ.get("DS_EMBED_DEVICE", "").strip()
    if override:
        return override
    try:
        import torch
        return "cuda" if torch.cuda.is_available() else "cpu"
    except ImportError:
        return "cpu"


# Adaptive batch size: large on GPU (fast VRAM), small on CPU (avoid OOM)
def _default_batch() -> int:
    raw = os.environ.get("DS_EMBED_BATCH_SIZE", "").strip()
    if raw.isdigit():
        return int(raw)
    return 256 if _detect_device() == "cuda" else 32


_BATCH_SIZE: int = _default_batch()  # exported for prose.py


class Embedder:
    def __init__(self, model_name: str = DEFAULT_MODEL):
        from sentence_transformers import SentenceTransformer

        self.model_name = model_name
        self.device = _detect_device()
        self.model = SentenceTransformer(model_name, device=self.device)
        try:
            self.dim = self.model.get_embedding_dimension()
        except AttributeError:
            self.dim = self.model.get_sentence_embedding_dimension()

        batch_note = f"batch={_BATCH_SIZE}"
        print(f"  Embedder: {model_name} ({self.dim}-dim) on {self.device.upper()}  [{batch_note}]")

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        # Process in adaptive batches so CPU doesn't run out of RAM
        all_vecs = []
        for i in range(0, len(texts), _BATCH_SIZE):
            batch = texts[i: i + _BATCH_SIZE]
            vecs = self.model.encode(
                batch, normalize_embeddings=True, show_progress_bar=False
            )
            all_vecs.extend(v.tolist() for v in vecs)
        return all_vecs

    def embed_query(self, text: str) -> list[float]:
        return self._embed_query_cached(text)

    @lru_cache(maxsize=256)
    def _embed_query_cached(self, text: str) -> list[float]:
        v = self.model.encode(
            _QUERY_PREFIX + text, normalize_embeddings=True, show_progress_bar=False
        )
        return v.tolist()


@lru_cache(maxsize=1)
def get_embedder(model_name: str = DEFAULT_MODEL) -> Embedder:
    """Process-wide singleton — the model is expensive to load."""
    return Embedder(model_name)
