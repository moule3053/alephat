"""Persist normalized ingress commands."""

import hashlib
import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from alephat_platform.common.config import get_settings
from alephat_platform.common.enums import IngressCommand, MessageKind, TaskStatus
from alephat_platform.common.models import Message, Task, WebhookDelivery
from alephat_platform.common.outbox import enqueue_control, enqueue_task_work
from alephat_platform.common.tasks import add_message, cancel_task, create_task


async def accept_delivery(
    session: AsyncSession,
    *,
    provider: str,
    delivery_id: str,
    raw_body: bytes,
) -> bool:
    """Return True if this is a new delivery, False if duplicate."""
    result = await session.execute(
        select(WebhookDelivery).where(
            WebhookDelivery.provider == provider,
            WebhookDelivery.delivery_id == delivery_id,
        )
    )
    if result.scalar_one_or_none() is not None:
        return False
    session.add(
        WebhookDelivery(
            provider=provider,
            delivery_id=delivery_id,
            payload_hash=hashlib.sha256(raw_body).hexdigest(),
        )
    )
    await session.flush()
    return True


async def apply_ingress_command(session: AsyncSession, command: dict[str, Any]) -> dict[str, Any]:
    settings = get_settings()
    cmd = command.get("command")
    if cmd == IngressCommand.IGNORE.value:
        return {"accepted": True, "ignored": True, "metadata": command.get("metadata")}

    org_id = uuid.UUID(command["org_id"])
    user_id = uuid.UUID(settings.default_user_id)
    thread_id = command.get("thread_id")
    if not thread_id:
        return {"accepted": True, "ignored": True, "reason": "no_thread"}

    # Find existing task by thread
    result = await session.execute(
        select(Task).where(Task.org_id == org_id, Task.thread_id == thread_id)
    )
    task = result.scalar_one_or_none()

    if cmd == IngressCommand.REQUEST_CANCEL.value and task:
        task = await cancel_task(session, task_id=task.task_id, reason="webhook_cancel")
        return {"accepted": True, "task_id": str(task.task_id), "status": task.status}

    message = command.get("message") or {}
    content = message.get("content") or ""

    if task is None:
        if cmd in {
            IngressCommand.UPSERT_AND_ENQUEUE.value,
            IngressCommand.CI_FAILURE.value,
        }:
            # CI failure without existing task: only enqueue if policy says so (v1: create)
            task = await create_task(
                session,
                org_id=org_id,
                user_id=user_id,
                title=command.get("title"),
                prompt=content,
                repo=command.get("repo"),
                source=command.get("source", "api"),
                source_ref=command.get("source_ref"),
                thread_id=thread_id,
                agent_type=command.get("agent_type", "coding"),
                metadata=command.get("metadata") or {},
                platform_default_model=settings.default_model,
                platform_default_sandbox_provider=settings.default_sandbox_provider,
            )
            return {
                "accepted": True,
                "task_id": str(task.task_id),
                "status": task.status,
                "created": True,
            }
        return {"accepted": True, "ignored": True, "reason": "no_task"}

    # Existing task: append message and enqueue/control
    if content:
        kind = message.get("kind") or MessageKind.USER_GUIDANCE.value
        if task.status == TaskStatus.RUNNING.value:
            session.add(
                Message(
                    task_id=task.task_id,
                    role="user",
                    kind=kind,
                    content=content,
                )
            )
            await enqueue_control(
                session,
                org_id=task.org_id,
                task_id=task.task_id,
                control="append_message",
                run_id=task.active_run_id,
            )
        else:
            await add_message(session, task_id=task.task_id, content=content, kind=kind)
    elif task.status in {TaskStatus.QUEUED.value, TaskStatus.PARKED.value}:
        if task.status == TaskStatus.PARKED.value:
            task.status = TaskStatus.QUEUED.value
        await enqueue_task_work(
            session,
            org_id=task.org_id,
            task_id=task.task_id,
            agent_type=task.agent_type,
            reason="resumed",
        )

    return {"accepted": True, "task_id": str(task.task_id), "status": task.status}
