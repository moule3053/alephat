"""GitHub OAuth routes for the platform dashboard."""

import hmac
import os
from typing import Literal
from urllib.parse import urlencode

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import RedirectResponse, Response

from agent.dashboard.oauth import (
    COOKIE_NAME,
    SESSION_TTL_SECONDS,
    STATE_COOKIE_NAME,
    STATE_TTL_SECONDS,
    decode_state,
    enforce_github_login_gate,
    exchange_code,
    fetch_github_user,
    hash_state_nonce,
    issue_session,
    issue_state,
    new_state_nonce,
    sanitize_redirect_to,
)

router = APIRouter(prefix="/dashboard/api", tags=["dashboard-auth"])


def _dashboard_url(name: str) -> str:
    value = os.environ.get(name, "").rstrip("/")
    if not value:
        raise HTTPException(500, f"{name} not configured")
    return value


def _cookie_security() -> tuple[bool, Literal["none", "lax"]]:
    if os.environ.get("DASHBOARD_API_BASE_URL", "").startswith("https://"):
        return True, "none"
    return False, "lax"


def _set_session_cookie(response: Response, token: str) -> None:
    secure, samesite = _cookie_security()
    response.set_cookie(
        key=COOKIE_NAME,
        value=token,
        max_age=SESSION_TTL_SECONDS,
        httponly=True,
        secure=secure,
        samesite=samesite,
        path="/",
    )


def _set_state_cookie(response: Response, nonce: str) -> None:
    secure, _ = _cookie_security()
    response.set_cookie(
        key=STATE_COOKIE_NAME,
        value=nonce,
        max_age=STATE_TTL_SECONDS,
        httponly=True,
        secure=secure,
        samesite="lax",
        path="/dashboard/api/auth",
    )


def _clear_state_cookie(response: Response) -> None:
    secure, _ = _cookie_security()
    response.delete_cookie(
        STATE_COOKIE_NAME,
        path="/dashboard/api/auth",
        secure=secure,
        samesite="lax",
    )


@router.get("/auth/login")
async def auth_login(redirect_to: str | None = None) -> RedirectResponse:
    client_id = os.environ.get("GITHUB_APP_CLIENT_ID", "")
    if not client_id:
        raise HTTPException(500, "GITHUB_APP_CLIENT_ID not configured")

    safe_redirect = sanitize_redirect_to(redirect_to) or _dashboard_url("DASHBOARD_BASE_URL")
    nonce = new_state_nonce()
    state = issue_state(redirect_to=safe_redirect, nonce_hash=hash_state_nonce(nonce))
    redirect_uri = f"{_dashboard_url('DASHBOARD_API_BASE_URL')}/dashboard/api/auth/callback"
    query = urlencode(
        {
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "state": state,
        }
    )
    response = RedirectResponse(
        f"https://github.com/login/oauth/authorize?{query}", status_code=302
    )
    _set_state_cookie(response, nonce)
    return response


@router.get("/auth/callback")
async def auth_callback(request: Request, code: str, state: str) -> RedirectResponse:
    state_payload = decode_state(state)
    state_nonce_hash = state_payload.get("nonce_hash")
    cookie_nonce = request.cookies.get(STATE_COOKIE_NAME)
    if (
        not isinstance(state_nonce_hash, str)
        or not cookie_nonce
        or not hmac.compare_digest(hash_state_nonce(cookie_nonce), state_nonce_hash)
    ):
        raise HTTPException(400, "oauth state mismatch — please retry login")

    token_data = await exchange_code(code)
    access_token = token_data.get("access_token")
    if not isinstance(access_token, str) or not access_token:
        raise HTTPException(400, "oauth exchange missing access_token")
    user, email = await fetch_github_user(access_token)
    login = user.get("login")
    if not isinstance(login, str) or not login:
        raise HTTPException(400, "could not resolve GitHub login")
    await enforce_github_login_gate(login)

    redirect_to = sanitize_redirect_to(state_payload.get("redirect_to")) or _dashboard_url(
        "DASHBOARD_BASE_URL"
    )
    session_token = issue_session(
        login=login,
        email=email,
        avatar_url=user.get("avatar_url"),
    )
    response = RedirectResponse(redirect_to, status_code=302)
    _set_session_cookie(response, session_token)
    _clear_state_cookie(response)
    return response


@router.post("/auth/logout")
async def auth_logout() -> Response:
    response = Response(status_code=204)
    secure, samesite = _cookie_security()
    response.delete_cookie(COOKIE_NAME, path="/", secure=secure, samesite=samesite)
    return response
