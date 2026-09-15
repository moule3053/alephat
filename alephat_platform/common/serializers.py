"""JSON serializers for API responses."""

from datetime import datetime
from typing import Any
from uuid import UUID

from alephat_platform.common.models import Approval, McpServer, Run, RunEvent, Task


def _dt(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def _uuid(value: UUID | None) -> str | None:
    return str(value) if value else None


def task_to_dict(task: Task) -> dict[str, Any]:
    return {
        "task_id": str(task.task_id),
        "org_id": str(task.org_id),
        "thread_id": task.thread_id,
        "source": task.source,
        "source_ref": task.source_ref,
        "agent_type": task.agent_type,
        "status": task.status,
        "title": task.title,
        "repo": task.repo,
        "base_ref": task.base_ref,
        "prompt": task.prompt,
        "model": task.model,
        "sandbox_provider": task.sandbox_provider,
        "mcp_server_ids": [str(i) for i in (task.mcp_server_ids or [])],
        "park_reason": task.park_reason,
        "cancel_requested_at": _dt(task.cancel_requested_at),
        "active_run_id": _uuid(task.active_run_id),
        "created_by": _uuid(task.created_by),
        "retry_count": task.retry_count,
        "metadata": task.metadata_ or {},
        "created_at": _dt(task.created_at),
        "updated_at": _dt(task.updated_at),
    }


def run_to_dict(run: Run) -> dict[str, Any]:
    return {
        "run_id": str(run.run_id),
        "task_id": str(run.task_id),
        "status": run.status,
        "attempt": run.attempt,
        "worker_id": run.worker_id,
        "lease_expires_at": _dt(run.lease_expires_at),
        "started_at": _dt(run.started_at),
        "ended_at": _dt(run.ended_at),
        "error_code": run.error_code,
        "error_message": run.error_message,
        "created_at": _dt(run.created_at),
    }


def event_to_dict(event: RunEvent) -> dict[str, Any]:
    return {
        "event_id": str(event.event_id),
        "task_id": str(event.task_id),
        "run_id": _uuid(event.run_id),
        "seq": event.seq,
        "event_type": event.event_type,
        "payload": event.payload or {},
        "created_at": _dt(event.created_at),
    }


def approval_to_dict(approval: Approval) -> dict[str, Any]:
    return {
        "approval_id": str(approval.approval_id),
        "task_id": str(approval.task_id),
        "run_id": _uuid(approval.run_id),
        "kind": approval.kind,
        "payload": approval.payload or {},
        "status": approval.status,
        "decided_by": _uuid(approval.decided_by),
        "decided_at": _dt(approval.decided_at),
        "comment": approval.comment,
        "expires_at": _dt(approval.expires_at),
        "created_at": _dt(approval.created_at),
    }


def mcp_to_dict(server: McpServer) -> dict[str, Any]:
    return {
        "mcp_server_id": str(server.mcp_server_id),
        "scope": server.scope,
        "org_id": _uuid(server.org_id),
        "user_id": _uuid(server.user_id),
        "name": server.name,
        "transport": server.transport,
        "url": server.url,
        "command": server.command,
        "args": server.args or [],
        "enabled": server.enabled,
        "tool_allowlist": list(server.tool_allowlist or []),
        "tool_denylist": list(server.tool_denylist or []),
        "agent_types": list(server.agent_types or []),
        "execution": server.execution,
        "required": server.required,
        "has_auth": bool(server.auth_ciphertext or server.headers_ciphertext),
        "last_test_at": _dt(server.last_test_at),
        "last_test_ok": server.last_test_ok,
        "last_tools": list(server.last_tools or []),
        "created_at": _dt(server.created_at),
        "updated_at": _dt(server.updated_at),
    }
