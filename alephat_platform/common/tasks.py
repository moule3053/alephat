"""Task relational persistence and Temporal coordination."""

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from alephat_platform.common.enums import (
    DEFAULT_SANDBOX_PROVIDER,
    AgentType,
    ApprovalStatus,
    McpMode,
    MessageKind,
    RunStatus,
    TaskSource,
    TaskStatus,
)
from alephat_platform.common.errors import ConflictError, NotFoundError
from alephat_platform.common.models import (
    Approval,
    McpServer,
    Message,
    OrgSettings,
    Run,
    RunEvent,
    SandboxRow,
    Task,
    UserSettings,
)
from alephat_platform.common.resolution import (
    mcp_public_snapshot,
    resolve_mcp_ids,
    resolve_model,
    resolve_sandbox_provider,
)
from alephat_platform.common.state_machine import assert_task_transition, is_terminal
from alephat_platform.common.temporal import (
    ApprovalResolution,
    TemporalPlatformClient,
    TurnEnvelope,
)


@dataclass(frozen=True)
class PreparedTurnSubmission:
    """Relational task + message state ready to commit and dispatch to Temporal."""

    task: Task
    turn: TurnEnvelope | None


class TaskStore:
    """Relational operations on tasks, messages, approvals, events, and sandboxes."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get_task(self, task_id: uuid.UUID) -> Task:
        task = await self.session.get(Task, task_id)
        if task is None:
            raise NotFoundError(f"task {task_id} not found")
        return task

    async def append_event(
        self,
        *,
        task_id: uuid.UUID,
        event_type: str,
        payload: dict[str, object] | None = None,
        run_id: uuid.UUID | None = None,
    ) -> RunEvent:
        event = RunEvent(
            task_id=task_id,
            run_id=run_id,
            event_type=event_type,
            payload=payload or {},
        )
        self.session.add(event)
        await self.session.flush()
        return event

    async def create_task(
        self,
        *,
        org_id: uuid.UUID,
        user_id: uuid.UUID,
        title: str | None,
        prompt: str | None,
        prompt_message_id: uuid.UUID | None = None,
        repo: str | None = None,
        base_ref: str | None = "main",
        source: TaskSource | str = TaskSource.WEB_UI,
        source_ref: str | None = None,
        thread_id: str | None = None,
        agent_type: AgentType | str = AgentType.CODING,
        model: str | None = None,
        sandbox_provider: str | None = None,
        mcp_server_ids: list[uuid.UUID] | None = None,
        mcp_mode: McpMode | str = McpMode.INHERIT,
        metadata: dict[str, object] | None = None,
        platform_default_model: str = "gpt-4.1",
        platform_default_sandbox_provider: str = DEFAULT_SANDBOX_PROVIDER.value,
    ) -> PreparedTurnSubmission:
        agent = AgentType(agent_type)
        source_val = TaskSource(source) if not isinstance(source, TaskSource) else source
        mode = McpMode(mcp_mode)

        org_settings = await self.session.get(OrgSettings, org_id)
        user_settings = await self.session.get(UserSettings, user_id)

        resolved_model = resolve_model(
            request_model=model,
            user_preferred=user_settings.preferred_model if user_settings else None,
            org_default=org_settings.default_model if org_settings else None,
            platform_default=platform_default_model,
        )
        provider = resolve_sandbox_provider(
            request_provider=sandbox_provider,
            user_preferred=(user_settings.preferred_sandbox_provider if user_settings else None),
            org_default=org_settings.default_sandbox_provider if org_settings else None,
            enabled=list(org_settings.enabled_sandbox_providers)
            if org_settings and org_settings.enabled_sandbox_providers
            else None,
            platform_default=platform_default_sandbox_provider,
        )

        mcp_result = await self.session.execute(
            select(McpServer).where(
                McpServer.deleted_at.is_(None),
                McpServer.enabled.is_(True),
                (
                    ((McpServer.scope == "org") & (McpServer.org_id == org_id))
                    | ((McpServer.scope == "user") & (McpServer.user_id == user_id))
                ),
            )
        )
        servers = list(mcp_result.scalars().all())
        visible = {s.mcp_server_id for s in servers}
        org_required = [s.mcp_server_id for s in servers if s.scope == "org" and s.required]
        agent_types_map = {s.mcp_server_id: list(s.agent_types or []) for s in servers}
        user_defaults = list(user_settings.default_mcp_server_ids) if user_settings else []
        max_mcp = org_settings.max_mcp_servers_per_run if org_settings else 10

        resolved_mcp_ids = resolve_mcp_ids(
            mode=mode,
            request_ids=mcp_server_ids,
            user_defaults=user_defaults,
            org_required=org_required,
            visible_enabled=visible,
            agent_type=agent,
            server_agent_types=agent_types_map,
            max_servers=max_mcp,
        )
        by_id = {s.mcp_server_id: s for s in servers}
        snapshot = [
            mcp_public_snapshot(by_id[i], include_secrets=True)
            for i in resolved_mcp_ids
            if i in by_id
        ]

        tid = thread_id or f"api:{org_id}:{uuid.uuid4()}"
        meta = dict(metadata or {})
        task = Task(
            org_id=org_id,
            thread_id=tid,
            source=source_val.value,
            source_ref=source_ref,
            agent_type=agent.value,
            status=TaskStatus.QUEUED.value,
            title=title,
            repo=repo,
            base_ref=base_ref,
            prompt=prompt,
            model=resolved_model,
            sandbox_provider=provider.value,
            mcp_server_ids=resolved_mcp_ids,
            mcp_snapshot=snapshot,
            created_by=user_id,
            metadata_=meta,
        )
        self.session.add(task)
        await self.session.flush()

        msg_id = prompt_message_id or uuid.uuid4()
        turn = TurnEnvelope(
            turn_id=str(msg_id),
            message_id=str(msg_id),
            content=prompt or "",
            role="user",
            message_ts=(
                str(meta["message_ts"]) if isinstance(meta.get("message_ts"), str) else None
            ),
        )
        if prompt:
            self.session.add(
                Message(
                    message_id=msg_id,
                    task_id=task.task_id,
                    role="user",
                    kind=MessageKind.USER_INPUT.value,
                    content=prompt,
                )
            )
            await self.session.flush()

        await self.append_event(
            task_id=task.task_id,
            event_type="task_created",
            payload={
                "source": task.source,
                "model": task.model,
                "sandbox_provider": task.sandbox_provider,
                "mcp_server_ids": [str(i) for i in resolved_mcp_ids],
            },
        )
        return PreparedTurnSubmission(task=task, turn=turn)

    async def append_message(
        self,
        *,
        task_id: uuid.UUID,
        content: str,
        kind: MessageKind | str = MessageKind.USER_GUIDANCE,
        message_id: uuid.UUID | None = None,
        allow_reopen: bool = False,
        message_ts: str | None = None,
    ) -> tuple[Task, Message, TurnEnvelope]:
        task = await self.get_task(task_id)
        if is_terminal(task.status):
            if not allow_reopen:
                raise ConflictError("task is terminal", error_code="task_terminal")
            task.status = TaskStatus.QUEUED.value
            task.park_reason = None
            task.cancel_requested_at = None
            task.cancel_reason = None
            task.updated_at = datetime.now(UTC)
        elif task.status == TaskStatus.PARKED.value:
            assert_task_transition(task.status, TaskStatus.QUEUED)
            task.status = TaskStatus.QUEUED.value
            task.park_reason = None
            task.updated_at = datetime.now(UTC)

        if message_ts:
            task.metadata_ = {**(task.metadata_ or {}), "message_ts": message_ts}

        resolved_id = message_id or uuid.uuid4()
        msg = Message(
            message_id=resolved_id,
            task_id=task_id,
            role="user",
            kind=MessageKind(kind).value,
            content=content,
        )
        self.session.add(msg)
        await self.session.flush()
        await self.append_event(
            task_id=task_id,
            event_type="message_received",
            payload={"message_id": str(msg.message_id), "kind": msg.kind},
            run_id=task.active_run_id,
        )
        turn = TurnEnvelope(
            turn_id=str(msg.message_id),
            message_id=str(msg.message_id),
            content=content,
            role="user",
            message_ts=message_ts,
        )
        return task, msg, turn

    async def request_cancel(
        self,
        *,
        task_id: uuid.UUID,
        reason: str = "user_requested",
    ) -> tuple[Task, bool]:
        """Record cancellation and return ``(task, needs_workflow_cancel)``."""
        task = await self.get_task(task_id)
        if is_terminal(task.status):
            return task, False

        task.cancel_requested_at = datetime.now(UTC)
        task.cancel_reason = reason
        await self.append_event(
            task_id=task_id,
            event_type="cancel_requested",
            payload={"reason": reason},
            run_id=task.active_run_id,
        )

        if task.status in {TaskStatus.QUEUED.value, TaskStatus.PARKED.value}:
            assert_task_transition(task.status, TaskStatus.CANCELLED)
            task.status = TaskStatus.CANCELLED.value
            task.park_reason = None
            task.updated_at = datetime.now(UTC)
            if task.active_run_id:
                run = await self.session.get(Run, task.active_run_id)
                if run and run.status not in {
                    RunStatus.SUCCEEDED.value,
                    RunStatus.FAILED.value,
                    RunStatus.CANCELLED.value,
                }:
                    run.status = RunStatus.CANCELLED.value
                    run.ended_at = datetime.now(UTC)
            await self.append_event(task_id=task_id, event_type="cancelled", payload={})
        return task, True

    async def record_approval_decision(
        self,
        *,
        approval_id: uuid.UUID,
        decision: str,
        user_id: uuid.UUID,
        comment: str | None = None,
    ) -> tuple[Task, Approval, ApprovalResolution]:
        approval = await self.session.get(Approval, approval_id)
        if approval is None:
            raise NotFoundError(f"approval {approval_id} not found")
        if approval.status != ApprovalStatus.PENDING.value:
            raise ConflictError("approval already decided", error_code="approval_decided")

        approval.status = (
            ApprovalStatus.APPROVED.value
            if decision == "approved"
            else ApprovalStatus.REJECTED.value
        )
        approval.decided_by = user_id
        approval.decided_at = datetime.now(UTC)
        approval.comment = comment

        task = await self.get_task(approval.task_id)
        await self.append_event(
            task_id=task.task_id,
            event_type="approval_decided",
            payload={
                "approval_id": str(approval_id),
                "decision": approval.status,
                "comment": comment,
            },
            run_id=approval.run_id,
        )
        if task.status == TaskStatus.PARKED.value:
            assert_task_transition(task.status, TaskStatus.QUEUED)
            task.status = TaskStatus.QUEUED.value
            task.park_reason = None
            task.updated_at = datetime.now(UTC)
            if decision == "rejected":
                self.session.add(
                    Message(
                        task_id=task.task_id,
                        role="user",
                        kind=MessageKind.USER_GUIDANCE.value,
                        content=comment or "Plan rejected; revise.",
                    )
                )
        await self.session.flush()
        resolution = ApprovalResolution(
            approval_id=str(approval.approval_id),
            decision=decision,
            comment=comment,
        )
        return task, approval, resolution

    async def upsert_sandbox(
        self,
        *,
        task_id: uuid.UUID,
        provider: str,
        sandbox_id: str,
        metadata: dict[str, object] | None = None,
    ) -> SandboxRow:
        result = await self.session.execute(
            select(SandboxRow).where(
                SandboxRow.task_id == task_id,
                SandboxRow.provider == provider,
                SandboxRow.sandbox_id == sandbox_id,
            )
        )
        row = result.scalar_one_or_none()
        if row:
            row.last_seen_at = datetime.now(UTC)
            row.status = "active"
            if metadata:
                row.provider_metadata = {**(row.provider_metadata or {}), **metadata}
            return row
        row = SandboxRow(
            task_id=task_id,
            provider=provider,
            sandbox_id=sandbox_id,
            status="active",
            provider_metadata=metadata or {},
        )
        self.session.add(row)
        await self.session.flush()
        return row


class TaskDispatcher:
    """Commit relational task updates and drive Temporal workflows."""

    def __init__(self, session: AsyncSession, temporal: TemporalPlatformClient) -> None:
        self.session = session
        self.store = TaskStore(session)
        self.temporal = temporal

    async def create_and_start(
        self,
        *,
        org_id: uuid.UUID,
        user_id: uuid.UUID,
        title: str | None,
        prompt: str | None,
        prompt_message_id: uuid.UUID | None = None,
        repo: str | None = None,
        base_ref: str | None = "main",
        source: TaskSource | str = TaskSource.WEB_UI,
        source_ref: str | None = None,
        thread_id: str | None = None,
        agent_type: AgentType | str = AgentType.CODING,
        model: str | None = None,
        sandbox_provider: str | None = None,
        mcp_server_ids: list[uuid.UUID] | None = None,
        mcp_mode: McpMode | str = McpMode.INHERIT,
        metadata: dict[str, object] | None = None,
        platform_default_model: str = "gpt-4.1",
        platform_default_sandbox_provider: str = DEFAULT_SANDBOX_PROVIDER.value,
    ) -> Task:
        submission = await self.store.create_task(
            org_id=org_id,
            user_id=user_id,
            title=title,
            prompt=prompt,
            prompt_message_id=prompt_message_id,
            repo=repo,
            base_ref=base_ref,
            source=source,
            source_ref=source_ref,
            thread_id=thread_id,
            agent_type=agent_type,
            model=model,
            sandbox_provider=sandbox_provider,
            mcp_server_ids=mcp_server_ids,
            mcp_mode=mcp_mode,
            metadata=metadata,
            platform_default_model=platform_default_model,
            platform_default_sandbox_provider=platform_default_sandbox_provider,
        )
        await self.session.commit()
        if submission.turn is not None:
            accepted = await self.temporal.submit_turn(
                task_id=submission.task.task_id,
                org_id=submission.task.org_id,
                turn=submission.turn,
            )
            submission.task.active_run_id = uuid.UUID(accepted.run_id)
            await self.session.commit()
        return submission.task

    async def send_message(
        self,
        *,
        task_id: uuid.UUID,
        content: str,
        kind: MessageKind | str = MessageKind.USER_GUIDANCE,
        message_id: uuid.UUID | None = None,
        allow_reopen: bool = False,
        message_ts: str | None = None,
    ) -> tuple[Task, Message]:
        task, msg, turn = await self.store.append_message(
            task_id=task_id,
            content=content,
            kind=kind,
            message_id=message_id,
            allow_reopen=allow_reopen,
            message_ts=message_ts,
        )
        await self.session.commit()
        await self.temporal.submit_turn(
            task_id=task.task_id,
            org_id=task.org_id,
            turn=turn,
        )
        return task, msg

    async def cancel(
        self,
        *,
        task_id: uuid.UUID,
        reason: str = "user_requested",
    ) -> Task:
        task, needs_workflow_cancel = await self.store.request_cancel(
            task_id=task_id,
            reason=reason,
        )
        await self.session.commit()
        if needs_workflow_cancel:
            await self.temporal.cancel_task(task_id=task.task_id, reason=reason)
        return task

    async def decide_approval(
        self,
        *,
        approval_id: uuid.UUID,
        decision: str,
        user_id: uuid.UUID,
        comment: str | None = None,
    ) -> Approval:
        task, approval, resolution = await self.store.record_approval_decision(
            approval_id=approval_id,
            decision=decision,
            user_id=user_id,
            comment=comment,
        )
        await self.session.commit()
        await self.temporal.submit_approval(
            task_id=task.task_id,
            resolution=resolution,
        )
        return approval
