"""Opt-in cross-encoder reranker for ds_search prose results.

Enabled by setting DS_RERANKER_MODEL in mcp/.env:
    DS_RERANKER_MODEL=cross-encoder/ms-marco-MiniLM-L-6-v2   # fast, ~24 MB, CPU-friendly
    DS_RERANKER_MODEL=BAAI/bge-reranker-base                  # higher quality, ~110 MB

When DS_RERANKER_MODEL is unset (default), rerank() is a zero-cost pass-through.

How it works:
  1. ds_search fetches k*4 candidates from the hybrid BM25+dense index
  2. CrossEncoder scores every (query, passage) pair jointly
  3. Results are re-sorted by cross-encoder score; top-k are returned
"""
from __future__ import annotations

import os
from functools import lru_cache

_RERANKER_MODEL = os.environ.get("DS_RERANKER_MODEL", "").strip()


@lru_cache(maxsize=1)
def _load_reranker():
    """Process-wide singleton. Returns CrossEncoder or None."""
    if not _RERANKER_MODEL:
        return None
    from sentence_transformers import CrossEncoder
    print(f"  Reranker: {_RERANKER_MODEL}")
    return CrossEncoder(_RERANKER_MODEL)


def is_enabled() -> bool:
    return bool(_RERANKER_MODEL)


def rerank(query: str, results: list[dict], *, top_k: int) -> list[dict]:
    """Re-score results with the cross-encoder and return top_k.

    If DS_RERANKER_MODEL is unset, returns results[:top_k] unchanged.
    """
    model = _load_reranker()
    if model is None or not results:
        return results[:top_k]

    pairs = [(query, r.get("text", "")) for r in results]
    scores = model.predict(pairs)
    ranked = sorted(zip(scores, results), key=lambda x: x[0], reverse=True)
    return [r for _, r in ranked[:top_k]]
