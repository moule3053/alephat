"""NATS JetStream helpers and message envelopes."""

import json
import logging
import uuid
from datetime import UTC, datetime
from typing import Any

from alephat_platform.common.enums import SCHEMA_VERSION

logger = logging.getLogger(__name__)

STREAM_TASKS = "TASKS"
STREAM_CONTROL = "CONTROL"
STREAM_EVENTS = "EVENTS"
STREAM_DLQ = "DLQ"

SUBJECT_ENQUEUE_PREFIX = "tasks.enqueue"
SUBJECT_CONTROL_PREFIX = "tasks.control"
SUBJECT_EVENTS_PREFIX = "tasks.events"


def enqueue_subject(org_id: str, agent_type: str, task_id: str) -> str:
    return f"{SUBJECT_ENQUEUE_PREFIX}.{org_id}.{agent_type}.{task_id}"


def control_subject(task_id: str) -> str:
    return f"{SUBJECT_CONTROL_PREFIX}.{task_id}"


def events_subject(task_id: str) -> str:
    return f"{SUBJECT_EVENTS_PREFIX}.{task_id}"


def envelope(
    type_: str,
    *,
    org_id: str | None,
    task_id: str | None,
    payload: dict[str, Any],
    run_id: str | None = None,
    msg_id: str | None = None,
) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "msg_id": msg_id or str(uuid.uuid4()),
        "type": type_,
        "occurred_at": datetime.now(UTC).isoformat(),
        "org_id": org_id,
        "task_id": task_id,
        "run_id": run_id,
        "payload": payload,
    }


def task_enqueue_payload(
    task_id: str,
    agent_type: str,
    *,
    reason: str = "created",
    priority: int = 50,
    trace_id: str | None = None,
) -> dict[str, Any]:
    return {
        "task_id": task_id,
        "agent_type": agent_type,
        "reason": reason,
        "priority": priority,
        "not_before": None,
        "trace_id": trace_id,
    }


class NatsBus:
    """Thin JetStream wrapper. Degrades gracefully if NATS is unavailable."""

    def __init__(self, url: str) -> None:
        self.url = url
        self._nc: Any = None
        self._js: Any = None

    async def connect(self) -> None:
        try:
            import nats
        except ImportError as exc:
            raise RuntimeError("nats-py is required: pip install nats-py") from exc

        self._nc = await nats.connect(self.url)
        self._js = self._nc.jetstream()
        await self._ensure_streams()
        logger.info("Connected to NATS at %s", self.url)

    async def _ensure_streams(self) -> None:
        assert self._js is not None
        from nats.js.api import RetentionPolicy, StreamConfig

        specs = [
            (STREAM_TASKS, [f"{SUBJECT_ENQUEUE_PREFIX}.>"], RetentionPolicy.WORK_QUEUE),
            (STREAM_CONTROL, [f"{SUBJECT_CONTROL_PREFIX}.>"], RetentionPolicy.LIMITS),
            (STREAM_EVENTS, [f"{SUBJECT_EVENTS_PREFIX}.>"], RetentionPolicy.LIMITS),
            (STREAM_DLQ, ["dlq.>"], RetentionPolicy.LIMITS),
        ]
        for name, subjects, retention in specs:
            try:
                await self._js.stream_info(name)
            except Exception:
                await self._js.add_stream(
                    StreamConfig(name=name, subjects=subjects, retention=retention)
                )
                logger.info("Created JetStream stream %s", name)

    async def publish(self, subject: str, body: dict[str, Any], msg_id: str | None = None) -> None:
        if self._js is None:
            raise RuntimeError("NATS not connected")
        data = json.dumps(body).encode()
        headers = {"Nats-Msg-Id": msg_id or body.get("msg_id") or str(uuid.uuid4())}
        await self._js.publish(subject, data, headers=headers)

    async def close(self) -> None:
        if self._nc is not None:
            await self._nc.drain()
            self._nc = None
            self._js = None

    @property
    def js(self) -> Any:
        return self._js

    @property
    def connected(self) -> bool:
        return bool(self._nc is not None and getattr(self._nc, "is_connected", False))
