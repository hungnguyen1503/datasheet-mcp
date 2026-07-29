"""Datasheet MCP server — exposes component datasheets to agents as
token-bounded tools.

TOOL ROUTING POLICY (read before choosing a tool):

  ds_auto            → USE THIS when unsure which tool to call. Single entry
                        point that routes internally: register names, procedural
                        questions, pin assignments, spec lookups, ordering info,
                        and general queries are all dispatched automatically to
                        the correct backend.

  ds_search          → DEFAULT for any conceptual / value question:
                        supply voltage, sensitivity, package, bandwidth, overview,
                        spec tables, and dependency questions ("what enables X?").
                        Set content_type="operation" for init/procedure sections,
                        content_type="spec" for electrical/timing specs,
                        content_type="order" for ordering/part-number info.

  ds_lookup_register → ONLY when user explicitly names a register symbol OR a
                        bit/flag. Omit `bit` for full card; supply `bit` for one row.

  ds_find_pin        → ONLY for pin/pad questions:
                        "which pin is SDA?", "pinout", "CS signal".

  ds_neighbors       → dependency graph around a block or register node.

  ds_list            → ONLY when user explicitly asks for a list:
                        omit `part` to list all indexed parts;
                        supply `part` to list that part's functional blocks.

GLOBAL RULES:
  1. Use exactly ONE tool per query. One call, then stop.
  2. Do NOT chain ds_search + ds_lookup_register + ds_search(content_type="operation").
  3. Never call ds_list automatically — only when the user explicitly asks.

Run (stdio):  python mcp/server.py
Run (HTTP):   DS_TRANSPORT=streamable-http python mcp/server.py
"""

from __future__ import annotations

import os
import logging

from mcp.server.fastmcp import FastMCP
from mcp.server.auth.provider import TokenVerifier, AccessToken
from mcp.server.auth.settings import AuthSettings
from mcp.server.transport_security import TransportSecuritySettings

from .query import DS, _build_footer

_log = logging.getLogger(__name__)


class _StaticBearerTokenVerifier(TokenVerifier):
    """Accepts any token present in the DS_API_KEYS env var (comma-separated).

    When DS_API_KEYS is unset or empty the verifier is not installed and the
    server runs in open mode (suitable for local / dev use).
    """

    def __init__(self, tokens: set[str]) -> None:
        self._tokens = tokens

    async def verify_token(self, token: str) -> AccessToken | None:
        if token in self._tokens:
            return AccessToken(token=token, client_id="ds-client", scopes=[])
        return None


_transport = os.environ.get("DS_TRANSPORT", "streamable-http")
_host = os.environ.get("DS_HOST", "0.0.0.0")
_port = int(os.environ.get("DS_PORT", "8060"))

_raw_keys = os.environ.get("DS_API_KEYS", "")
_api_keys = {t.strip() for t in _raw_keys.split(",") if t.strip()}

if _api_keys:
    _server_url = os.environ.get("DS_SERVER_URL", "https://datasheetmcp.hungnguyenjx.space")
    _auth_settings: AuthSettings | None = AuthSettings(
        issuer_url=_server_url, resource_server_url=_server_url,
    )
    _token_verifier: TokenVerifier | None = _StaticBearerTokenVerifier(_api_keys)
else:
    _auth_settings = None
    _token_verifier = None

# Transport security — allow the cloudflared domain + localhost
def _transport_security() -> TransportSecuritySettings:
    from urllib.parse import urlparse
    hosts = {"localhost", "127.0.0.1", f"localhost:{_port}", f"127.0.0.1:{_port}"}
    origins: set[str] = {f"http://localhost:{_port}", f"http://127.0.0.1:{_port}"}
    su = os.environ.get("DS_SERVER_URL", "").strip()
    if su:
        p = urlparse(su)
        if p.hostname:
            hosts.add(p.hostname)
            hosts.add(p.netloc)
            origins.add(f"{p.scheme}://{p.netloc}")
    extra = os.environ.get("DS_ALLOWED_HOSTS", "").strip()
    if extra == "*":
        return TransportSecuritySettings(enable_dns_rebinding_protection=False)
    for h in extra.split(","):
        if h.strip():
            hosts.add(h.strip())
    return TransportSecuritySettings(
        allowed_hosts=sorted(hosts), allowed_origins=sorted(origins))

mcp = FastMCP(
    "ds",
    host=_host,
    port=_port,
    auth=_auth_settings,
    token_verifier=_token_verifier,
    transport_security=_transport_security(),
    stateless_http=True,   # Each request is self-contained — no sessions needed.
)

# ASGI app for gunicorn/uvicorn (imported as ds.mcp_server:app)
app = mcp.streamable_http_app()

# ── Optional prefix middleware for future multi-tenant support ──────────
from .collections import set_prefix


class _PrefixMiddleware:
    """Set collection prefix from bearer token before each request (async-safe ContextVar)."""

    def __init__(self, asgi_app):
        self._app = asgi_app

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            headers = {}
            for k, v in scope.get("headers", []):
                headers[k] = v
            auth = headers.get(b"authorization", b"").decode("latin-1")
            token = auth.removeprefix("Bearer ").strip() if auth.lower().startswith("bearer ") else None
            # Currently all tokens map to empty prefix (single namespace).
            # Future: route specific tokens to "nightly_" prefix.
            set_prefix("")
        await self._app(scope, receive, send)


app.add_middleware(_PrefixMiddleware)

# ── Singleton DS instance ────────────────────────────────────────────────

_ds: DS | None = None


def _d() -> DS:
    global _ds
    if _ds is None:
        _ds = DS()
    return _ds


def _fmt(r) -> str:
    """Wrap a DSResult with a structured metadata footer for agent self-audit.

    For plain strings (ds_list), returns the string unchanged.
    """
    if not hasattr(r, "kind"):
        return str(r)
    return r.text + r.footer


# ── Tool 1: catalog listing (merged ds_list_parts + ds_list_blocks) ───────

@mcp.tool()
def ds_list(part: str = "") -> str:
    """Catalog lookup — two modes controlled by whether `part` is supplied.

    Mode 1 — part omitted (or ""):
      Returns all indexed parts with vendor and revision.
      Call when user asks: "what datasheets are indexed?",
      "what parts do you have?", "list all parts".

    Mode 2 — part supplied:
      Returns functional blocks + register counts for that part.
      Call when user asks: "what blocks does ADXL345 have?",
      "list ADXL345 sections", "what's in OV7670?".

    Call ONLY when user explicitly asks for a list.
    Never call automatically before a search or lookup — the part
    name is always provided by the user in those contexts.

    Args:
        part: Part name to list blocks for, e.g. "ADXL345".
              Omit (or pass "") to list all indexed parts.
    """
    d = _d()
    if not part:
        return d.list_parts().text
    return d.list_blocks(part).text


# ── Tool 2: register lookup ──────────────────────────────────────────────

@mcp.tool()
def ds_lookup_register(
    part: str, register: str, block: str = "", bit: str = "", bits: bool = True
) -> str:
    """Look up a register definition or a single bit/flag within a register.

    Use ONLY when the user explicitly names a register symbol or a bit/flag.

    Mode 1 — Full register (bit omitted):
      Returns addresses + all bit fields for the register.
      Examples: "explain POWER_CTL", "show FIFO_CTL bits", "DATA_FORMAT register"

    Mode 2 — Single bit (bit supplied):
      Returns only the one bit/flag row — smallest possible answer.
      Examples: "what is the MEASURE bit?", "FULL_RES flag", "RANGE field"

    Do NOT use for value/overview questions (→ ds_search), procedures
    (→ ds_search with content_type="operation"), or pins (→ ds_find_pin).

    Args:
        part: Part name, e.g. "ADXL345". Required.
        register: Register symbol, e.g. "POWER_CTL". Required.
        block: Optional — disambiguates if symbol exists in multiple blocks.
        bit: Optional — bit symbol/name. Supply to get one bit instead of card.
        bits: Set False for just the header line. Ignored when bit is set.
    """
    d = _d()
    if bit:
        return _fmt(d.lookup_bit(part, register, bit))
    return _fmt(d.lookup_register(part, register, block=block or None, bits=bits))


# ── Tool 3: hybrid search + operation + spec + order ─────────────────────

@mcp.tool()
def ds_search(
    part: str,
    query: str,
    block: str = "",
    k: int = 5,
    max_tokens: int = 1500,
    content_type: str = "",
) -> str:
    """Hybrid semantic + BM25 search with optional content-type filtering.

    Mode 1 — content_type="" (default): All content types.
      Semantic + BM25 hybrid over all prose + register names.
      Use for ALL conceptual / value questions:
        - Electrical: "supply voltage range", "operating current"
        - Specs: "I2C address", "output data rates", "FIFO modes overview"
        - Features: "what does the MEASURE bit do", "self-test feature"
      Results include a "Depends on:" footer when graph edges exist.

    Mode 2 — content_type="operation":
      Returns initialization / procedure sections in document order.
      Use for HOW-TO questions:
        "how to configure FIFO", "startup sequence", "power-up procedure",
        "enable measurement mode", "SPI initialization steps".

    Mode 3 — content_type="spec":
      Returns electrical specifications, timing characteristics, ratings.
      Use for spec/parameter questions:
        "supply voltage range", "operating current", "timing parameters",
        "absolute maximum ratings", "DC characteristics".

    Mode 4 — content_type="order":
      Returns ordering information, part numbers, package codes.
      Use for ordering/part-number questions:
        "available part numbers", "package options", "ordering codes",
        "temperature grades", "how to order".

    This tool is SUFFICIENT on its own for these questions.
    Do NOT also call ds_lookup_register for the same question.

    Prefer ds_auto over calling this directly.

    Args:
        part: Part name, e.g. "ADXL345". Required.
        query: Natural-language question or keywords.
        block: Optional block filter, e.g. "FIFO".
        k: Number of prose passages (default 5).
        max_tokens: Output token budget (default 1500).
        content_type: "" (all) | "operation" | "spec" | "order".
    """
    d = _d()
    ct = content_type.strip().lower() if content_type else None
    if ct == "operation":
        return _fmt(d.get_operation(part, block or None, max_tokens=max_tokens))
    if ct == "spec":
        return _fmt(d.search_spec(part, query, block=block or None, k=k, max_tokens=max_tokens))
    if ct == "order":
        return _fmt(d.search_order(part, query, block=block or None, k=k, max_tokens=max_tokens))
    return _fmt(d.search(part, query, block=block or None, k=k, max_tokens=max_tokens))


# ── Tool 4: pin finder ───────────────────────────────────────────────────

@mcp.tool()
def ds_find_pin(part: str, block: str = "", signal: str = "") -> str:
    """Find pin / pad assignments for a part.

    Use ONLY for pin/pad questions:
      "which pin is SDA?", "pinout", "package pins", "serial interface pins",
      "what is the CS pin?", "show all power pins".

    Args:
        part: Part name, e.g. "ADXL345". Required.
        block: Optional — narrow to one functional block.
        signal: Optional — narrow to one signal/pad name, e.g. "SDA".
    """
    return _fmt(_d().find_pin(part, block=block or None, signal=signal or None))


# ── Tool 5: dependency graph ─────────────────────────────────────────────

@mcp.tool()
def ds_neighbors(part: str, node: str, depth: int = 2) -> str:
    """Dependency-graph neighborhood around a block or register node.

    Returns prerequisites, registers in a block, bit fields, pins, and prose
    back-links. Accepts a short name (block or register symbol) or a full
    "PART/BLOCK/REGISTER" node path.

    Args:
        part: Part name, e.g. "ADXL345". Required.
        node: Block name ("FIFO"), register symbol ("POWER_CTL"), or node path.
        depth: Traversal depth, 1–3 (default 2).
    """
    return _fmt(_d().neighbors(part, node, depth=depth))


# ── Tool 6: auto-router ──────────────────────────────────────────────────

@mcp.tool()
def ds_auto(part: str, query: str, block: str = "") -> str:
    """Single-entry auto-routing tool — use this when unsure which ds tool to call.

    Analyzes `query` and dispatches internally to the most appropriate backend:
    • Procedural question ("how to configure FIFO") → operation procedure
    • Pin question ("which pin is SDA") → pin/pad table
    • Spec question ("supply voltage", "operating current") → spec search
    • Ordering question ("part number", "package options") → order search
    • Named register/bit ("POWER_CTL", "MEASURE bit") → exact register card
    • Everything else → hybrid semantic + BM25 search

    Args:
        part: Part name (e.g. "ADXL345", "OV7670"). Required.
        query: Natural-language question, register/bit name, or keyword phrase.
        block: Optional — narrows scope / resolves the target block for
               operation and pin routes when it cannot be extracted from the query.
    """
    return _fmt(_d().auto(part, query, block=block or None))


# ── Main ─────────────────────────────────────────────────────────────────

def main() -> None:
    _prewarm()
    # When running under gunicorn, the ASGI app (app) is served directly.
    # Skip mcp.run() so the module-level startup runs but gunicorn owns the loop.
    if os.environ.get("GUNICORN_RUN"):
        return
    mcp.run(transport=_transport)


def _prewarm() -> None:
    """Pre-load heavy models so the first user request pays zero latency.

    Runs before mcp.run(). Each step is wrapped so a failure never
    prevents the server from starting (HUM pattern).
    """
    # 1. BGE dense embedder (~430 MB, 3-5s on CPU, 0.5s on GPU).
    try:
        d = _d()
        _ = d.store.embedder   # triggers Embedder init
        _log.info("prewarm: embedder + register store ready")
    except Exception as exc:
        _log.warning("prewarm: embedder failed to load (%s)", exc)

    # 2. Prose index — load synchronously (BM25 sparse model + Qdrant check).
    try:
        _ = _d().prose
        _log.info("prewarm: prose index ready")
    except Exception as exc:
        _log.warning("prewarm: prose index failed to load (%s)", exc)

    # 3. Cross-encoder reranker (~24 MB, 1-3s on first load).
    try:
        from . import reranker as _rr
        _rr._load_reranker()
        _log.info("prewarm: reranker ready")
    except Exception as exc:
        _log.warning("prewarm: reranker failed to load (%s)", exc)


if __name__ == "__main__":
    main()
