"""Datasheet MCP server — exposes component datasheets to agents as
token-bounded tools.

TOOL ROUTING POLICY (read before choosing a tool):

  ds_auto            → USE THIS when unsure which tool to call. Single entry
                        point that routes internally: register names, procedural
                        questions, pin assignments, and general queries are all
                        dispatched automatically to the correct backend.

  ds_search          → DEFAULT for any conceptual / value question:
                        supply voltage, current, sensitivity, package, ranges,
                        feature overviews, spec tables, dependency questions.
                        Results include a "Depends on:" footer when graph edges exist.

  ds_lookup_register → ONLY when the user explicitly names a register symbol OR a
                        bit/flag. Omit `bit` for the full register card; supply
                        `bit` for one bit row.

  ds_get_operation   → ONLY for procedural / how-to questions:
                        "how to configure the FIFO", "initialization sequence".

  ds_find_pin        → ONLY for pin/pad questions:
                        "which pin is SDA?", "pinout", "package pins".

  ds_neighbors       → dependency graph around a block or register node.

  ds_list_parts      → ONLY when the user explicitly asks which parts are indexed.
  ds_list_blocks     → ONLY when the user explicitly asks for a part's blocks.

GLOBAL RULES:
  1. Use exactly ONE tool per query. One call, then stop.
  2. Do NOT chain ds_search + ds_lookup_register + ds_get_operation together.
  3. Never call ds_list_parts or ds_list_blocks automatically.

Run (stdio):  python -m ds.mcp_server   (or  python mcp/server.py)
Run (HTTP):   DS_TRANSPORT=streamable-http python -m ds.mcp_server
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


@mcp.tool()
def ds_list_parts() -> str:
    """List the datasheet parts currently indexed.

    Call ONLY when the user explicitly asks which parts/devices are supported
    (e.g. "what datasheets do you have?"). The part name is otherwise always
    provided by the user.
    """
    return _d().list_parts().text


@mcp.tool()
def ds_list_blocks(part: str) -> str:
    """List the functional blocks (with register counts) for one part.

    Call ONLY when the user explicitly asks which blocks a part has.

    Args:
        part: Part name, e.g. "ADXL345". Required.
    """
    return _d().list_blocks(part).text


@mcp.tool()
def ds_lookup_register(
    part: str, register: str, block: str = "", bit: str = "", bits: bool = True
) -> str:
    """Look up a register definition or a single bit/flag within a register.

    Use ONLY when the user explicitly names a register symbol or a bit/flag.

    Mode 1 — Full register (bit omitted): addresses + all bit fields.
      Examples: "Explain POWER_CTL", "Show FIFO_CTL bits", "DATA_FORMAT register"
    Mode 2 — Single bit (bit supplied): only the one bit/flag row.
      Examples: "What is the MEASURE bit?", "FULL_RES flag", "RANGE field"

    Do NOT use for value/overview questions (→ ds_search), procedures
    (→ ds_get_operation), or pins (→ ds_find_pin).

    Args:
        part: Part name, e.g. "ADXL345". Required.
        register: Register symbol, e.g. "POWER_CTL". Required.
        block: Optional — disambiguates if the symbol exists in multiple blocks.
        bit: Optional — bit symbol/name. Supply to get one bit instead of the card.
        bits: Set False for just the header line (address + name). Ignored when bit is set.
    """
    d = _d()
    if bit:
        return d.lookup_bit(part, register, bit).text
    return d.lookup_register(part, register, block=block or None, bits=bits).text


@mcp.tool()
def ds_search(part: str, query: str, block: str = "", k: int = 5) -> str:
    """Semantic + BM25 hybrid search — the DEFAULT tool for most questions.

    Use for ALL conceptual / value questions:
      - Electrical values: "supply voltage range", "operating current", "sensitivity"
      - Specs/features: "I2C address", "output data rates", "FIFO modes overview"
      - Mechanical: "package type", "pin count"
      - Dependency questions: results add a "Depends on:" footer when edges exist.

    This tool is SUFFICIENT on its own. Do NOT also call ds_lookup_register or
    ds_get_operation for the same question.

    Args:
        part: Part name, e.g. "ADXL345". Required.
        query: Natural-language question or keywords.
        block: Optional — narrows results to one functional block.
        k: Number of prose passages to retrieve (default 5).
    """
    return _d().search(part, query, block=block or None, k=k).text


@mcp.tool()
def ds_get_operation(part: str, block: str = "") -> str:
    """Retrieve the operating procedure / initialization sequence.

    Use ONLY for procedural / how-to questions:
      "how to configure the FIFO", "initialization sequence", "power-up procedure",
      "how to enable measurement mode".

    Args:
        part: Part name, e.g. "ADXL345". Required.
        block: Optional functional block, e.g. "FIFO". Omit to get all operation
               sections for the part.
    """
    return _d().get_operation(part, block or None).text


@mcp.tool()
def ds_find_pin(part: str, block: str = "", signal: str = "") -> str:
    """Find pin / pad assignments for a part.

    Use ONLY for pin/pad questions:
      "which pin is SDA?", "pinout", "package pins", "serial interface pins".

    Args:
        part: Part name, e.g. "ADXL345". Required.
        block: Optional — narrow to one functional block.
        signal: Optional — narrow to one signal/pad name, e.g. "SDA".
    """
    return _d().find_pin(part, block=block or None, signal=signal or None).text


@mcp.tool()
def ds_neighbors(part: str, node: str, depth: int = 2) -> str:
    """Dependency-graph neighborhood around a block or register node.

    Returns prerequisites, registers in a block, bit fields, pins, and prose
    back-links. Accepts a short name (block or register symbol) or a full
    "PART/BLOCK/REGISTER" node path.

    Args:
        part: Part name, e.g. "ADXL345". Required.
        node: A block name ("FIFO"), register symbol ("POWER_CTL"), or node path.
        depth: Traversal depth, 1-3 (default 2).
    """
    return _d().neighbors(part, node, depth=depth).text


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
