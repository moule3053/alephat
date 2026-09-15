"""Task domain operations (create, message, cancel, park, resume, claim)."""

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from alephat_platform.common.enums import (
    DEFAULT_SANDBOX_PROVIDER,
    AgentType,
    ApprovalStatus,
    McpMode,
    MessageKind,
    ParkReason,
    RunStatus,
    TaskSource,
    TaskStatus,
)
from alephat_platform.common.errors import ConflictError, NotFoundError
from alephat_platform.common.models import (
    Approval,
    GraphCheckpoint,
    McpServer,
    Message,
    OrgSettings,
    Run,
    RunEvent,
    SandboxRow,
    Task,
    UserSettings,
)
from alephat_platform.common.outbox import enqueue_control, enqueue_task_work
from alephat_platform.common.resolution import (
    mcp_public_snapshot,
    resolve_mcp_ids,
    resolve_model,
    resolve_sandbox_provider,
)
from alephat_platform.common.state_machine import assert_task_transition, is_terminal


async def _get_task(session: AsyncSession, task_id: uuid.UUID) -> Task:
    task = await session.get(Task, task_id)
    if task is None:
        raise NotFoundError(f"task {task_id} not found")
    return task


async def append_event(
    session: AsyncSession,
    *,
    task_id: uuid.UUID,
    event_type: str,
    payload: dict[str, Any] | None = None,
    run_id: uuid.UUID | None = None,
) -> RunEvent:
    event = RunEvent(
        task_id=task_id,
        run_id=run_id,
        event_type=event_type,
        payload=payload or {},
    )
    session.add(event)
    await session.flush()
    return event


async def create_task(
    session: AsyncSession,
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
    metadata: dict[str, Any] | None = None,
    platform_default_model: str = "gpt-4.1",
    platform_default_sandbox_provider: str = DEFAULT_SANDBOX_PROVIDER.value,
) -> Task:
    agent = AgentType(agent_type)
    source_val = TaskSource(source) if not isinstance(source, TaskSource) else source
    mode = McpMode(mcp_mode)

    org_settings = await session.get(OrgSettings, org_id)
    user_settings = await session.get(UserSettings, user_id)

    resolved_model = resolve_model(
        request_model=model,
        user_preferred=user_settings.preferred_model if user_settings else None,
        org_default=org_settings.default_model if org_settings else None,
        platform_default=platform_default_model,
    )
    provider = resolve_sandbox_provider(
        request_provider=sandbox_provider,
        user_preferred=user_settings.preferred_sandbox_provider if user_settings else None,
        org_default=org_settings.default_sandbox_provider if org_settings else None,
        enabled=list(org_settings.enabled_sandbox_providers)
        if org_settings and org_settings.enabled_sandbox_providers
        else None,
        platform_default=platform_default_sandbox_provider,
    )

    # Load visible MCP servers
    mcp_result = await session.execute(
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
        mcp_public_snapshot(by_id[i], include_secrets=True) for i in resolved_mcp_ids if i in by_id
    ]

    tid = thread_id or f"api:{org_id}:{uuid.uuid4()}"
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
        metadata_=metadata or {},
    )
    session.add(task)
    await session.flush()

    if prompt:
        session.add(
            Message(
                message_id=prompt_message_id or uuid.uuid4(),
                task_id=task.task_id,
                role="user",
                kind=MessageKind.USER_INPUT.value,
                content=prompt,
            )
        )

    await append_event(
        session,
        task_id=task.task_id,
        event_type="task_created",
        payload={
            "source": task.source,
            "model": task.model,
            "sandbox_provider": task.sandbox_provider,
            "mcp_server_ids": [str(i) for i in resolved_mcp_ids],
        },
    )
    await enqueue_task_work(
        session,
        org_id=org_id,
        task_id=task.task_id,
        agent_type=agent.value,
        reason="created",
    )
    return task


async def add_message(
    session: AsyncSession,
    *,
    task_id: uuid.UUID,
    content: str,
    kind: MessageKind | str = MessageKind.USER_GUIDANCE,
    message_id: uuid.UUID | None = None,
) -> Message:
    task = await _get_task(session, task_id)
    if is_terminal(task.status):
        raise ConflictError("task is terminal", error_code="task_terminal")

    msg = Message(
        message_id=message_id or uuid.uuid4(),
        task_id=task_id,
        role="user",
        kind=MessageKind(kind).value,
        content=content,
    )
    session.add(msg)
    await session.flush()
    await append_event(
        session,
        task_id=task_id,
        event_type="message_received",
        payload={"message_id": str(msg.message_id), "kind": msg.kind},
        run_id=task.active_run_id,
    )

    if task.status == TaskStatus.PARKED.value:
        assert_task_transition(task.status, TaskStatus.QUEUED)
        task.status = TaskStatus.QUEUED.value
        task.park_reason = None
        task.updated_at = datetime.now(UTC)
        await enqueue_task_work(
            session,
            org_id=task.org_id,
            task_id=task.task_id,
            agent_type=task.agent_type,
            reason="resumed",
        )
    elif task.status == TaskStatus.RUNNING.value:
        await enqueue_control(
            session,
            org_id=task.org_id,
            task_id=task.task_id,
            control="append_message",
            run_id=task.active_run_id,
            extra={"message_id": str(msg.message_id)},
        )
    return msg


async def cancel_task(
    session: AsyncSession,
    *,
    task_id: uuid.UUID,
    reason: str = "user_requested",
) -> Task:
    task = await _get_task(session, task_id)
    if is_terminal(task.status):
        return task

    task.cancel_requested_at = datetime.now(UTC)
    task.cancel_reason = reason
    await append_event(
        session,
        task_id=task_id,
        event_type="cancel_requested",
        payload={"reason": reason},
        run_id=task.active_run_id,
    )

    if task.status in {TaskStatus.QUEUED.value, TaskStatus.PARKED.value}:
        assert_task_transition(task.status, TaskStatus.CANCELLED)
        task.status = TaskStatus.CANCELLED.value
        task.updated_at = datetime.now(UTC)
        if task.active_run_id:
            run = await session.get(Run, task.active_run_id)
            if run and run.status not in {
                RunStatus.SUCCEEDED.value,
                RunStatus.FAILED.value,
                RunStatus.CANCELLED.value,
            }:
                run.status = RunStatus.CANCELLED.value
                run.ended_at = datetime.now(UTC)
        await append_event(session, task_id=task_id, event_type="cancelled", payload={})
    elif task.status == TaskStatus.RUNNING.value:
        await enqueue_control(
            session,
            org_id=task.org_id,
            task_id=task.task_id,
            control="cancel",
            run_id=task.active_run_id,
            extra={"cancel_reason": reason},
        )
    return task


async def decide_approval(
    session: AsyncSession,
    *,
    approval_id: uuid.UUID,
    decision: str,
    user_id: uuid.UUID,
    comment: str | None = None,
) -> Approval:
    approval = await session.get(Approval, approval_id)
    if approval is None:
        raise NotFoundError(f"approval {approval_id} not found")
    if approval.status != ApprovalStatus.PENDING.value:
        raise ConflictError("approval already decided", error_code="approval_decided")

    approval.status = (
        ApprovalStatus.APPROVED.value if decision == "approved" else ApprovalStatus.REJECTED.value
    )
    approval.decided_by = user_id
    approval.decided_at = datetime.now(UTC)
    approval.comment = comment

    task = await _get_task(session, approval.task_id)
    await append_event(
        session,
        task_id=task.task_id,
        event_type="approval_decided",
        payload={
            "approval_id": str(approval_id),
            "decision": approval.status,
            "comment": comment,
        },
        run_id=approval.run_id,
    )

    if decision == "approved" and task.status == TaskStatus.PARKED.value:
        assert_task_transition(task.status, TaskStatus.QUEUED)
        task.status = TaskStatus.QUEUED.value
        task.park_reason = None
        await enqueue_task_work(
            session,
            org_id=task.org_id,
            task_id=task.task_id,
            agent_type=task.agent_type,
            reason="resumed",
        )
    elif decision == "rejected" and task.status == TaskStatus.PARKED.value:
        # default: re-queue for revision rather than hard fail
        assert_task_transition(task.status, TaskStatus.QUEUED)
        task.status = TaskStatus.QUEUED.value
        task.park_reason = None
        session.add(
            Message(
                task_id=task.task_id,
                role="user",
                kind=MessageKind.USER_GUIDANCE.value,
                content=comment or "Plan rejected; revise.",
            )
        )
        await enqueue_task_work(
            session,
            org_id=task.org_id,
            task_id=task.task_id,
            agent_type=task.agent_type,
            reason="resumed",
        )
    return approval


async def claim_task(
    session: AsyncSession,
    *,
    task_id: uuid.UUID,
    worker_id: str,
    lease_ttl_seconds: int = 60,
) -> tuple[Task, Run] | None:
    """Claim a queued task. Returns None if not claimable."""
    result = await session.execute(select(Task).where(Task.task_id == task_id).with_for_update())
    task = result.scalar_one_or_none()
    if task is None:
        return None
    if task.cancel_requested_at and task.status == TaskStatus.QUEUED.value:
        task.status = TaskStatus.CANCELLED.value
        await append_event(session, task_id=task_id, event_type="cancelled", payload={})
        return None
    if task.status != TaskStatus.QUEUED.value:
        return None

    # Resume existing parked run if present
    run: Run | None = None
    if task.active_run_id:
        existing = await session.get(Run, task.active_run_id)
        if existing and existing.status == RunStatus.PARKED.value:
            run = existing
            run.status = RunStatus.ACTIVE.value
            run.worker_id = worker_id
            run.lease_expires_at = datetime.now(UTC) + timedelta(seconds=lease_ttl_seconds)
            run.updated_at = datetime.now(UTC)

    if run is None:
        attempt = 1
        if task.active_run_id:
            prev = await session.get(Run, task.active_run_id)
            if prev:
                attempt = prev.attempt + 1
        run = Run(
            task_id=task.task_id,
            status=RunStatus.ACTIVE.value,
            attempt=attempt,
            worker_id=worker_id,
            lease_expires_at=datetime.now(UTC) + timedelta(seconds=lease_ttl_seconds),
            started_at=datetime.now(UTC),
        )
        session.add(run)
        await session.flush()

    assert_task_transition(task.status, TaskStatus.RUNNING)
    task.status = TaskStatus.RUNNING.value
    task.active_run_id = run.run_id
    task.updated_at = datetime.now(UTC)

    await append_event(
        session,
        task_id=task.task_id,
        run_id=run.run_id,
        event_type="run_leased",
        payload={"worker_id": worker_id, "attempt": run.attempt},
    )
    await append_event(
        session,
        task_id=task.task_id,
        run_id=run.run_id,
        event_type="run_started",
        payload={},
    )
    return task, run


async def heartbeat_lease(
    session: AsyncSession,
    *,
    run_id: uuid.UUID,
    worker_id: str,
    lease_ttl_seconds: int = 60,
) -> bool:
    run = await session.get(Run, run_id)
    if run is None or run.worker_id != worker_id:
        return False
    if run.status not in {RunStatus.ACTIVE.value, RunStatus.LEASED.value}:
        return False
    run.lease_expires_at = datetime.now(UTC) + timedelta(seconds=lease_ttl_seconds)
    run.updated_at = datetime.now(UTC)
    return True


async def park_task(
    session: AsyncSession,
    *,
    task_id: uuid.UUID,
    run_id: uuid.UUID,
    reason: ParkReason | str,
    approval_payload: dict[str, Any] | None = None,
) -> Approval | None:
    task = await _get_task(session, task_id)
    run = await session.get(Run, run_id)
    if run is None:
        raise NotFoundError("run not found")

    assert_task_transition(task.status, TaskStatus.PARKED)
    task.status = TaskStatus.PARKED.value
    task.park_reason = ParkReason(reason).value
    task.updated_at = datetime.now(UTC)
    run.status = RunStatus.PARKED.value
    run.worker_id = None
    run.lease_expires_at = None
    run.updated_at = datetime.now(UTC)

    approval = None
    if ParkReason(reason) == ParkReason.PLAN_APPROVAL:
        approval = Approval(
            task_id=task_id,
            run_id=run_id,
            kind="plan_approval",
            payload=approval_payload or {},
            status=ApprovalStatus.PENDING.value,
        )
        session.add(approval)
        await session.flush()
        await append_event(
            session,
            task_id=task_id,
            run_id=run_id,
            event_type="plan_proposed",
            payload={"approval_id": str(approval.approval_id)},
        )

    await append_event(
        session,
        task_id=task_id,
        run_id=run_id,
        event_type="parked",
        payload={"reason": task.park_reason},
    )
    return approval


async def complete_task(
    session: AsyncSession,
    *,
    task_id: uuid.UUID,
    run_id: uuid.UUID,
    success: bool,
    error_code: str | None = None,
    error_message: str | None = None,
) -> None:
    task = await _get_task(session, task_id)
    run = await session.get(Run, run_id)
    if run is None:
        raise NotFoundError("run not found")

    target = TaskStatus.SUCCEEDED if success else TaskStatus.FAILED
    if task.cancel_requested_at:
        target = TaskStatus.CANCELLED
        success = False
        error_code = error_code or "cancelled"

    assert_task_transition(task.status, target)
    task.status = target.value
    task.updated_at = datetime.now(UTC)
    run.status = (
        RunStatus.CANCELLED.value
        if target == TaskStatus.CANCELLED
        else (RunStatus.SUCCEEDED.value if success else RunStatus.FAILED.value)
    )
    run.ended_at = datetime.now(UTC)
    run.worker_id = None
    run.lease_expires_at = None
    run.error_code = error_code
    run.error_message = error_message

    event_type = (
        "cancelled"
        if target == TaskStatus.CANCELLED
        else ("run_succeeded" if success else "run_failed")
    )
    await append_event(
        session,
        task_id=task_id,
        run_id=run_id,
        event_type=event_type,
        payload={"error_code": error_code, "error_message": error_message},
    )


async def save_checkpoint(
    session: AsyncSession,
    *,
    task_id: uuid.UUID,
    run_id: uuid.UUID,
    blob: dict[str, Any],
    metadata: dict[str, Any] | None = None,
    thread_key: str = "default",
) -> GraphCheckpoint:
    result = await session.execute(
        select(GraphCheckpoint)
        .where(
            GraphCheckpoint.run_id == run_id,
            GraphCheckpoint.thread_key == thread_key,
        )
        .order_by(GraphCheckpoint.version.desc())
        .limit(1)
    )
    prev = result.scalar_one_or_none()
    version = (prev.version + 1) if prev else 1
    cp = GraphCheckpoint(
        task_id=task_id,
        run_id=run_id,
        thread_key=thread_key,
        version=version,
        blob=blob,
        metadata_=metadata or {},
    )
    session.add(cp)
    await session.flush()
    return cp


async def load_latest_checkpoint(
    session: AsyncSession,
    *,
    run_id: uuid.UUID | None = None,
    task_id: uuid.UUID | None = None,
    thread_key: str = "default",
) -> GraphCheckpoint | None:
    if run_id is None and task_id is None:
        raise ValueError("run_id or task_id is required")
    query = select(GraphCheckpoint).where(GraphCheckpoint.thread_key == thread_key)
    if task_id is not None:
        query = query.where(GraphCheckpoint.task_id == task_id)
    else:
        query = query.where(GraphCheckpoint.run_id == run_id)
    result = await session.execute(query.order_by(GraphCheckpoint.created_at.desc()).limit(1))
    return result.scalar_one_or_none()


async def upsert_sandbox(
    session: AsyncSession,
    *,
    task_id: uuid.UUID,
    provider: str,
    sandbox_id: str,
    metadata: dict[str, Any] | None = None,
) -> SandboxRow:
    result = await session.execute(
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
    session.add(row)
    await session.flush()
    return row


async def requeue_expired_leases(
    session: AsyncSession,
    *,
    max_retries: int = 3,
) -> int:
    """Reaper: running tasks with expired leases -> queued or failed."""
    now = datetime.now(UTC)
    result = await session.execute(
        select(Run)
        .where(
            Run.status == RunStatus.ACTIVE.value,
            Run.lease_expires_at.is_not(None),
            Run.lease_expires_at < now,
        )
        .with_for_update(skip_locked=True)
    )
    runs = list(result.scalars().all())
    count = 0
    for run in runs:
        task_result = await session.execute(
            select(Task).where(Task.task_id == run.task_id).with_for_update()
        )
        task = task_result.scalar_one_or_none()
        if task is None or task.status != TaskStatus.RUNNING.value:
            continue
        run.status = RunStatus.FAILED.value
        run.ended_at = now
        run.error_code = "lease_expired"
        run.error_message = "Worker lost lease"
        run.worker_id = None
        run.lease_expires_at = None
        if task.retry_count < max_retries and not task.cancel_requested_at:
            task.retry_count += 1
            task.status = TaskStatus.QUEUED.value
            await enqueue_task_work(
                session,
                org_id=task.org_id,
                task_id=task.task_id,
                agent_type=task.agent_type,
                reason="redelivered",
            )
            await append_event(
                session,
                task_id=task.task_id,
                run_id=run.run_id,
                event_type="error",
                payload={"error_code": "lease_expired", "requeued": True},
            )
        else:
            task.status = TaskStatus.FAILED.value
            await append_event(
                session,
                task_id=task.task_id,
                run_id=run.run_id,
                event_type="run_failed",
                payload={"error_code": "lease_expired", "requeued": False},
            )
        count += 1
    return count
