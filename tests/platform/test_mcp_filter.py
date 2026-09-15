import pytest

from alephat_platform.harness import mcp_runtime
from alephat_platform.harness.mcp_runtime import connect_mcp_servers, filter_tools


def test_allowlist():
    assert filter_tools(["a", "b", "c"], allowlist=["a", "c"], denylist=[]) == ["a", "c"]


def test_denylist():
    assert filter_tools(["a", "b"], allowlist=[], denylist=["b"]) == ["a"]


def test_both():
    assert filter_tools(["a", "b", "c"], allowlist=["a", "b"], denylist=["b"]) == ["a"]


@pytest.mark.asyncio
async def test_connected_tool_invokes_underlying_langchain_tool(monkeypatch):
    calls = []

    class FakeTool:
        name = "search"
        description = "Search internal docs"

        async def ainvoke(self, args):
            calls.append(args)
            return {"answer": "found"}

    async def load_tools(_config):
        return [FakeTool()]

    monkeypatch.setattr(mcp_runtime, "_load_tools", load_tools)
    sessions = await connect_mcp_servers(
        [{"mcp_server_id": "server-1", "name": "docs", "transport": "sse"}]
    )
    assert sessions[0].ok
    assert sessions[0].tools[0].namespaced == "mcp_docs_search"
    handler = sessions[0].tools[0].handler
    assert handler is not None
    assert await handler(query="leases") == '{"answer": "found"}'
    assert calls == [{"query": "leases"}]
