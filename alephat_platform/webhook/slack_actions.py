"""Signed Slack buttons resolve to authorized platform task operations."""

import hashlib
import logging
import uuid

from pydantic import BaseModel, JsonValue, TypeAdapter
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from alephat_platform.common.config import get_settings
from alephat_platform.common.db import session_scope
from alephat_platform.common.enums import TaskStatus
from alephat_platform.common.models import Approval, OrgMembership, Task, User
from alephat_platform.common.slack import SlackAction, SlackActionSigner, SlackClient, SlackMessage
from alephat_platform.common.tasks import (
    add_message,
    append_event,
    cancel_task,
    create_task,
    decide_approval,
)
from alephat_platform.webhook.handlers import accept_delivery

logger = logging.getLogger(__name__)


class SlackActor(BaseModel):
    user_id: uuid.UUID
    role: str


class SlackActionHandler:
    def __init__(self, org_id: str) -> None:
        settings = get_settings()
        self.org_id = uuid.UUID(org_id)
        self.users = TypeAdapter(dict[str, uuid.UUID]).validate_json(settings.slack_user_map)
        self.signer = SlackActionSigner(settings.slack_signing_secret or settings.slack_bot_token)
        self.client = SlackClient(settings.slack_bot_token)

    async def authorize(
        self, session: AsyncSession, team: str, slack_user: str, task: Task
    ) -> SlackActor:
        user_id = self.users.get(f"{team}:{slack_user}")
        if user_id is None or await session.get(User, user_id) is None:
            raise ValueError(
                "Your Slack account is not linked. Ask the administrator to configure SLACK_USER_MAP."
            )
        membership = await session.get(OrgMembership, (self.org_id, user_id))
        if membership is None or task.org_id != self.org_id:
            raise ValueError("You are not a member of this task's organization.")
        if membership.role != "admin" and task.created_by != user_id:
            raise ValueError("Only the task creator or an administrator can use these actions.")
        return SlackActor(user_id=user_id, role=membership.role)

    async def handle(self, payload: dict[str, JsonValue]) -> dict[str, JsonValue]:
        channel_obj, user_obj, team_obj = (
            payload.get("channel"),
            payload.get("user"),
            payload.get("team"),
        )
        channel = str(channel_obj.get("id") or "") if isinstance(channel_obj, dict) else ""
        slack_user = str(user_obj.get("id") or "") if isinstance(user_obj, dict) else ""
        team = str(team_obj.get("id") or "") if isinstance(team_obj, dict) else ""
        actions = payload.get("actions")
        if not isinstance(actions, list) or len(actions) != 1:
            return {"accepted": True, "ignored": True}
        button = actions[0]
        if not isinstance(button, dict) or not str(button.get("action_id") or "").startswith(
            "alephat:"
        ):
            return {"accepted": True, "ignored": True}
        token = str(button.get("value") or "")
        try:
            action = await self.signer.verify(token)
            message_obj = payload.get("message")
            message_ts = str(message_obj.get("ts") or "") if isinstance(message_obj, dict) else ""
            thread_ts = (
                str(message_obj.get("thread_ts") or message_ts)
                if isinstance(message_obj, dict)
                else ""
            )
            if (
                channel != action.channel
                or thread_ts != action.thread_ts
                or str(button.get("action_id")) != f"alephat:{action.action}"
            ):
                raise ValueError("This button belongs to another conversation.")
            async with session_scope() as session:
                task = (
                    await session.execute(
                        select(Task).where(Task.task_id == action.task_id).with_for_update()
                    )
                ).scalar_one_or_none()
                if (
                    task is None
                    or task.active_run_id != action.run_id
                    or (task.metadata_ or {}).get("slack_team_id") != team
                ):
                    raise ValueError("This task has changed. Use its latest response.")
                actor = await self.authorize(session, team, slack_user, task)
                delivery_id = hashlib.sha256(token.encode()).hexdigest()
                if not await accept_delivery(
                    session,
                    provider="slack_action",
                    delivery_id=delivery_id,
                    raw_body=token.encode(),
                ):
                    return {"accepted": True, "duplicate": True}
                context = (
                    str(message_obj.get("text") or "") if isinstance(message_obj, dict) else ""
                )
                await self.apply(session, task, actor, action, context=context)
                await append_event(
                    session,
                    task_id=task.task_id,
                    run_id=task.active_run_id,
                    event_type="slack_action",
                    payload={
                        "user_id": str(actor.user_id),
                        "action": action.action,
                        "label": action.label,
                    },
                )
                if message_ts and isinstance(message_obj, dict):
                    original = SlackMessage.model_validate(message_obj)
                    original.blocks = [
                        block for block in original.blocks if block.get("type") != "actions"
                    ]
                    original.blocks.append(
                        {
                            "type": "context",
                            "elements": [
                                {"type": "plain_text", "text": f"{action.label}: request received."}
                            ],
                        }
                    )
                    try:
                        await self.client.update(channel, message_ts, original)
                    except Exception:
                        logger.warning("Could not update clicked Slack message", exc_info=True)
        except ValueError as exc:
            logger.warning(
                "Slack action rejected", extra={"slack_channel": channel, "reason": str(exc)}
            )
            if channel and slack_user:
                await self.client.ephemeral(channel, slack_user, str(exc))
            return {"accepted": True, "rejected": True}
        return {"accepted": True}

    async def apply(
        self,
        session: AsyncSession,
        task: Task,
        actor: SlackActor,
        action: SlackAction,
        *,
        context: str = "",
    ) -> None:
        content = f'Clicked "{action.label}".' + (f"\n{action.value}" if action.value else "")
        if action.action in {"approve", "reject"}:
            approval = (
                await session.execute(
                    select(Approval)
                    .where(
                        Approval.task_id == task.task_id,
                        Approval.run_id == action.run_id,
                        Approval.status == "pending",
                    )
                    .with_for_update()
                )
            ).scalar_one_or_none()
            if approval is None or task.status != TaskStatus.PARKED.value:
                raise ValueError("There is no pending plan approval for this task.")
            await decide_approval(
                session,
                approval_id=approval.approval_id,
                decision="approved" if action.action == "approve" else "rejected",
                user_id=actor.user_id,
                comment=content,
            )
            await add_message(session, task_id=task.task_id, content=content)
        elif action.action == "cancel":
            if task.status not in {"queued", "running", "parked"}:
                raise ValueError("This task has already finished.")
            await add_message(session, task_id=task.task_id, content=content)
            await cancel_task(session, task_id=task.task_id, reason="slack_button")
        elif task.status in {"queued", "running", "parked"}:
            await add_message(session, task_id=task.task_id, content=content)
        else:
            # Terminal tasks stay immutable. A follow-up gets a new task in the same Slack thread.
            settings = get_settings()
            await create_task(
                session,
                org_id=task.org_id,
                user_id=actor.user_id,
                title=action.label,
                prompt=f"Previous task: {task.title or task.prompt or ''}\n\nPrevious response:\n{context}\n\n{content}",
                repo=task.repo,
                base_ref=task.base_ref,
                source="slack",
                source_ref=task.source_ref,
                thread_id=f"slack-action:{uuid.uuid4()}",
                agent_type=task.agent_type,
                model=task.model,
                sandbox_provider=task.sandbox_provider,
                metadata={
                    key: value
                    for key, value in (task.metadata_ or {}).items()
                    if key in {"channel", "thread_ts", "message_ts", "slack_team_id"}
                },
                platform_default_model=settings.default_model,
                platform_default_sandbox_provider=settings.default_sandbox_provider,
            )
