"""MinerU markdown → ProseBlock extraction.

Reads data/<part>/MD/<NN_Section_Name>/*.md, treats each top-level section
folder as a functional *block*, and splits the markdown into heading-scoped
prose blocks. The block title is reused as the graph's block node key, so
register↔prose and graph cross-links line up.
"""

from __future__ import annotations

import re

from ..catalog import iter_sections, block_title
from ..model import ProseBlock

_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$")
_IMG_RE = re.compile(r"!\[[^\]]*\]\([^)]*\)")
_HTML_RE = re.compile(r"<[^>]+>")


def _clean(text: str) -> str:
    text = _IMG_RE.sub("", text)
    text = _HTML_RE.sub(" ", text)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def extract_prose(part: str, *, vendor: str = "", revision: str = "") -> list[ProseBlock]:
    blocks: list[ProseBlock] = []
    for sec in iter_sections(part):
        blk = block_title(sec.section_name)
        md = sec.path.read_text(encoding="utf-8", errors="replace")

        heading = blk
        buf: list[str] = []

        def flush():
            body = _clean("\n".join(buf))
            if len(body) >= 60:
                blocks.append(ProseBlock(
                    vendor=vendor,
                    part=part,
                    block=blk,
                    section=sec.section_name,
                    heading=heading,
                    breadcrumb=f"{part} > {blk} > {heading}",
                    text=body,
                    revision=revision,
                ))

        for line in md.splitlines():
            m = _HEADING_RE.match(line)
            if m:
                flush()
                buf = []
                heading = _clean(m.group(2)) or blk
            else:
                buf.append(line)
        flush()
    return blocks
