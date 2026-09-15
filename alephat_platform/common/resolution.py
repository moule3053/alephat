"""Resolve model, sandbox provider, and MCP servers for a task."""

import uuid
from dataclasses import dataclass
from typing import Any

from alephat_platform.common.enums import (
    DEFAULT_SANDBOX_PROVIDER,
    AgentType,
    McpMode,
    SandboxProvider,
)
from alephat_platform.common.errors import PlatformError


@dataclass
class ResolvedConfig:
    model: str
    sandbox_provider: SandboxProvider
    mcp_server_ids: list[uuid.UUID]
    mcp_snapshot: list[dict[str, Any]]


def resolve_model(
    *,
    request_model: str | None,
    user_preferred: str | None,
    org_default: str | None,
    platform_default: str,
) -> str:
    return (
        (request_model or "").strip()
        or (user_preferred or "").strip()
        or (org_default or "").strip()
        or platform_default
    )


def resolve_sandbox_provider(
    *,
    request_provider: str | None,
    user_preferred: str | None,
    org_default: str | None,
    enabled: list[str] | None,
    platform_default: str = DEFAULT_SANDBOX_PROVIDER.value,
) -> SandboxProvider:
    candidate = (
        (request_provider or "").strip()
        or (user_preferred or "").strip()
        or (org_default or "").strip()
        or platform_default
    )
    try:
        provider = SandboxProvider(candidate)
    except ValueError as exc:
        raise PlatformError(
            "Unknown sandbox provider",
            status=400,
            detail=f"Unknown sandbox_provider: {candidate}",
            error_code="unknown_sandbox_provider",
        ) from exc

    enabled_set = set(enabled or [p.value for p in SandboxProvider])
    if provider.value not in enabled_set:
        raise PlatformError(
            "Sandbox provider disabled",
            status=403,
            detail=f"sandbox_provider {provider.value} is not enabled for this org",
            error_code="sandbox_provider_disabled",
        )
    return provider


def resolve_mcp_ids(
    *,
    mode: McpMode,
    request_ids: list[uuid.UUID] | None,
    user_defaults: list[uuid.UUID] | None,
    org_required: list[uuid.UUID],
    visible_enabled: set[uuid.UUID],
    agent_type: AgentType | str,
    server_agent_types: dict[uuid.UUID, list[str]],
    max_servers: int,
) -> list[uuid.UUID]:
    agent = AgentType(agent_type) if not isinstance(agent_type, AgentType) else agent_type
    selected: list[uuid.UUID] = []

    def _matches_agent(sid: uuid.UUID) -> bool:
        types = server_agent_types.get(sid) or []
        return not types or agent.value in types

    for sid in org_required:
        if sid in visible_enabled and _matches_agent(sid):
            selected.append(sid)

    if mode == McpMode.REPLACE:
        for sid in request_ids or []:
            if sid not in visible_enabled:
                raise PlatformError(
                    "MCP server not visible",
                    status=403,
                    detail=f"mcp_server_id {sid} is not visible",
                    error_code="mcp_not_visible",
                )
            if _matches_agent(sid):
                selected.append(sid)
    else:
        for sid in user_defaults or []:
            if sid in visible_enabled and _matches_agent(sid):
                selected.append(sid)
        if mode == McpMode.APPEND:
            for sid in request_ids or []:
                if sid not in visible_enabled:
                    raise PlatformError(
                        "MCP server not visible",
                        status=403,
                        detail=f"mcp_server_id {sid} is not visible",
                        error_code="mcp_not_visible",
                    )
                if _matches_agent(sid):
                    selected.append(sid)

    # dedupe preserve order
    seen: set[uuid.UUID] = set()
    ordered: list[uuid.UUID] = []
    for sid in selected:
        if sid not in seen:
            seen.add(sid)
            ordered.append(sid)

    if len(ordered) > max_servers:
        ordered = ordered[:max_servers]
    return ordered


def mcp_public_snapshot(server: Any, *, include_secrets: bool = False) -> dict[str, Any]:
    """Build the immutable task snapshot without materializing plaintext secrets."""
    snap: dict[str, Any] = {
        "mcp_server_id": str(server.mcp_server_id),
        "name": server.name,
        "transport": server.transport,
        "url": server.url,
        "command": server.command,
        "args": server.args or [],
        "execution": server.execution,
        "tool_allowlist": list(server.tool_allowlist or []),
        "tool_denylist": list(server.tool_denylist or []),
        "agent_types": list(server.agent_types or []),
        "required": bool(getattr(server, "required", False)),
        "has_auth": bool(server.auth_ciphertext or server.headers_ciphertext),
    }
    if include_secrets:
        snap["auth_ciphertext"] = server.auth_ciphertext
        snap["headers_ciphertext"] = server.headers_ciphertext
    return snap
