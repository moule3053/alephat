"""Persist normalized ingress commands and dispatch to Temporal."""

import hashlib
import logging
import uuid

from pydantic import TypeAdapter
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from alephat_platform.common.config import get_settings
from alephat_platform.common.enums import IngressCommand, MessageKind
from alephat_platform.common.errors import ForbiddenError
from alephat_platform.common.models import (
    AllowedSlackBot,
    OrgMembership,
    Task,
    User,
    WebhookDelivery,
)
from alephat_platform.common.tasks import TaskDispatcher
from alephat_platform.common.temporal import TemporalPlatformClient, TurnEnvelope

logger = logging.getLogger(__name__)


class IngressCommandService:
    """Deduplicate and apply normalized GitHub/Slack ingress commands over Temporal."""

    def __init__(
        self,
        session: AsyncSession,
        temporal: TemporalPlatformClient | None = None,
    ) -> None:
        self.session = session
        self.temporal = temporal or TemporalPlatformClient()
        self.dispatcher = TaskDispatcher(session, self.temporal)

    async def accept_delivery(
        self,
        *,
        provider: str,
        delivery_id: str,
        raw_body: bytes,
    ) -> bool:
        result = await self.session.execute(
            select(WebhookDelivery).where(
                WebhookDelivery.provider == provider,
                WebhookDelivery.delivery_id == delivery_id,
            )
        )
        if result.scalar_one_or_none() is not None:
            return False
        self.session.add(
            WebhookDelivery(
                provider=provider,
                delivery_id=delivery_id,
                payload_hash=hashlib.sha256(raw_body).hexdigest(),
            )
        )
        await self.session.flush()
        return True

    async def _enqueue_slack_ack(self, token: str, metadata: object) -> None:
        if not token or not isinstance(metadata, dict):
            return
        channel = metadata.get("channel")
        message_ts = metadata.get("message_ts") or metadata.get("thread_ts")
        if not isinstance(channel, str) or not isinstance(message_ts, str):
            return
        try:
            await self.temporal.acknowledge_slack_message(
                channel=channel,
                message_ts=message_ts,
            )
        except Exception:
            logger.warning(
                "Could not schedule Slack reaction standalone activity",
                extra={"slack_channel": channel},
                exc_info=True,
            )

    async def apply(self, command: dict[str, object]) -> dict[str, object]:
        settings = get_settings()
        cmd = command.get("command")
        if cmd == IngressCommand.IGNORE.value:
            return {"accepted": True, "ignored": True, "metadata": command.get("metadata")}

        org_id = uuid.UUID(str(command["org_id"]))
        user_id = uuid.UUID(settings.default_user_id)
        raw_meta = command.get("metadata")
        metadata: dict[str, object] = dict(raw_meta) if isinstance(raw_meta, dict) else {}

        if command.get("source") == "slack":
            raw_actor = command.get("actor")
            actor = raw_actor if isinstance(raw_actor, dict) else {}
            identities = TypeAdapter(dict[str, uuid.UUID]).validate_json(settings.slack_user_map)
            mapped_user = identities.get(f"{metadata.get('slack_team_id')}:{actor.get('id')}")
            if mapped_user is not None:
                if (
                    await self.session.get(User, mapped_user) is None
                    or await self.session.get(OrgMembership, (org_id, mapped_user)) is None
                ):
                    raise ForbiddenError("Linked Slack user is not a member of this organization")
                user_id = mapped_user
            elif not settings.api_auth_disabled and not metadata.get("triggering_bot_id"):
                raise ForbiddenError("Slack user is not linked to a platform account")
            bot_id = metadata.get("triggering_bot_id")
            if isinstance(bot_id, str) and bot_id:
                allowed = (
                    await self.session.execute(
                        select(AllowedSlackBot).where(
                            AllowedSlackBot.org_id == org_id,
                            AllowedSlackBot.team_id == str(metadata.get("slack_team_id") or ""),
                            AllowedSlackBot.bot_id == bot_id,
                        )
                    )
                ).scalar_one_or_none()
                if allowed is None:
                    return {"accepted": True, "ignored": True, "reason": "bot_not_allowed"}
                metadata["triggering_bot"] = {
                    "key": f"{allowed.team_id}:{allowed.bot_id}",
                    "name": allowed.name,
                }
                command["metadata"] = metadata

        thread_id_raw = command.get("thread_id")
        thread_id = str(thread_id_raw) if isinstance(thread_id_raw, str) and thread_id_raw else ""
        if not thread_id:
            return {"accepted": True, "ignored": True, "reason": "no_thread"}

        result = await self.session.execute(
            select(Task).where(Task.org_id == org_id, Task.thread_id == thread_id)
        )
        task = result.scalar_one_or_none()

        if cmd == IngressCommand.REQUEST_CANCEL.value and task:
            task = await self.dispatcher.cancel(task_id=task.task_id, reason="webhook_cancel")
            return {"accepted": True, "task_id": str(task.task_id), "status": task.status}

        raw_msg = command.get("message")
        message = raw_msg if isinstance(raw_msg, dict) else {}
        content = str(message.get("content") or "")

        if task is None:
            if metadata.get("require_existing_task"):
                return {"accepted": True, "ignored": True, "metadata": {"reason": "no_mention"}}
            if cmd in {
                IngressCommand.UPSERT_AND_ENQUEUE.value,
                IngressCommand.CI_FAILURE.value,
            }:
                title_raw = command.get("title")
                repo_raw = command.get("repo")
                source_ref_raw = command.get("source_ref")
                task = await self.dispatcher.create_and_start(
                    org_id=org_id,
                    user_id=user_id,
                    title=str(title_raw) if isinstance(title_raw, str) else None,
                    prompt=content,
                    repo=str(repo_raw) if isinstance(repo_raw, str) else None,
                    source=str(command.get("source") or "api"),
                    source_ref=str(source_ref_raw) if isinstance(source_ref_raw, str) else None,
                    thread_id=thread_id,
                    agent_type=str(command.get("agent_type") or "coding"),
                    metadata=metadata,
                    platform_default_model=settings.default_model,
                    platform_default_sandbox_provider=settings.default_sandbox_provider,
                )
                await self._enqueue_slack_ack(settings.slack_bot_token, metadata)
                return {
                    "accepted": True,
                    "task_id": str(task.task_id),
                    "status": task.status,
                    "created": True,
                }
            return {"accepted": True, "ignored": True, "reason": "no_task"}

        repo_raw = command.get("repo")
        if not task.repo and isinstance(repo_raw, str) and repo_raw:
            task.repo = repo_raw

        msg_ts_raw = metadata.get("message_ts")
        msg_ts = str(msg_ts_raw) if isinstance(msg_ts_raw, str) and msg_ts_raw else None
        if content:
            kind = str(message.get("kind") or MessageKind.USER_GUIDANCE.value)
            task, _ = await self.dispatcher.send_message(
                task_id=task.task_id,
                content=content,
                kind=kind,
                allow_reopen=True,
                message_ts=msg_ts,
            )
        elif task.status in {"queued", "parked"}:
            resume_id = str(uuid.uuid4())
            await self.session.commit()
            await self.temporal.submit_turn(
                task_id=task.task_id,
                org_id=task.org_id,
                turn=TurnEnvelope(
                    turn_id=resume_id,
                    message_id=resume_id,
                    content="",
                    message_ts=msg_ts,
                ),
            )

        await self._enqueue_slack_ack(settings.slack_bot_token, metadata)
        return {"accepted": True, "task_id": str(task.task_id), "status": task.status}
