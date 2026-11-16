"""Task API routes (/v1/tasks) backed by Temporal workflows."""

import asyncio
import json
import uuid
from collections.abc import AsyncIterator

from fastapi import APIRouter, Depends, Header, Query
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from alephat_platform.api.deps import (
    AuthContext,
    get_auth,
    get_db,
    get_temporal,
    require_task_actor,
)
from alephat_platform.common.config import get_settings
from alephat_platform.common.db import get_session_factory
from alephat_platform.common.enums import McpMode, MessageKind, TaskSource
from alephat_platform.common.errors import PlatformError
from alephat_platform.common.idempotency import (
    begin_idempotent_command,
    finish_idempotent_command,
)
from alephat_platform.common.models import Approval, Run, RunEvent, Task
from alephat_platform.common.serializers import (
    approval_to_dict,
    event_to_dict,
    run_to_dict,
    task_to_dict,
)
from alephat_platform.common.tasks import TaskStore
from alephat_platform.common.temporal import TemporalPlatformClient

router = APIRouter(prefix="/v1", tags=["tasks"])


class CreateTaskBody(BaseModel):
    agent_type: str = "coding"
    title: str | None = None
    repo: str | None = None
    base_ref: str | None = "main"
    prompt: str | None = None
    source: str = TaskSource.WEB_UI.value
    model: str | None = None
    sandbox_provider: str | None = None
    mcp_server_ids: list[uuid.UUID] | None = None
    mcp_mode: str = McpMode.INHERIT.value
    metadata: dict[str, object] = Field(default_factory=dict)
    thread_id: str | None = None
    source_ref: str | None = None


class MessageBody(BaseModel):
    content: str
    kind: str = MessageKind.USER_GUIDANCE.value


class CancelBody(BaseModel):
    reason: str = "user_requested"


class ApprovalBody(BaseModel):
    decision: str
    comment: str | None = None


@router.post("/tasks", status_code=201)
async def post_task(
    body: CreateTaskBody,
    db: AsyncSession = Depends(get_db),
    auth: AuthContext = Depends(get_auth),
    temporal: TemporalPlatformClient = Depends(get_temporal),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> dict[str, object]:
    route = "POST /v1/tasks"
    stored = await begin_idempotent_command(
        db,
        org_id=auth.org_id,
        route=route,
        key=idempotency_key,
    )
    if stored:
        return stored.body
    settings = get_settings()
    store = TaskStore(db)
    try:
        submission = await store.create_task(
            org_id=auth.org_id,
            user_id=auth.user_id,
            title=body.title,
            prompt=body.prompt,
            repo=body.repo,
            base_ref=body.base_ref,
            source=body.source,
            source_ref=body.source_ref,
            thread_id=body.thread_id,
            agent_type=body.agent_type,
            model=body.model,
            sandbox_provider=body.sandbox_provider,
            mcp_server_ids=body.mcp_server_ids,
            mcp_mode=body.mcp_mode,
            metadata=body.metadata,
            platform_default_model=settings.default_model,
            platform_default_sandbox_provider=settings.default_sandbox_provider,
        )
    except PlatformError:
        raise
    except ValueError as exc:
        raise PlatformError(
            "Invalid task request",
            status=400,
            detail=str(exc),
            error_code="invalid_task_request",
        ) from exc

    task = submission.task
    await db.commit()

    if submission.turn is not None:
        accepted = await temporal.submit_turn(
            task_id=task.task_id,
            org_id=task.org_id,
            turn=submission.turn,
        )
        task.active_run_id = uuid.UUID(accepted.run_id)
        await db.flush()

    response: dict[str, object] = {
        "task_id": str(task.task_id),
        "thread_id": task.thread_id,
        "status": task.status,
        "active_run_id": str(task.active_run_id) if task.active_run_id else None,
        "model": task.model,
        "sandbox_provider": task.sandbox_provider,
        "mcp_server_ids": [str(i) for i in (task.mcp_server_ids or [])],
    }
    await finish_idempotent_command(
        db,
        org_id=auth.org_id,
        route=route,
        key=idempotency_key or "",
        status=201,
        body=response,
    )
    return response


@router.get("/tasks")
async def list_tasks(
    status: str | None = None,
    repo: str | None = None,
    source: str | None = None,
    limit: int = Query(default=50, le=200),
    db: AsyncSession = Depends(get_db),
    auth: AuthContext = Depends(get_auth),
) -> dict[str, object]:
    q = select(Task).where(Task.org_id == auth.org_id).order_by(Task.created_at.desc()).limit(limit)
    if status:
        q = q.where(Task.status == status)
    if repo:
        q = q.where(Task.repo == repo)
    if source:
        q = q.where(Task.source == source)
    result = await db.execute(q)
    tasks = list(result.scalars().all())
    return {"items": [task_to_dict(t) for t in tasks]}


@router.get("/tasks/{task_id}")
async def get_task(
    task_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    auth: AuthContext = Depends(get_auth),
) -> dict[str, object]:
    task = await db.get(Task, task_id)
    if task is None or task.org_id != auth.org_id:
        raise PlatformError(
            "Not Found", status=404, detail="task not found", error_code="not_found"
        )
    body = task_to_dict(task)
    if task.active_run_id:
        run = await db.get(Run, task.active_run_id)
        body["active_run"] = run_to_dict(run) if run else None
    return body


@router.post("/tasks/{task_id}/messages")
async def post_message(
    task_id: uuid.UUID,
    body: MessageBody,
    db: AsyncSession = Depends(get_db),
    auth: AuthContext = Depends(get_auth),
    temporal: TemporalPlatformClient = Depends(get_temporal),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> dict[str, object]:
    route = f"POST /v1/tasks/{task_id}/messages"
    stored = await begin_idempotent_command(
        db, org_id=auth.org_id, route=route, key=idempotency_key
    )
    if stored:
        return stored.body
    task = await db.get(Task, task_id)
    if task is None or task.org_id != auth.org_id:
        raise PlatformError(
            "Not Found", status=404, detail="task not found", error_code="not_found"
        )
    require_task_actor(task, auth)
    store = TaskStore(db)
    try:
        task, msg, turn = await store.append_message(
            task_id=task_id,
            content=body.content,
            kind=body.kind,
            allow_reopen=False,
        )
    except ValueError as exc:
        raise PlatformError(
            "Invalid message",
            status=400,
            detail=str(exc),
            error_code="invalid_message_kind",
        ) from exc

    response: dict[str, object] = {"message_id": str(msg.message_id), "kind": msg.kind}
    await finish_idempotent_command(
        db,
        org_id=auth.org_id,
        route=route,
        key=idempotency_key or "",
        status=200,
        body=response,
    )
    await db.commit()
    await temporal.submit_turn(task_id=task.task_id, org_id=task.org_id, turn=turn)
    return response


@router.post("/tasks/{task_id}/cancel")
async def post_cancel(
    task_id: uuid.UUID,
    body: CancelBody,
    db: AsyncSession = Depends(get_db),
    auth: AuthContext = Depends(get_auth),
    temporal: TemporalPlatformClient = Depends(get_temporal),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> dict[str, object]:
    route = f"POST /v1/tasks/{task_id}/cancel"
    stored = await begin_idempotent_command(
        db, org_id=auth.org_id, route=route, key=idempotency_key
    )
    if stored:
        return stored.body
    task = await db.get(Task, task_id)
    if task is None or task.org_id != auth.org_id:
        raise PlatformError(
            "Not Found", status=404, detail="task not found", error_code="not_found"
        )
    require_task_actor(task, auth)
    task, needs_workflow_cancel = await TaskStore(db).request_cancel(
        task_id=task_id,
        reason=body.reason,
    )
    response = task_to_dict(task)
    await finish_idempotent_command(
        db,
        org_id=auth.org_id,
        route=route,
        key=idempotency_key or "",
        status=200,
        body=response,
    )
    await db.commit()
    if needs_workflow_cancel:
        await temporal.cancel_task(task_id=task.task_id, reason=body.reason)
    return response


@router.get("/tasks/{task_id}/events")
async def list_events(
    task_id: uuid.UUID,
    after_seq: int = 0,
    limit: int = Query(default=100, le=500),
    db: AsyncSession = Depends(get_db),
    auth: AuthContext = Depends(get_auth),
) -> dict[str, object]:
    task = await db.get(Task, task_id)
    if task is None or task.org_id != auth.org_id:
        raise PlatformError(
            "Not Found", status=404, detail="task not found", error_code="not_found"
        )
    result = await db.execute(
        select(RunEvent)
        .where(RunEvent.task_id == task_id, RunEvent.seq > after_seq)
        .order_by(RunEvent.seq)
        .limit(limit)
    )
    events = list(result.scalars().all())
    return {"items": [event_to_dict(e) for e in events]}


class TaskEventStream:
    """SSE stream producer reading ordered task RunEvents from PostgreSQL."""

    def __init__(self, *, task_id: uuid.UUID, org_id: uuid.UUID, last_event_id: str | None) -> None:
        self.task_id = task_id
        self.org_id = org_id
        self.last_event_id = last_event_id

    async def iterate(self) -> AsyncIterator[str]:
        try:
            last_seq = int(self.last_event_id or 0)
        except ValueError:
            last_seq = 0
        factory = get_session_factory()
        while True:
            async with factory() as session:
                task = await session.get(Task, self.task_id)
                if task is None or task.org_id != self.org_id:
                    yield f"event: error\ndata: {json.dumps({'error': 'not_found'})}\n\n"
                    return
                result = await session.execute(
                    select(RunEvent)
                    .where(RunEvent.task_id == self.task_id, RunEvent.seq > last_seq)
                    .order_by(RunEvent.seq)
                    .limit(50)
                )
                events = list(result.scalars().all())
                for event in events:
                    last_seq = event.seq
                    data = event_to_dict(event)
                    yield f"id: {event.seq}\nevent: run_event\ndata: {json.dumps(data)}\n\n"
                yield f"event: task_status\ndata: {json.dumps({'status': task.status})}\n\n"
                if task.status in {"succeeded", "failed", "cancelled"}:
                    return
            await asyncio.sleep(1.0)


@router.get("/tasks/{task_id}/events/stream")
async def stream_events(
    task_id: uuid.UUID,
    auth: AuthContext = Depends(get_auth),
    last_event_id: str | None = Header(default=None, alias="Last-Event-ID"),
) -> StreamingResponse:
    stream = TaskEventStream(task_id=task_id, org_id=auth.org_id, last_event_id=last_event_id)
    return StreamingResponse(stream.iterate(), media_type="text/event-stream")


@router.get("/tasks/{task_id}/runs")
async def list_runs(
    task_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    auth: AuthContext = Depends(get_auth),
) -> dict[str, object]:
    task = await db.get(Task, task_id)
    if task is None or task.org_id != auth.org_id:
        raise PlatformError(
            "Not Found", status=404, detail="task not found", error_code="not_found"
        )
    result = await db.execute(
        select(Run).where(Run.task_id == task_id).order_by(Run.created_at.desc())
    )
    return {"items": [run_to_dict(r) for r in result.scalars().all()]}


@router.get("/runs/{run_id}")
async def get_run(
    run_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    auth: AuthContext = Depends(get_auth),
) -> dict[str, object]:
    run = await db.get(Run, run_id)
    if run is None:
        raise PlatformError("Not Found", status=404, detail="run not found", error_code="not_found")
    task = await db.get(Task, run.task_id)
    if task is None or task.org_id != auth.org_id:
        raise PlatformError("Not Found", status=404, detail="run not found", error_code="not_found")
    return run_to_dict(run)


@router.get("/tasks/{task_id}/approvals/pending")
async def pending_approvals(
    task_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    auth: AuthContext = Depends(get_auth),
) -> dict[str, object]:
    task = await db.get(Task, task_id)
    if task is None or task.org_id != auth.org_id:
        raise PlatformError(
            "Not Found", status=404, detail="task not found", error_code="not_found"
        )
    result = await db.execute(
        select(Approval).where(Approval.task_id == task_id, Approval.status == "pending")
    )
    return {"items": [approval_to_dict(a) for a in result.scalars().all()]}


@router.post("/approvals/{approval_id}/decision")
async def approval_decision(
    approval_id: uuid.UUID,
    body: ApprovalBody,
    db: AsyncSession = Depends(get_db),
    auth: AuthContext = Depends(get_auth),
    temporal: TemporalPlatformClient = Depends(get_temporal),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> dict[str, object]:
    route = f"POST /v1/approvals/{approval_id}/decision"
    stored = await begin_idempotent_command(
        db, org_id=auth.org_id, route=route, key=idempotency_key
    )
    if stored:
        return stored.body
    if body.decision not in {"approved", "rejected"}:
        raise PlatformError(
            "Invalid decision",
            status=400,
            detail="decision must be approved or rejected",
            error_code="invalid_decision",
        )
    existing_approval = await db.get(Approval, approval_id)
    if existing_approval is None:
        raise PlatformError(
            "Not Found", status=404, detail="approval not found", error_code="not_found"
        )
    existing_task = await db.get(Task, existing_approval.task_id)
    if existing_task is None or existing_task.org_id != auth.org_id:
        raise PlatformError(
            "Not Found", status=404, detail="approval not found", error_code="not_found"
        )
    require_task_actor(existing_task, auth)
    task, approval, resolution = await TaskStore(db).record_approval_decision(
        approval_id=approval_id,
        decision=body.decision,
        user_id=auth.user_id,
        comment=body.comment,
    )
    response = approval_to_dict(approval)
    await finish_idempotent_command(
        db,
        org_id=auth.org_id,
        route=route,
        key=idempotency_key or "",
        status=200,
        body=response,
    )
    await db.commit()
    await temporal.submit_approval(task_id=task.task_id, resolution=resolution)
    return response
