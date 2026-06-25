"""Token counting + budgeting helpers.

Uses tiktoken's cl100k_base as a stable, model-agnostic proxy for token count.
The goal is a consistent budget so ds responses stay small and predictable.
"""

from __future__ import annotations

from functools import lru_cache


@lru_cache(maxsize=1)
def _enc():
    import tiktoken

    return tiktoken.get_encoding("cl100k_base")


def count(text: str) -> int:
    return len(_enc().encode(text))


def truncate_to(text: str, max_tokens: int) -> tuple[str, bool]:
    """Truncate text to at most max_tokens. Returns (text, was_truncated)."""
    enc = _enc()
    toks = enc.encode(text)
    if len(toks) <= max_tokens:
        return text, False
    return enc.decode(toks[:max_tokens]), True


def pack(blocks: list[str], budget: int, *, sep: str = "\n\n") -> tuple[str, int]:
    """Greedily concatenate blocks until the token budget is exhausted.

    Returns (packed_text, n_blocks_included). Whole blocks only — never cuts a
    block in half — so register cards stay intact.
    """
    out: list[str] = []
    used = 0
    sep_cost = count(sep)
    for b in blocks:
        c = count(b)
        add = c + (sep_cost if out else 0)
        if used + add > budget and out:
            break
        out.append(b)
        used += add
    return sep.join(out), len(out)
