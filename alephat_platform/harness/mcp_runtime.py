"""MCP tool discovery and invocation for Temporal Activity workers."""

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

from langchain_core.tools import BaseTool
from langchain_core.utils.function_calling import convert_to_openai_tool

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
    langchain_tool: BaseTool | None = None

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


@dataclass
class McpToolSpec:
    """Serializable MCP tool descriptor crossing the Workflow <-> Activity boundary."""

    namespaced_name: str
    server_id: str
    server_name: str
    tool_name: str
    description: str
    args_schema: dict[str, object]
    server_config: dict[str, object]


class McpToolInvoker:
    """Wrap one LangChain MCP tool with a configured execution timeout."""

    def __init__(self, tool: BaseTool) -> None:
        self._tool = tool

    async def __call__(self, **kwargs: object) -> str:
        async with asyncio.timeout(get_settings().mcp_tool_timeout_seconds):
            result = await self._tool.ainvoke(dict(kwargs))
        if isinstance(result, str):
            return result
        return json.dumps(result, default=str)


async def _load_tools(cfg: dict[str, object]) -> list[BaseTool]:
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
    if transport in {"sse", "streamable_http"} and isinstance(url, str) and url:
        await validate_remote_url(
            url,
            allow_private=get_settings().mcp_allow_private_networks,
        )
        auth_cipher = cfg.get("auth_ciphertext")
        headers_cipher = cfg.get("headers_ciphertext")
        headers_json = try_decrypt(headers_cipher if isinstance(headers_cipher, str) else None)
        raw_headers = json.loads(headers_json) if headers_json else {}
        headers = (
            {str(k): str(v) for k, v in raw_headers.items()}
            if isinstance(raw_headers, dict)
            else {}
        )
        auth = try_decrypt(auth_cipher if isinstance(auth_cipher, str) else None)
        if auth:
            headers.setdefault("Authorization", f"Bearer {auth}")
        if transport == "sse":
            connection = SSEConnection(
                url=url,
                transport="sse",
                headers=headers,
            )
        else:
            connection = StreamableHttpConnection(
                url=url,
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
        raw_args = cfg.get("args")
        args = [str(arg) for arg in raw_args] if isinstance(raw_args, list) else []
        connection = StdioConnection(
            transport="stdio",
            command=command,
            args=args,
        )
        client = MultiServerMCPClient({server_key: connection})
        async with asyncio.timeout(30):
            tools = await client.get_tools()
        return list(tools)
    raise RuntimeError(f"unsupported MCP transport: {transport}")


class McpRuntime:
    """Filter, discover, and invoke MCP server tools inside Temporal Activities."""

    def filter_tools(
        self,
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

    async def connect_servers(self, snapshot: list[dict[str, object]]) -> list[McpSession]:
        """Connect configured MCP servers; fail open for optional servers."""
        sessions: list[McpSession] = []
        for cfg in snapshot or []:
            server_id = str(cfg.get("mcp_server_id") or "")
            name = str(cfg.get("name") or "mcp")
            required = bool(cfg.get("required"))
            try:
                tools = await _load_tools(cfg)
                raw_allow = cfg.get("tool_allowlist")
                raw_deny = cfg.get("tool_denylist")
                allow = [str(x) for x in raw_allow] if isinstance(raw_allow, list) else []
                deny = [str(x) for x in raw_deny] if isinstance(raw_deny, list) else []
                filtered_names = self.filter_tools(
                    [str(getattr(tool, "name", "")) for tool in tools],
                    allowlist=allow,
                    denylist=deny,
                )
                mcp_tools: list[McpTool] = []
                for tool in tools:
                    tname = str(getattr(tool, "name", ""))
                    if tname not in filtered_names:
                        continue
                    item = McpTool(
                        server_id=server_id,
                        server_name=name,
                        tool_name=tname,
                        description=str(getattr(tool, "description", "") or ""),
                        handler=McpToolInvoker(tool),
                    )
                    item.langchain_tool = (
                        tool.model_copy(update={"name": item.namespaced})
                        if hasattr(tool, "model_copy")
                        else tool
                    )
                    mcp_tools.append(item)
                sessions.append(
                    McpSession(server_id=server_id, name=name, ok=True, tools=mcp_tools)
                )
            except Exception as exc:
                logger.exception("MCP connect failed", extra={"mcp_server_name": name})
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

    def extract_schema(self, tool: BaseTool) -> dict[str, object]:
        try:
            openai_schema = convert_to_openai_tool(tool)
            fn = openai_schema.get("function")
            if isinstance(fn, dict) and isinstance(fn.get("parameters"), dict):
                return dict(fn["parameters"])
        except Exception:
            logger.warning(
                "Falling back to empty MCP tool parameter schema",
                extra={"tool_name": getattr(tool, "name", "")},
                exc_info=True,
            )
        return {"type": "object", "properties": {}}

    async def discover_specs(
        self, snapshot: list[dict[str, object]]
    ) -> tuple[list[McpSession], list[McpToolSpec]]:
        sessions = await self.connect_servers(snapshot)
        by_id = {str(cfg.get("mcp_server_id") or ""): cfg for cfg in (snapshot or [])}
        specs: list[McpToolSpec] = []
        for session in sessions:
            if not session.ok:
                continue
            cfg = by_id.get(session.server_id, {})
            for item in session.tools:
                schema = (
                    self.extract_schema(item.langchain_tool)
                    if item.langchain_tool is not None
                    else {"type": "object", "properties": {}}
                )
                specs.append(
                    McpToolSpec(
                        namespaced_name=item.namespaced,
                        server_id=session.server_id,
                        server_name=session.name,
                        tool_name=item.tool_name,
                        description=item.description or f"MCP tool {item.namespaced}",
                        args_schema=schema,
                        server_config=dict(cfg),
                    )
                )
        return sessions, specs

    async def invoke_tool(
        self,
        *,
        server_config: dict[str, object],
        tool_name: str,
        args: dict[str, object],
    ) -> str:
        tools = await _load_tools(server_config)
        target = next(
            (t for t in tools if str(getattr(t, "name", "")) == tool_name),
            None,
        )
        if target is None:
            raise RuntimeError(f"MCP tool '{tool_name}' was not found on server")
        return await McpToolInvoker(target)(**args)


MCP_RUNTIME = McpRuntime()
filter_tools = MCP_RUNTIME.filter_tools
connect_mcp_servers = MCP_RUNTIME.connect_servers
