"""FastAPI dependencies."""

import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from secrets import compare_digest

import jwt
from fastapi import Depends, Header, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from agent.dashboard.oauth import COOKIE_NAME, decode_session
from alephat_platform.common.config import get_settings
from alephat_platform.common.db import get_session_factory
from alephat_platform.common.errors import ForbiddenError, PlatformError, UnauthorizedError
from alephat_platform.common.models import OrgMembership, Task


@dataclass
class AuthContext:
    org_id: uuid.UUID
    user_id: uuid.UUID
    role: str = "member"
    github_login: str | None = None
    email: str | None = None
    avatar_url: str | None = None


async def get_db() -> AsyncIterator[AsyncSession]:
    factory = get_session_factory()
    async with factory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


async def get_auth(
    request: Request,
    db: AsyncSession = Depends(get_db),
    authorization: str | None = Header(default=None, alias="Authorization"),
    x_org_id: str | None = Header(default=None, alias="X-Org-Id"),
    x_user_id: str | None = Header(default=None, alias="X-User-Id"),
) -> AuthContext:
    settings = get_settings()
    org = x_org_id or settings.default_org_id
    user = x_user_id or settings.default_user_id
    github_login = email = avatar_url = None

    if not settings.api_auth_disabled:
        if authorization and authorization.startswith("Bearer "):
            token = authorization.removeprefix("Bearer ").strip()
            if settings.platform_api_token and compare_digest(token, settings.platform_api_token):
                pass
            elif settings.platform_jwt_secret:
                try:
                    claims = jwt.decode(token, settings.platform_jwt_secret, algorithms=["HS256"])
                    org = str(claims.get("org_id") or "")
                    user = str(claims.get("user_id") or claims.get("sub") or "")
                except jwt.PyJWTError as exc:
                    raise UnauthorizedError("Invalid bearer token") from exc
            else:
                raise PlatformError(
                    "Authentication unavailable",
                    status=503,
                    detail="Configure PLATFORM_API_TOKEN or PLATFORM_JWT_SECRET",
                    error_code="auth_not_configured",
                )
        elif session_token := request.cookies.get(COOKIE_NAME):
            claims = decode_session(session_token)
            github_login = str(claims.get("sub") or "") or None
            email = str(claims.get("email") or "") or None
            avatar_url = str(claims.get("avatar_url") or "") or None
        else:
            raise UnauthorizedError("Bearer or GitHub session authentication is required")

    try:
        org_id = uuid.UUID(org)
        user_id = uuid.UUID(user)
    except ValueError as exc:
        raise UnauthorizedError("Invalid identity") from exc

    membership = await db.execute(
        select(OrgMembership).where(
            OrgMembership.org_id == org_id,
            OrgMembership.user_id == user_id,
        )
    )
    row = membership.scalar_one_or_none()
    if row is None and not settings.api_auth_disabled:
        raise ForbiddenError("User is not a member of this organization")
    return AuthContext(
        org_id=org_id,
        user_id=user_id,
        role=row.role if row else "admin",
        github_login=github_login,
        email=email,
        avatar_url=avatar_url,
    )


def require_admin(auth: AuthContext) -> None:
    if auth.role != "admin":
        raise ForbiddenError("Organization administrator access is required")


def require_task_actor(task: Task, auth: AuthContext) -> None:
    if auth.role != "admin" and task.created_by != auth.user_id:
        raise ForbiddenError("Only the task creator or an organization administrator may modify it")
