"""Transactional idempotency for mutating HTTP commands."""

import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from alephat_platform.common.errors import PlatformError
from alephat_platform.common.models import CommandIdempotency


@dataclass(frozen=True)
class StoredResponse:
    status: int
    body: dict[str, Any]


async def begin_idempotent_command(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    route: str,
    key: str | None,
) -> StoredResponse | None:
    if not key or not key.strip():
        raise PlatformError(
            "Idempotency key required",
            status=400,
            detail="Idempotency-Key header is required",
            error_code="idempotency_key_required",
        )
    normalized = key.strip()
    lock_key = f"{org_id}:{route}:{normalized}"
    await session.execute(
        text("SELECT pg_advisory_xact_lock(hashtextextended(:lock_key, 0))"),
        {"lock_key": lock_key},
    )
    existing = await session.execute(
        select(CommandIdempotency).where(
            CommandIdempotency.org_id == org_id,
            CommandIdempotency.route == route,
            CommandIdempotency.idempotency_key == normalized,
        )
    )
    row = existing.scalar_one_or_none()
    if row is None:
        return None
    return StoredResponse(status=row.response_status, body=dict(row.response_body))


async def finish_idempotent_command(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    route: str,
    key: str,
    status: int,
    body: dict[str, Any],
) -> None:
    session.add(
        CommandIdempotency(
            org_id=org_id,
            route=route,
            idempotency_key=key.strip(),
            response_status=status,
            response_body=body,
        )
    )
    await session.flush()
