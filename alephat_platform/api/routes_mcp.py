"""MCP server configuration API."""

import json
import uuid
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from alephat_platform.api.deps import AuthContext, get_auth, get_db, require_admin
from alephat_platform.common.config import get_settings
from alephat_platform.common.crypto import EncryptionKeyMissingError, try_encrypt
from alephat_platform.common.enums import McpExecution, McpScope, McpTransport
from alephat_platform.common.errors import ForbiddenError, NotFoundError, PlatformError
from alephat_platform.common.models import McpServer
from alephat_platform.common.network_security import validate_remote_url
from alephat_platform.common.resolution import mcp_public_snapshot
from alephat_platform.common.serializers import mcp_to_dict
from alephat_platform.harness.mcp_runtime import connect_mcp_servers

router = APIRouter(prefix="/v1/mcp-servers", tags=["mcp"])


class McpCreateBody(BaseModel):
    scope: str = McpScope.USER.value
    name: str
    transport: str
    url: str | None = None
    command: str | None = None
    args: list[str] = Field(default_factory=list)
    headers: dict[str, str] | None = None
    auth: dict[str, Any] | None = None
    enabled: bool = True
    tool_allowlist: list[str] = Field(default_factory=list)
    tool_denylist: list[str] = Field(default_factory=list)
    agent_types: list[str] = Field(default_factory=list)
    execution: str = McpExecution.HARNESS.value
    required: bool = False


class McpPatchBody(BaseModel):
    name: str | None = None
    url: str | None = None
    command: str | None = None
    args: list[str] | None = None
    headers: dict[str, str] | None = None
    auth: dict[str, Any] | None = None
    enabled: bool | None = None
    tool_allowlist: list[str] | None = None
    tool_denylist: list[str] | None = None
    agent_types: list[str] | None = None
    execution: str | None = None
    required: bool | None = None


def _auth_cipher(auth: dict[str, Any] | None) -> str | None:
    if not auth:
        return None
    token = auth.get("token") or auth.get("bearer") or json.dumps(auth)
    try:
        return try_encrypt(str(token))
    except EncryptionKeyMissingError as exc:
        raise PlatformError(
            "Secret encryption unavailable",
            status=503,
            detail="TOKEN_ENCRYPTION_KEY is required before storing MCP credentials",
            error_code="encryption_not_configured",
        ) from exc


def _headers_cipher(headers: dict[str, str] | None) -> str | None:
    if not headers:
        return None
    try:
        return try_encrypt(json.dumps(headers))
    except EncryptionKeyMissingError as exc:
        raise PlatformError(
            "Secret encryption unavailable",
            status=503,
            detail="TOKEN_ENCRYPTION_KEY is required before storing MCP headers",
            error_code="encryption_not_configured",
        ) from exc


@router.get("")
async def list_mcp(
    scope: str | None = None,
    enabled: bool | None = Query(default=None),
    db: AsyncSession = Depends(get_db),
    auth: AuthContext = Depends(get_auth),
) -> dict[str, Any]:
    q = select(McpServer).where(McpServer.deleted_at.is_(None))
    q = q.where(
        ((McpServer.scope == "org") & (McpServer.org_id == auth.org_id))
        | ((McpServer.scope == "user") & (McpServer.user_id == auth.user_id))
    )
    if scope:
        q = q.where(McpServer.scope == scope)
    if enabled is not None:
        q = q.where(McpServer.enabled.is_(enabled))
    result = await db.execute(q.order_by(McpServer.created_at.desc()))
    return {"items": [mcp_to_dict(s) for s in result.scalars().all()]}


@router.post("", status_code=201)
async def create_mcp(
    body: McpCreateBody,
    db: AsyncSession = Depends(get_db),
    auth: AuthContext = Depends(get_auth),
) -> dict[str, Any]:
    settings = get_settings()
    try:
        transport = McpTransport(body.transport)
    except ValueError as exc:
        raise PlatformError(
            "Invalid transport",
            status=400,
            error_code="invalid_transport",
            detail=str(exc),
        ) from exc

    if transport == McpTransport.STDIO and not settings.mcp_stdio_allowed:
        raise ForbiddenError("stdio MCP transport is disabled by policy")

    if transport in {McpTransport.SSE, McpTransport.STREAMABLE_HTTP} and not body.url:
        raise PlatformError(
            "URL required",
            status=400,
            error_code="url_required",
            detail="url is required for remote transports",
        )
    if body.url:
        try:
            await validate_remote_url(
                body.url,
                allow_private=settings.mcp_allow_private_networks,
                resolve=False,
            )
        except ValueError as exc:
            raise PlatformError(
                "MCP URL blocked",
                status=400,
                detail=str(exc),
                error_code="mcp_url_blocked",
            ) from exc
    if transport == McpTransport.STDIO and not body.command:
        raise PlatformError(
            "command required",
            status=400,
            error_code="command_required",
            detail="command is required for stdio transport",
        )

    try:
        scope = McpScope(body.scope)
        execution = McpExecution(body.execution)
    except ValueError as exc:
        raise PlatformError(
            "Invalid MCP configuration",
            status=400,
            detail=str(exc),
            error_code="invalid_mcp_configuration",
        ) from exc
    if scope == McpScope.ORG:
        require_admin(auth)
    server = McpServer(
        scope=scope.value,
        org_id=auth.org_id if scope == McpScope.ORG else None,
        user_id=auth.user_id if scope == McpScope.USER else None,
        name=body.name,
        transport=transport.value,
        url=body.url,
        command=body.command,
        args=body.args,
        auth_ciphertext=_auth_cipher(body.auth),
        headers_ciphertext=_headers_cipher(body.headers),
        enabled=body.enabled,
        tool_allowlist=body.tool_allowlist,
        tool_denylist=body.tool_denylist,
        agent_types=body.agent_types,
        execution=execution.value,
        required=body.required if scope == McpScope.ORG else False,
        created_by=auth.user_id,
    )
    db.add(server)
    await db.flush()
    return mcp_to_dict(server)


@router.get("/{mcp_server_id}")
async def get_mcp(
    mcp_server_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    auth: AuthContext = Depends(get_auth),
) -> dict[str, Any]:
    server = await _visible_server(db, mcp_server_id, auth)
    return mcp_to_dict(server)


@router.patch("/{mcp_server_id}")
async def patch_mcp(
    mcp_server_id: uuid.UUID,
    body: McpPatchBody,
    db: AsyncSession = Depends(get_db),
    auth: AuthContext = Depends(get_auth),
) -> dict[str, Any]:
    server = await _visible_server(db, mcp_server_id, auth, require_owner=True)
    data = body.model_dump(exclude_unset=True)
    if "url" in data and data["url"]:
        try:
            await validate_remote_url(
                str(data["url"]),
                allow_private=get_settings().mcp_allow_private_networks,
                resolve=False,
            )
        except ValueError as exc:
            raise PlatformError(
                "MCP URL blocked",
                status=400,
                detail=str(exc),
                error_code="mcp_url_blocked",
            ) from exc
    if "execution" in data and data["execution"] is not None:
        try:
            data["execution"] = McpExecution(data["execution"]).value
        except ValueError as exc:
            raise PlatformError(
                "Invalid MCP execution mode",
                status=400,
                detail=str(exc),
                error_code="invalid_mcp_execution",
            ) from exc
    if "auth" in data:
        server.auth_ciphertext = _auth_cipher(data.pop("auth"))
    if "headers" in data:
        server.headers_ciphertext = _headers_cipher(data.pop("headers"))
    for key, value in data.items():
        setattr(server, key, value)
    server.updated_at = datetime.now(UTC)
    await db.flush()
    return mcp_to_dict(server)


@router.delete("/{mcp_server_id}", status_code=204)
async def delete_mcp(
    mcp_server_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    auth: AuthContext = Depends(get_auth),
) -> None:
    server = await _visible_server(db, mcp_server_id, auth, require_owner=True)
    server.deleted_at = datetime.now(UTC)
    server.enabled = False
    await db.flush()


@router.post("/{mcp_server_id}/test")
async def test_mcp(
    mcp_server_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    auth: AuthContext = Depends(get_auth),
) -> dict[str, Any]:
    server = await _visible_server(db, mcp_server_id, auth)
    tools: list[str] = []
    ok = True
    detail = "ok"
    try:
        sessions = await connect_mcp_servers([mcp_public_snapshot(server, include_secrets=True)])
        session = sessions[0]
        ok = session.ok
        detail = session.error or "connected"
        tools = [tool.tool_name for tool in session.tools]
    except Exception as exc:
        ok = False
        detail = str(exc)

    server.last_test_at = datetime.now(UTC)
    server.last_test_ok = ok
    server.last_tools = tools
    await db.flush()
    return {"ok": ok, "tools": tools, "detail": detail}


async def _visible_server(
    db: AsyncSession,
    mcp_server_id: uuid.UUID,
    auth: AuthContext,
    *,
    require_owner: bool = False,
) -> McpServer:
    server = await db.get(McpServer, mcp_server_id)
    if server is None or server.deleted_at is not None:
        raise NotFoundError("mcp server not found")
    visible = (server.scope == "org" and server.org_id == auth.org_id) or (
        server.scope == "user" and server.user_id == auth.user_id
    )
    if not visible:
        raise NotFoundError("mcp server not found")
    if require_owner:
        if server.scope == "user" and server.user_id != auth.user_id:
            raise ForbiddenError()
        if server.scope == "org":
            require_admin(auth)
    return server
