"""Datasheet MCP server — source-linked implementation evidence for agents.

The public surface is deliberately small: ``ds_catalog`` discovers indexed
parts and coverage, ``ds_query`` assembles an implementation packet, and
``ds_get`` resolves one exact entity plus graph context.  Every evidence query
is part-scoped.

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

from .evidence.model import CatalogResponse, GetResponse, QueryResponse

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

# ── Evidence service and public tools ────────────────────────────────────

_evidence_service = None


def _service():
    global _evidence_service
    if _evidence_service is None:
        from .evidence.service import DatasheetService
        _evidence_service = DatasheetService()
    return _evidence_service


@mcp.tool()
def ds_catalog(part: str = "", cursor: str = "", limit: int = 200) -> CatalogResponse:
    """List indexed parts or inspect one part's hierarchy and extraction coverage.

    Use without ``part`` only for explicit discovery.  With ``part``, use this
    before selecting an exact entity when document structure or extraction gaps
    matter.  ``limit`` is bounded by the service and ``cursor`` continues an
    outline page.
    """
    return _service().catalog(part=part, cursor=cursor, limit=limit)


@mcp.tool()
def ds_query(
    part: str,
    question: str,
    focus: str = "auto",
    max_tokens: int = 3000,
) -> QueryResponse:
    """Return one source-linked implementation packet for an embedded task.

    Use for configuration, exact values, timing, modes, commands, and operation
    flows. ``focus`` may be ``auto``, ``configure``, ``exact``, ``operation``,
    ``timing``, or ``explain``.  The result explicitly reports normalized MCU
    settings, ordered steps, evidence, constraints, relations, sources, gaps,
    conflicts, coverage, confidence, and truncation.
    """
    return _service().query(
        part=part, question=question, focus=focus, max_tokens=max_tokens)


@mcp.tool()
def ds_get(
    part: str,
    target: str,
    relation_depth: int = 1,
    cursor: str = "",
    limit: int = 100,
) -> GetResponse:
    """Resolve an exact ds:// ID or symbol and return lossless evidence.

    Use for a named register, bitfield, command, mode, operation, table, figure,
    parameter, or memory region.  Graph traversal is bounded to depth 0-2 and
    never crosses the requested part.
    """
    return _service().get(
        part=part,
        target=target,
        relation_depth=relation_depth,
        cursor=cursor,
        limit=limit,
    )


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
    # One canonical 768-d embedder and its evidence collections.
    try:
        _service().prewarm()
        _log.info("prewarm: evidence store ready")
    except Exception as exc:
        _log.warning("prewarm: evidence store failed to load (%s)", exc)


if __name__ == "__main__":
    main()
