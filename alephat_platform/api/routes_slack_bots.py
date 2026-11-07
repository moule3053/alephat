"""Allow trusted Slack bots to mention Alephat and start tasks."""

import re
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, field_validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from alephat_platform.api.deps import AuthContext, get_auth, get_db, require_admin
from alephat_platform.common.config import get_settings
from alephat_platform.common.models import AllowedSlackBot
from alephat_platform.common.slack import SlackClient

router = APIRouter(prefix="/dashboard/api/slack", tags=["slack-bots"])
_BOT_ID = re.compile(r"[BUW][A-Z0-9]{1,31}")


class AllowSlackBotBody(BaseModel):
    bot_id: str

    @field_validator("bot_id")
    @classmethod
    def validate_bot_id(cls, value: str) -> str:
        value = value.strip()
        if not _BOT_ID.fullmatch(value):
            raise ValueError("Enter a Slack bot ID (B…) or bot member ID (U…)")
        return value


class SlackBotDirectory:
    def __init__(self, token: str) -> None:
        if not token:
            raise HTTPException(503, "SLACK_BOT_TOKEN is not configured")
        self.client = SlackClient(token)

    async def _call(self, method: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
        try:
            return await self.client.method(method, body)
        except RuntimeError as exc:
            message = str(exc)
            if "missing_scope" in message:
                raise HTTPException(
                    403,
                    "Add the users:read bot scope in the Slack app, reinstall it, and try again.",
                ) from exc
            raise HTTPException(502, "Slack could not be reached") from exc

    async def identity(self) -> dict[str, Any]:
        return await self._call("auth.test")

    async def options(self) -> list[dict[str, str]]:
        auth = await self.identity()
        team_id = str(auth.get("team_id") or "")
        cursor = ""
        found: dict[str, dict[str, str]] = {}
        for _ in range(10):
            page = await self._call("users.list", {"limit": 200, "cursor": cursor})
            members = page.get("members")
            if isinstance(members, list):
                for member in members:
                    option = self._option(member, auth, team_id)
                    if option:
                        found[option["bot_id"]] = option
            metadata = page.get("response_metadata")
            cursor = str(metadata.get("next_cursor") or "") if isinstance(metadata, dict) else ""
            if not cursor:
                break
        return sorted(found.values(), key=lambda item: item["name"].casefold())

    async def verify(self, raw_id: str) -> dict[str, str]:
        auth = await self.identity()
        team_id = str(auth.get("team_id") or "")
        bot_id = raw_id
        user: dict[str, Any] | None = None
        if not raw_id.startswith("B"):
            user = self._object(await self._call("users.info", {"user": raw_id}), "user")
            profile = user.get("profile") if isinstance(user, dict) else None
            resolved = profile.get("bot_id") if isinstance(profile, dict) else None
            if not isinstance(resolved, str) or not resolved:
                raise HTTPException(400, "That Slack member is not a bot.")
            bot_id = resolved
        bot = self._object(await self._call("bots.info", {"bot": bot_id}), "bot")
        if bot.get("deleted") or bot.get("id") != bot_id:
            raise HTTPException(400, "That Slack bot is unavailable.")
        user_id = str(bot.get("user_id") or "")
        if bot_id == auth.get("bot_id") or (user_id and user_id == auth.get("user_id")):
            raise HTTPException(400, "Alephat cannot trigger itself.")
        if user_id and user is None:
            user = self._object(await self._call("users.info", {"user": user_id}), "user")
        if user_id and (
            not isinstance(user, dict)
            or user.get("is_bot") is not True
            or user.get("deleted")
            or user.get("team_id") != team_id
            or user.get("id") != user_id
        ):
            raise HTTPException(400, "Choose an active bot in this Slack workspace.")
        profile = user.get("profile") if isinstance(user, dict) else None
        image = profile.get("image_48") if isinstance(profile, dict) else ""
        name = str(bot.get("name") or bot_id)
        return {
            "team_id": team_id,
            "bot_id": bot_id,
            "user_id": user_id,
            "app_id": str(bot.get("app_id") or ""),
            "name": name,
            "image_url": image if isinstance(image, str) and image.startswith("https://") else "",
        }

    @staticmethod
    def _object(payload: dict[str, Any], key: str) -> dict[str, Any]:
        value = payload.get(key)
        if not isinstance(value, dict):
            raise HTTPException(400, "Slack did not return that bot.")
        return value

    @staticmethod
    def _option(member: object, auth: dict[str, Any], team_id: str) -> dict[str, str] | None:
        if (
            not isinstance(member, dict)
            or member.get("is_bot") is not True
            or member.get("deleted")
        ):
            return None
        if member.get("team_id") != team_id or member.get("id") in {
            auth.get("user_id"),
            "USLACKBOT",
        }:
            return None
        profile = member.get("profile")
        if not isinstance(profile, dict):
            return None
        bot_id, user_id = profile.get("bot_id"), member.get("id")
        if not isinstance(bot_id, str) or not bot_id or bot_id == auth.get("bot_id"):
            return None
        if not isinstance(user_id, str) or not user_id:
            return None
        image = profile.get("image_48") or ""
        return {
            "team_id": team_id,
            "bot_id": bot_id,
            "user_id": user_id,
            "name": str(
                profile.get("display_name")
                or profile.get("real_name")
                or member.get("name")
                or user_id
            ),
            "image_url": image if isinstance(image, str) and image.startswith("https://") else "",
        }


def _record(row: AllowedSlackBot) -> dict[str, object]:
    return {
        "team_id": row.team_id,
        "bot_id": row.bot_id,
        "user_id": row.slack_user_id,
        "app_id": row.app_id,
        "name": row.name,
        "image_url": row.image_url,
        "created_by": str(row.created_by or ""),
        "created_at": row.created_at,
    }


@router.get("/allowed-bots")
async def list_allowed_bots(
    db: AsyncSession = Depends(get_db), auth: AuthContext = Depends(get_auth)
) -> list[dict[str, object]]:
    require_admin(auth)
    rows = (
        await db.execute(
            select(AllowedSlackBot)
            .where(AllowedSlackBot.org_id == auth.org_id)
            .order_by(AllowedSlackBot.created_at)
        )
    ).scalars()
    return [_record(row) for row in rows]


@router.get("/allowed-bots/directory")
async def allowed_bot_directory(
    db: AsyncSession = Depends(get_db), auth: AuthContext = Depends(get_auth)
) -> list[dict[str, str]]:
    rows = (
        await db.execute(
            select(AllowedSlackBot)
            .where(AllowedSlackBot.org_id == auth.org_id)
            .order_by(AllowedSlackBot.name)
        )
    ).scalars()
    return [
        {"key": f"{row.team_id}:{row.bot_id}", "name": row.name, "image_url": row.image_url}
        for row in rows
    ]


@router.get("/bots")
async def list_workspace_bots(auth: AuthContext = Depends(get_auth)) -> list[dict[str, str]]:
    require_admin(auth)
    return await SlackBotDirectory(get_settings().slack_bot_token).options()


@router.post("/allowed-bots")
async def allow_bot(
    body: AllowSlackBotBody,
    db: AsyncSession = Depends(get_db),
    auth: AuthContext = Depends(get_auth),
) -> dict[str, object]:
    require_admin(auth)
    verified = await SlackBotDirectory(get_settings().slack_bot_token).verify(body.bot_id)
    existing = (
        await db.execute(
            select(AllowedSlackBot).where(
                AllowedSlackBot.org_id == auth.org_id,
                AllowedSlackBot.team_id == verified["team_id"],
                AllowedSlackBot.bot_id == verified["bot_id"],
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        raise HTTPException(409, "This bot is already allowed.")
    row = AllowedSlackBot(
        org_id=auth.org_id,
        team_id=verified["team_id"],
        bot_id=verified["bot_id"],
        slack_user_id=verified["user_id"],
        app_id=verified["app_id"],
        name=verified["name"],
        image_url=verified["image_url"],
        created_by=auth.user_id,
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )
    db.add(row)
    await db.flush()
    return _record(row)


@router.delete("/allowed-bots/{team_id}/{bot_id}")
async def remove_bot(
    team_id: str,
    bot_id: str,
    db: AsyncSession = Depends(get_db),
    auth: AuthContext = Depends(get_auth),
) -> dict[str, bool]:
    require_admin(auth)
    row = (
        await db.execute(
            select(AllowedSlackBot).where(
                AllowedSlackBot.org_id == auth.org_id,
                AllowedSlackBot.team_id == team_id,
                AllowedSlackBot.bot_id == bot_id,
            )
        )
    ).scalar_one_or_none()
    if row is not None:
        await db.delete(row)
    return {"ok": True}
