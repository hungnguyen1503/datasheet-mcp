"""Qdrant collection-name prefix for Datasheet MCP.

Every collection name is prefixed with DS_COLLECTION_PREFIX (default "").
Both environments (stable / nightly) share ONE Qdrant instance; isolation is by
collection namespace.

Runtime switching: use set_prefix() / get_prefix() backed by a ContextVar
(async-safe per-request).
"""

import os
from contextvars import ContextVar

_prefix: ContextVar[str] = ContextVar(
    "ds_prefix", default=os.environ.get("DS_COLLECTION_PREFIX", "")
)


def get_prefix() -> str:
    """Return the current collection prefix (async-safe per-request)."""
    return _prefix.get()


def set_prefix(p: str) -> None:
    """Set the collection prefix for the current async context."""
    _prefix.set(p)
    os.environ["DS_COLLECTION_PREFIX"] = p  # backward compat for batch use


PREFIX = os.environ.get("DS_COLLECTION_PREFIX", "")

REG_COLLECTION   = f"{PREFIX}ds_registers"
PIN_COLLECTION   = f"{PREFIX}ds_pins"
CATALOG_COLLECTION = f"{PREFIX}ds_catalog"
PROSE_COLLECTION = f"{PREFIX}ds_prose"
GRAPH_COLLECTION = f"{PREFIX}ds_graph"
