"""The datasheet query API — what the ds_* tools run underneath.

Answer paths, cheapest first:
  1. lookup_register(part, reg)  — deterministic, exact register card.
  2. lookup_bit(part, reg, bit)  — a single fully-scoped bit row.
  3. find_pin(part, …)           — deterministic pin/pad table.
  4. get_operation(part, block)  — ordered operation/procedure prose.
  5. search(part, query)         — hybrid BM25+semantic prose + register hits.
  6. neighbors(part, node)       — graph dependency summary.

Every path is part-scoped, so a result can never describe the wrong device, and
every path is token-bounded, so responses stay small by construction.
"""

from __future__ import annotations

import re
import threading
from dataclasses import dataclass, field

from .index.registers import RegisterStore
from .index.pins import PinStore
from .cards import render_card, render_bit
from .router import classify_query
from . import tokens


@dataclass
class DSResult:
    text: str
    kind: str   # "register" | "bit" | "search" | "operation" | "pin" | "graph" | "miss"
    part: str
    n_tokens: int = 0
    sources: list[str] = field(default_factory=list)
    truncated: bool = False

    def __str__(self) -> str:
        return self.text


class DS:
    """Facade over the Qdrant register/pin stores, prose index, and graph."""

    def __init__(self, *, with_prose: bool = True):
        self.store = RegisterStore()
        self.pins = PinStore()
        self._with_prose = with_prose
        self._prose = None
        self._prose_lock = threading.Lock()
        self._prose_loading = False
        self._graph = None
        self._graph_lock = threading.Lock()

    # ── lazy prose index ──────────────────────────────────────────────────

    @property
    def prose(self):
        if self._prose is None and self._with_prose:
            with self._prose_lock:
                if self._prose is None:
                    from .index.prose import ProseIndex
                    self._prose = ProseIndex()
        return self._prose

    def _trigger_prose_load(self) -> None:
        if self._prose is not None or not self._with_prose:
            return
        with self._prose_lock:
            if self._prose is not None or self._prose_loading:
                return
            self._prose_loading = True

        def _load():
            try:
                from .index.prose import ProseIndex
                prose = ProseIndex()
                with self._prose_lock:
                    self._prose = prose
            except Exception:
                pass
            finally:
                self._prose_loading = False

        threading.Thread(target=_load, daemon=True, name="ds-prose-load").start()

    @property
    def _graph_store(self):
        if self._graph is None:
            with self._graph_lock:
                if self._graph is None:
                    from .graph.store import GraphStore
                    self._graph = GraphStore()
        return self._graph

    # ── 1. exact register ─────────────────────────────────────────────────

    def lookup_register(
        self, part: str, register: str, *, block: str | None = None,
        bits: bool = True, max_tokens: int = 1200,
    ) -> DSResult:
        cards = self.store.get_register(part, register, block)
        if not cards:
            return self._suggest(part, register)

        rendered = [render_card(c, bits=bits) for c in cards]
        if len(cards) > 1:
            header = (
                f"⚠️ '{register}' exists in {len(cards)} blocks on {part}: "
                f"{', '.join(c.block for c in cards)}. Showing all:\n"
            )
            body, n = tokens.pack(rendered, max_tokens - tokens.count(header))
            text = header + body
            return DSResult(text=text, kind="register", part=part,
                            n_tokens=tokens.count(text), sources=[c.key for c in cards])
        text, truncated = tokens.truncate_to(rendered[0], max_tokens)
        return DSResult(text=text, kind="register", part=part,
                        n_tokens=tokens.count(text), sources=[cards[0].key],
                        truncated=truncated)

    # ── 2. single bit ─────────────────────────────────────────────────────

    def lookup_bit(self, part: str, register: str, bit: str) -> DSResult:
        cards = self.store.get_register(part, register)
        if not cards:
            return self._suggest(part, register)

        def _stem(s: str) -> str:
            return re.sub(r"\[.*?\]", "", s).replace(" ", "").upper()

        want = _stem(bit)
        lines = []
        for c in cards:
            for bf in c.bitfields:
                if _stem(bf.symbol) == want or _stem(bf.bits) == want:
                    lines.append(render_bit(c, bf))
        if not lines:
            avail = ", ".join(
                bf.symbol for c in cards for bf in c.bitfields if not bf.reserved
            )
            return DSResult(text=f"No bit '{bit}' in {part}/{register}. Bits: {avail}",
                            kind="miss", part=part)
        text = "\n".join(lines)
        return DSResult(text=text, kind="bit", part=part,
                        n_tokens=tokens.count(text), sources=[c.key for c in cards])

    # ── 3. hybrid search ──────────────────────────────────────────────────

    def search(
        self, part: str, query: str, *, block: str | None = None,
        k: int = 5, max_tokens: int = 1500,
    ) -> DSResult:
        reg_hits = self.store.search_registers(part, query, limit=3)
        blocks: list[str] = []
        sources: list[str] = []
        for c in reg_hits:
            blocks.append(render_card(c, bits=False))
            sources.append(c.key)

        prose = self._prose
        if prose is not None:
            if block:
                prose_results = prose.search(part, query, block=block, k=k)
            else:
                prose_results = prose.search_groups(part, query, k=k, group_size=2)
            for r in prose_results:
                blocks.append(f"[{r['breadcrumb']}]\n{r['text']}")
                sources.append(f"{r['part']}/{r['block']}::{r['heading']}")
        elif self._with_prose:
            self._trigger_prose_load()
            blocks.append(
                "_(Embedding model is still loading — semantic results will appear "
                "after ~10 s. The register hits above are complete.)_"
            )

        if not blocks:
            return DSResult(text=f"No results for '{query}' on {part}.", kind="miss", part=part)

        text, n = tokens.pack(blocks, max_tokens)
        result = DSResult(text=text, kind="search", part=part,
                          n_tokens=tokens.count(text), sources=sources[:n],
                          truncated=n < len(blocks))
        return self._enrich_with_dep_hint(result, part)

    def _enrich_with_dep_hint(self, result: DSResult, part: str) -> DSResult:
        """Append a 'Depends on:' footer using one lightweight graph query."""
        if result.kind == "miss" or not result.sources:
            return result

        # Parse block name from the top source.
        #   Register key: VENDOR/PART/BLOCK/REGISTER  (len >= 4)
        #   Prose   key:  PART/BLOCK::heading          (len == 2)
        blk: str | None = None
        for src in result.sources:
            parts = src.split("/")
            if len(parts) >= 4:
                blk = parts[2]
                break
            if len(parts) >= 2:
                blk = parts[1].split("::")[0]
                break
        if not blk:
            return result

        from .graph.build import block_id
        try:
            edges = self._graph_store.get_neighbors(
                part, block_id(part, blk), direction="out",
                edge_types=["BLOCK_DEPENDS_ON"], limit=10,
            )
        except Exception:
            return result
        if not edges:
            return result

        dep_names = sorted({e.target_id.split("/")[-1] for e in edges})
        footer = f"\n\nDepends on: {', '.join(dep_names)}"
        return DSResult(text=result.text + footer, kind=result.kind, part=result.part,
                        n_tokens=result.n_tokens + tokens.count(footer),
                        sources=result.sources, truncated=result.truncated)

    # ── 4. operation sections ─────────────────────────────────────────────

    def get_operation(self, part: str, block: str | None = None, *, max_tokens: int = 2000) -> DSResult:
        rows = self.store.get_operation(part, block)
        if not rows and block:
            rows = self.store.get_operation(part, None)   # fall back to all op sections
        if not rows:
            parts = [p for _, p, _ in self.store.list_parts()]
            if part not in parts:
                return DSResult(text=f"Unknown part '{part}'. Indexed parts: {', '.join(parts)}.",
                                kind="miss", part=part)
            avail = self.store.list_operation_blocks(part)
            msg = f"No operation/procedure sections found for {part}"
            msg += f" / {block}." if block else "."
            if avail:
                msg += f"\nBlocks with operation sections: {', '.join(avail)}"
            return DSResult(text=msg, kind="miss", part=part)

        blocks = [f"[{r['breadcrumb']}]\n{r['text']}" for r in rows]
        text, n = tokens.pack(blocks, max_tokens)
        return DSResult(text=text, kind="operation", part=part,
                        n_tokens=tokens.count(text),
                        sources=[r["breadcrumb"] for r in rows[:n]],
                        truncated=n < len(blocks))

    # ── 5. pin finder ─────────────────────────────────────────────────────

    def find_pin(self, part: str, *, block: str | None = None,
                 signal: str | None = None, max_tokens: int = 2000) -> DSResult:
        rows = self.pins.find_pins(part, block=block, signal=signal)
        if not rows:
            parts = [p for _, p, _ in self.store.list_parts()]
            if part not in parts:
                return DSResult(text=f"Unknown part '{part}'. Indexed parts: {', '.join(parts)}.",
                                kind="miss", part=part)
            avail = self.pins.list_pin_blocks(part)
            msg = f"No pin data for {part}"
            msg += f" / {block or signal}." if (block or signal) else "."
            if avail:
                msg += f"\nBlocks with pin data: {', '.join(avail)}"
            else:
                msg += f"\nNo pin data indexed for {part}. Re-run: build.bat --part {part}"
            return DSResult(text=msg, kind="miss", part=part)

        lines = [f"Pin / pad assignments for {part}"
                 + (f" (block={block})" if block else "") + ":\n"]
        current_block = None
        for r in rows:
            if r["block"] != current_block:
                current_block = r["block"]
                lines.append(f"\n{current_block or 'GENERAL'}:")
            typ = f"[{r['type']}]" if r["type"] else ""
            desc = f"  {r['description']}" if r["description"] else ""
            lines.append(f"  {r['pin']:<6} {r['signal']:<12} {typ:<8}{desc}".rstrip())
        text, n = tokens.pack(lines, max_tokens)
        return DSResult(text=text, kind="pin", part=part, n_tokens=tokens.count(text),
                        sources=[f"{part}/PINS"], truncated=n < len(lines))

    # ── 6. auto-routing ───────────────────────────────────────────────────

    def auto(self, part: str, query: str, *, block: str | None = None,
             k: int = 5, max_tokens: int = 1500) -> DSResult:
        route, kw = classify_query(query, block)
        if route == "operation":
            return self.get_operation(part, kw.get("block"))
        if route == "pin":
            return self.find_pin(part, block=kw.get("block"))
        if route == "register":
            return self.lookup_register(part, kw["register"], block=block, max_tokens=max_tokens)
        if route == "bit":
            return self.lookup_bit(part, kw["register"], kw["bit"])
        return self.search(part, query, block=block, k=k, max_tokens=max_tokens)

    # ── 7. graph dependency query ─────────────────────────────────────────

    def neighbors(self, part: str, node: str, *, depth: int = 2) -> DSResult:
        from .graph.query import dependency_summary
        from .graph.build import block_id, reg_id

        parts = [p for _, p, _ in self.store.list_parts()]
        if part not in parts:
            return DSResult(text=f"Unknown part '{part}'. Indexed parts: {', '.join(parts)}.",
                            kind="miss", part=part)

        depth = max(1, min(depth, 3))
        node_up = node.upper()
        store = self._graph_store

        # If a full "PART/BLOCK[/REG]" path was given, use it directly.
        if "/" in node_up:
            node_id = node_up
        else:
            # Try as a block first, then resolve as a register symbol.
            b_id = block_id(part, node_up)
            if store.get_neighbors(part, b_id, direction="both", limit=1):
                node_id = b_id
            else:
                cards = self.store.get_register(part, node)
                if cards:
                    node_id = reg_id(part, cards[0].block, cards[0].register)
                else:
                    node_id = b_id

        text = dependency_summary(store, part, node_id, depth=depth)
        return DSResult(text=text, kind="graph", part=part,
                        n_tokens=tokens.count(text), sources=[node_id])

    # ── catalog ───────────────────────────────────────────────────────────

    def list_parts(self) -> DSResult:
        rows = self.store.list_parts()
        if not rows:
            return DSResult(text="No parts indexed. Run the ingestion pipeline + build.bat first.",
                            kind="miss", part="")
        lines = [f"{p}  ({v})" + (f"  {r}" if r else "") for v, p, r in rows]
        text = "\n".join(lines)
        return DSResult(text=text, kind="search", part="", n_tokens=tokens.count(text))

    def list_blocks(self, part: str) -> DSResult:
        bs = self.store.list_blocks(part)
        if not bs:
            return DSResult(
                text=f"No blocks indexed for '{part}'. "
                     f"Known parts: {', '.join(p for _, p, _ in self.store.list_parts())}",
                kind="miss", part=part)
        text = ", ".join(f"{b}({n})" for b, n in bs)
        return DSResult(text=text, kind="search", part=part, n_tokens=tokens.count(text))

    def _suggest(self, part: str, register: str) -> DSResult:
        hits = self.store.search_registers(part, register, limit=5)
        parts = [p for _, p, _ in self.store.list_parts()]
        if not hits and part not in parts:
            return DSResult(text=f"Unknown part '{part}'. Indexed parts: {', '.join(parts)}.",
                            kind="miss", part=part)
        sug = ", ".join(f"{c.block}/{c.register}" for c in hits)
        msg = f"No register '{register}' on {part}."
        if sug:
            msg += f" Did you mean: {sug}?"
        return DSResult(text=msg, kind="miss", part=part)

    def close(self):
        self.store.close()
        if self._graph is not None:
            self._graph.close()
