from __future__ import annotations

from ds import mcp_server
from ds.evidence.model import CatalogResponse, GetResponse, QueryResponse


class _FakeService:
    def catalog(self, **kwargs):
        return CatalogResponse(summary=f"catalog:{kwargs['part']}")

    def query(self, **kwargs):
        return QueryResponse(
            part=kwargs["part"], question=kwargs["question"],
            focus=kwargs["focus"], summary="packet",
        )

    def get(self, **kwargs):
        return GetResponse(
            part=kwargs["part"], target=kwargs["target"], summary="exact",
        )


def test_public_mcp_surface_is_exactly_three_tools():
    assert set(mcp_server.mcp._tool_manager._tools) == {
        "ds_catalog", "ds_query", "ds_get",
    }


def test_tools_return_structured_contracts_without_string_rendering(monkeypatch):
    monkeypatch.setattr(mcp_server, "_evidence_service", _FakeService())

    catalog = mcp_server.ds_catalog("MX25LM51245G")
    packet = mcp_server.ds_query(
        "MX25LM51245G", "How to configure SPI?", focus="configure")
    exact = mcp_server.ds_get("MX25LM51245G", "DC[2:0]")

    assert isinstance(catalog, CatalogResponse)
    assert isinstance(packet, QueryResponse)
    assert packet.focus == "configure"
    assert isinstance(exact, GetResponse)
