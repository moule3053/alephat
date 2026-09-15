"""Transactional outbox writer and publisher loop."""

import asyncio
import logging
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from alephat_platform.common.messaging import NatsBus, envelope
from alephat_platform.common.models import Outbox

logger = logging.getLogger(__name__)


async def enqueue_outbox(
    session: AsyncSession,
    *,
    aggregate_type: str,
    aggregate_id: str,
    subject: str,
    payload: dict[str, Any],
) -> uuid.UUID:
    row = Outbox(
        aggregate_type=aggregate_type,
        aggregate_id=aggregate_id,
        subject=subject,
        payload=payload,
    )
    session.add(row)
    await session.flush()
    return row.id


async def enqueue_task_work(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    task_id: uuid.UUID,
    agent_type: str,
    reason: str = "created",
) -> uuid.UUID:
    from alephat_platform.common.messaging import enqueue_subject, task_enqueue_payload

    subject = enqueue_subject(str(org_id), agent_type, str(task_id))
    body = envelope(
        "task.enqueue",
        org_id=str(org_id),
        task_id=str(task_id),
        payload=task_enqueue_payload(str(task_id), agent_type, reason=reason),
    )
    return await enqueue_outbox(
        session,
        aggregate_type="task",
        aggregate_id=str(task_id),
        subject=subject,
        payload=body,
    )


async def enqueue_control(
    session: AsyncSession,
    *,
    org_id: uuid.UUID | None,
    task_id: uuid.UUID,
    control: str,
    run_id: uuid.UUID | None = None,
    extra: dict[str, Any] | None = None,
) -> uuid.UUID:
    from alephat_platform.common.messaging import control_subject

    payload = {
        "task_id": str(task_id),
        "run_id": str(run_id) if run_id else None,
        "control": control,
        **(extra or {}),
    }
    body = envelope(
        "task.control",
        org_id=str(org_id) if org_id else None,
        task_id=str(task_id),
        run_id=str(run_id) if run_id else None,
        payload=payload,
    )
    return await enqueue_outbox(
        session,
        aggregate_type="task",
        aggregate_id=str(task_id),
        subject=control_subject(str(task_id)),
        payload=body,
    )


async def publish_pending(session: AsyncSession, bus: NatsBus, *, limit: int = 100) -> int:
    result = await session.execute(
        select(Outbox)
        .where(Outbox.published_at.is_(None))
        .order_by(Outbox.created_at)
        .limit(limit)
        .with_for_update(skip_locked=True)
    )
    rows = list(result.scalars().all())
    published = 0
    for row in rows:
        msg_id = str(row.id)
        body = dict(row.payload)
        body.setdefault("msg_id", msg_id)
        await bus.publish(row.subject, body, msg_id=msg_id)
        await session.execute(
            update(Outbox).where(Outbox.id == row.id).values(published_at=datetime.now(UTC))
        )
        published += 1
    return published


async def outbox_publisher_loop(
    session_factory: Any,
    bus: NatsBus,
    *,
    interval: float = 1.0,
    stop_event: asyncio.Event | None = None,
    raise_on_error: bool = False,
) -> None:
    stop = stop_event or asyncio.Event()
    while not stop.is_set():
        try:
            async with session_factory() as session:
                n = await publish_pending(session, bus)
                await session.commit()
                if n:
                    logger.info("Published %s outbox messages", n)
        except Exception:
            logger.exception("Outbox publish failed")
            if raise_on_error:
                raise
        try:
            await asyncio.wait_for(stop.wait(), timeout=interval)
        except TimeoutError:
            continue


async def outbox_supervisor_loop(
    session_factory: Any,
    *,
    nats_url: str,
    interval: float = 1.0,
    stop_event: asyncio.Event | None = None,
    on_connection: Callable[[NatsBus | None], None] | None = None,
) -> None:
    """Reconnect the outbox publisher after initial or runtime NATS failures."""
    stop = stop_event or asyncio.Event()
    while not stop.is_set():
        bus = NatsBus(nats_url)
        try:
            await bus.connect()
            if on_connection:
                on_connection(bus)
            await outbox_publisher_loop(
                session_factory,
                bus,
                interval=interval,
                stop_event=stop,
                raise_on_error=True,
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Outbox publisher disconnected; retrying")
        finally:
            if on_connection:
                on_connection(None)
            try:
                await bus.close()
            except Exception:
                logger.debug("Failed closing NATS connection", exc_info=True)
        if not stop.is_set():
            try:
                await asyncio.wait_for(stop.wait(), timeout=max(interval, 1.0))
            except TimeoutError:
                pass
