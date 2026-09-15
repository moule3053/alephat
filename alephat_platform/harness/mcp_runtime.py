"""Load MCP tools from task snapshot (harness-side)."""

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from alephat_platform.common.config import get_settings
from alephat_platform.common.crypto import try_decrypt
from alephat_platform.common.network_security import validate_remote_url

logger = logging.getLogger(__name__)


@dataclass
class McpTool:
    server_id: str
    server_name: str
    tool_name: str
    description: str = ""
    handler: Callable[..., Awaitable[str]] | None = None
    langchain_tool: Any = None

    @property
    def namespaced(self) -> str:
        safe = self.server_name.replace("-", "_").replace(" ", "_")
        return f"mcp_{safe}_{self.tool_name}"


@dataclass
class McpSession:
    server_id: str
    name: str
    ok: bool
    tools: list[McpTool] = field(default_factory=list)
    error: str | None = None


def filter_tools(
    tool_names: list[str],
    *,
    allowlist: list[str],
    denylist: list[str],
) -> list[str]:
    names = list(tool_names)
    if allowlist:
        names = [n for n in names if n in allowlist]
    if denylist:
        names = [n for n in names if n not in denylist]
    return names


async def connect_mcp_servers(snapshot: list[dict[str, Any]]) -> list[McpSession]:
    """Connect configured MCP servers. fail_open for optional, tracked per session."""
    sessions: list[McpSession] = []
    for cfg in snapshot or []:
        server_id = str(cfg.get("mcp_server_id") or "")
        name = str(cfg.get("name") or "mcp")
        required = bool(cfg.get("required"))
        try:
            tools = await _load_tools(cfg)
            allow = list(cfg.get("tool_allowlist") or [])
            deny = list(cfg.get("tool_denylist") or [])
            filtered_names = filter_tools(
                [str(getattr(tool, "name", "")) for tool in tools],
                allowlist=allow,
                denylist=deny,
            )
            mcp_tools = []
            for tool in tools:
                if str(getattr(tool, "name", "")) not in filtered_names:
                    continue
                item = McpTool(
                    server_id=server_id,
                    server_name=name,
                    tool_name=str(tool.name),
                    description=str(getattr(tool, "description", "") or ""),
                    handler=_make_handler(tool),
                )
                item.langchain_tool = (
                    tool.model_copy(update={"name": item.namespaced})
                    if hasattr(tool, "model_copy")
                    else tool
                )
                mcp_tools.append(item)
            sessions.append(McpSession(server_id=server_id, name=name, ok=True, tools=mcp_tools))
        except Exception as exc:
            logger.exception("MCP connect failed for %s", name)
            sessions.append(
                McpSession(
                    server_id=server_id,
                    name=name,
                    ok=False,
                    error=str(exc),
                )
            )
            if required:
                raise
    return sessions


async def _load_tools(cfg: dict[str, Any]) -> list[Any]:
    """Connect to one MCP server and return callable LangChain tools."""
    from langchain_mcp_adapters.client import MultiServerMCPClient
    from langchain_mcp_adapters.sessions import (
        SSEConnection,
        StdioConnection,
        StreamableHttpConnection,
    )

    transport = cfg.get("transport")
    url = cfg.get("url")
    server_key = str(cfg.get("name") or "server")
    if transport in {"sse", "streamable_http"} and url:
        await validate_remote_url(
            str(url),
            allow_private=get_settings().mcp_allow_private_networks,
        )
        headers_json = try_decrypt(cfg.get("headers_ciphertext"))
        headers = json.loads(headers_json) if headers_json else {}
        auth = try_decrypt(cfg.get("auth_ciphertext"))
        if auth:
            headers.setdefault("Authorization", f"Bearer {auth}")
        if transport == "sse":
            connection = SSEConnection(
                url=str(url),
                transport="sse",
                headers=headers,
            )
        else:
            connection = StreamableHttpConnection(
                url=str(url),
                transport="streamable_http",
                headers=headers,
            )
        client = MultiServerMCPClient({server_key: connection})
        async with asyncio.timeout(30):
            tools = await client.get_tools()
        return list(tools)
    if transport == "stdio":
        if not get_settings().mcp_stdio_allowed:
            raise RuntimeError("stdio MCP transport is disabled")
        command = str(cfg.get("command") or "")
        if not command:
            raise RuntimeError("stdio MCP command is missing")
        connection = StdioConnection(
            transport="stdio",
            command=command,
            args=[str(arg) for arg in (cfg.get("args") or [])],
        )
        client = MultiServerMCPClient({server_key: connection})
        async with asyncio.timeout(30):
            tools = await client.get_tools()
        return list(tools)
    raise RuntimeError(f"unsupported MCP transport: {transport}")


def _make_handler(tool: Any) -> Callable[..., Awaitable[str]]:
    async def _handler(**kwargs: Any) -> str:
        async with asyncio.timeout(get_settings().mcp_tool_timeout_seconds):
            result = await tool.ainvoke(kwargs)
        if isinstance(result, str):
            return result
        return json.dumps(result, default=str)

    return _handler
