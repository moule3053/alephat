"""JSON serializers for API responses."""

from datetime import datetime
from uuid import UUID

from alephat_platform.common.models import Approval, McpServer, Run, RunEvent, Task


class PlatformSerializer:
    """Serialize ORM records into API response payloads."""

    def iso_datetime(self, value: datetime | None) -> str | None:
        return value.isoformat() if value else None

    def str_uuid(self, value: UUID | None) -> str | None:
        return str(value) if value else None

    def task(self, task: Task) -> dict[str, object]:
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
            "cancel_requested_at": self.iso_datetime(task.cancel_requested_at),
            "active_run_id": self.str_uuid(task.active_run_id),
            "created_by": self.str_uuid(task.created_by),
            "retry_count": task.retry_count,
            "metadata": task.metadata_ or {},
            "created_at": self.iso_datetime(task.created_at),
            "updated_at": self.iso_datetime(task.updated_at),
        }

    def run(self, run: Run) -> dict[str, object]:
        return {
            "run_id": str(run.run_id),
            "task_id": str(run.task_id),
            "temporal_run_id": run.temporal_run_id,
            "status": run.status,
            "attempt": run.attempt,
            "worker_id": run.worker_id,
            "started_at": self.iso_datetime(run.started_at),
            "ended_at": self.iso_datetime(run.ended_at),
            "error_code": run.error_code,
            "error_message": run.error_message,
            "created_at": self.iso_datetime(run.created_at),
        }

    def event(self, event: RunEvent) -> dict[str, object]:
        return {
            "event_id": str(event.event_id),
            "task_id": str(event.task_id),
            "run_id": self.str_uuid(event.run_id),
            "seq": event.seq,
            "event_type": event.event_type,
            "payload": event.payload or {},
            "created_at": self.iso_datetime(event.created_at),
        }

    def approval(self, approval: Approval) -> dict[str, object]:
        return {
            "approval_id": str(approval.approval_id),
            "task_id": str(approval.task_id),
            "run_id": self.str_uuid(approval.run_id),
            "kind": approval.kind,
            "payload": approval.payload or {},
            "status": approval.status,
            "decided_by": self.str_uuid(approval.decided_by),
            "decided_at": self.iso_datetime(approval.decided_at),
            "comment": approval.comment,
            "expires_at": self.iso_datetime(approval.expires_at),
            "created_at": self.iso_datetime(approval.created_at),
        }

    def mcp(self, server: McpServer) -> dict[str, object]:
        return {
            "mcp_server_id": str(server.mcp_server_id),
            "scope": server.scope,
            "org_id": self.str_uuid(server.org_id),
            "user_id": self.str_uuid(server.user_id),
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
            "last_test_at": self.iso_datetime(server.last_test_at),
            "last_test_ok": server.last_test_ok,
            "last_tools": list(server.last_tools or []),
            "created_at": self.iso_datetime(server.created_at),
            "updated_at": self.iso_datetime(server.updated_at),
        }


SERIALIZER = PlatformSerializer()
task_to_dict = SERIALIZER.task
run_to_dict = SERIALIZER.run
event_to_dict = SERIALIZER.event
approval_to_dict = SERIALIZER.approval
mcp_to_dict = SERIALIZER.mcp
