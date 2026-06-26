"""Datasheet MCP server — exposes component datasheets to agents as
token-bounded tools.

TOOL ROUTING POLICY (read before choosing a tool):

  ds_auto            → USE THIS when unsure which tool to call. Single entry
                        point that routes internally: register names, procedural
                        questions, pin assignments, and general queries are all
                        dispatched automatically to the correct backend.

  ds_search          → DEFAULT for any conceptual / value question:
                        supply voltage, sensitivity, package, bandwidth, overview,
                        spec tables, and dependency questions ("what enables X?").
                        Set operation_only=True for init/procedure sections instead.

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
  2. Do NOT chain ds_search + ds_lookup_register + ds_search(operation_only=True).
  3. Never call ds_list automatically — only when the user explicitly asks.

Run (stdio):  python mcp/server.py
Run (HTTP):   DS_TRANSPORT=streamable-http python mcp/server.py
"""

from __future__ import annotations

import os

from mcp.server.fastmcp import FastMCP
from mcp.server.auth.provider import TokenVerifier, AccessToken
from mcp.server.auth.settings import AuthSettings

from .query import DS


class _StaticBearerTokenVerifier(TokenVerifier):
    """Accepts any token present in the DS_API_KEYS env var (comma-separated)."""

    def __init__(self, tokens: set[str]) -> None:
        self._tokens = tokens

    async def verify_token(self, token: str) -> AccessToken | None:
        if token in self._tokens:
            return AccessToken(token=token, client_id="ds-client", scopes=[])
        return None


_transport = os.environ.get("DS_TRANSPORT", "stdio")
_host = os.environ.get("DS_HOST", "0.0.0.0")
_port = int(os.environ.get("DS_PORT", "8002"))

_raw_keys = os.environ.get("DS_API_KEYS", "")
_api_keys = {t.strip() for t in _raw_keys.split(",") if t.strip()}

if _api_keys:
    _server_url = os.environ.get("DS_SERVER_URL", "https://datasheetmcp.example.com")
    _auth_settings: AuthSettings | None = AuthSettings(
        issuer_url=_server_url, resource_server_url=_server_url,
    )
    _token_verifier: TokenVerifier | None = _StaticBearerTokenVerifier(_api_keys)
else:
    _auth_settings = None
    _token_verifier = None

mcp = FastMCP(
    "ds",
    host=_host,
    port=_port,
    auth=_auth_settings,
    token_verifier=_token_verifier,
)

_ds: DS | None = None


def _d() -> DS:
    global _ds
    if _ds is None:
        _ds = DS()
    return _ds


# ── Tool 1: catalog listing (merged ds_list_parts + ds_list_blocks) ───────────

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


# ── Tool 2: register lookup ────────────────────────────────────────────────────

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
    (→ ds_search with operation_only=True), or pins (→ ds_find_pin).

    Args:
        part: Part name, e.g. "ADXL345". Required.
        register: Register symbol, e.g. "POWER_CTL". Required.
        block: Optional — disambiguates if symbol exists in multiple blocks.
        bit: Optional — bit symbol/name. Supply to get one bit instead of card.
        bits: Set False for just the header line. Ignored when bit is set.
    """
    d = _d()
    if bit:
        return d.lookup_bit(part, register, bit).text
    return d.lookup_register(part, register, block=block or None, bits=bits).text


# ── Tool 3: hybrid search + operation (merged ds_search + ds_get_operation) ───

@mcp.tool()
def ds_search(
    part: str,
    query: str,
    block: str = "",
    k: int = 5,
    operation_only: bool = False,
) -> str:
    """Hybrid semantic + BM25 search — two modes.

    Mode 1 — Default (operation_only=False):
      Semantic + BM25 hybrid over all prose + register names.
      Use for ALL conceptual / value questions:
        - Electrical: "supply voltage range", "operating current"
        - Specs: "I2C address", "output data rates", "FIFO modes overview"
        - Features: "what does the MEASURE bit do", "self-test feature"
      Results include a "Depends on:" footer when graph edges exist.

    Mode 2 — operation_only=True:
      Returns initialization / procedure sections in document order.
      Use for HOW-TO questions:
        "how to configure FIFO", "startup sequence", "power-up procedure",
        "enable measurement mode", "SPI initialization steps".
      When set, `query` is ignored — `block` narrows scope instead.

    This tool is SUFFICIENT on its own for these questions.
    Do NOT also call ds_lookup_register or ds_search(operation_only=True)
    for the same question.

    Prefer ds_auto over calling this directly.

    Args:
        part: Part name, e.g. "ADXL345". Required.
        query: Natural-language question or keywords (ignored when operation_only=True).
        block: Optional block filter, e.g. "FIFO".
        k: Number of prose passages (default 5). Ignored when operation_only=True.
        operation_only: True → return ordered init/procedure sections (no vector search).
    """
    d = _d()
    if operation_only:
        return d.get_operation(part, block or None).text
    return d.search(part, query, block=block or None, k=k).text


# ── Tool 4: pin finder ─────────────────────────────────────────────────────────

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
    return _d().find_pin(part, block=block or None, signal=signal or None).text


# ── Tool 5: dependency graph ───────────────────────────────────────────────────

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
    return _d().neighbors(part, node, depth=depth).text


# ── Tool 6: auto-router ────────────────────────────────────────────────────────

@mcp.tool()
def ds_auto(part: str, query: str, block: str = "") -> str:
    """Single-entry auto-routing tool — use this when unsure which ds tool to call.

    Analyzes `query` and dispatches internally to the most appropriate backend:
    • Procedural question ("how to configure FIFO") → operation procedure
    • Pin question ("which pin is SDA") → pin/pad table
    • Named register/bit ("POWER_CTL", "MEASURE bit") → exact register card
    • Everything else → hybrid semantic + BM25 search

    Args:
        part: Part name (e.g. "ADXL345", "OV7670"). Required.
        query: Natural-language question, register/bit name, or keyword phrase.
        block: Optional — narrows scope / resolves the target block for
               operation and pin routes when it cannot be extracted from the query.
    """
    return _d().auto(part, query, block=block or None).text


def main() -> None:
    mcp.run(transport=_transport)


if __name__ == "__main__":
    main()
