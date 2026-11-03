from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from urllib.parse import parse_qs, urlparse

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.requests import Request

from alephat_platform.api import routes_oauth
from alephat_platform.api.deps import get_auth
from alephat_platform.common.config import clear_settings_cache
from openswe.dashboard.oauth import COOKIE_NAME, decode_session, issue_session


def _configure_oauth(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GITHUB_APP_CLIENT_ID", "client-id")
    monkeypatch.setenv("DASHBOARD_JWT_SECRET", "dashboard-secret-at-least-32-bytes")
    monkeypatch.setenv("DASHBOARD_BASE_URL", "http://127.0.0.1:18080")
    monkeypatch.setenv("DASHBOARD_API_BASE_URL", "http://127.0.0.1:18080")


def test_platform_oauth_redirect_and_callback(monkeypatch: pytest.MonkeyPatch) -> None:
    _configure_oauth(monkeypatch)

    async def fake_exchange_code(code: str) -> dict[str, str]:
        assert code == "oauth-code"
        return {"access_token": "github-token"}

    async def fake_fetch_github_user(token: str) -> tuple[dict[str, str], str]:
        assert token == "github-token"
        return {
            "login": "alice",
            "avatar_url": "https://avatars.example/alice.png",
        }, "alice@example.com"

    async def fake_enforce_github_login_gate(login: str) -> None:
        assert login == "alice"

    monkeypatch.setattr(routes_oauth, "exchange_code", fake_exchange_code)
    monkeypatch.setattr(routes_oauth, "fetch_github_user", fake_fetch_github_user)
    monkeypatch.setattr(routes_oauth, "enforce_github_login_gate", fake_enforce_github_login_gate)

    app = FastAPI()
    app.include_router(routes_oauth.router)
    with TestClient(app) as client:
        login_response = client.get(
            "/dashboard/api/auth/login",
            params={"redirect_to": "/agents"},
            follow_redirects=False,
        )
        assert login_response.status_code == 302
        authorization_url = urlparse(login_response.headers["location"])
        query = parse_qs(authorization_url.query)
        assert authorization_url.netloc == "github.com"
        assert query["client_id"] == ["client-id"]
        assert query["redirect_uri"] == ["http://127.0.0.1:18080/dashboard/api/auth/callback"]

        callback_response = client.get(
            "/dashboard/api/auth/callback",
            params={"code": "oauth-code", "state": query["state"][0]},
            follow_redirects=False,
        )

        assert callback_response.status_code == 302
        assert callback_response.headers["location"] == "http://127.0.0.1:18080/agents"
        session = decode_session(client.cookies[COOKIE_NAME])
        assert session["sub"] == "alice"
        assert session["email"] == "alice@example.com"


@pytest.mark.asyncio
async def test_platform_auth_accepts_github_session_cookie(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _configure_oauth(monkeypatch)
    monkeypatch.setenv("API_AUTH_DISABLED", "false")
    clear_settings_cache()
    try:
        session_token = issue_session(
            login="alice",
            email="alice@example.com",
            avatar_url="https://avatars.example/alice.png",
        )
        request = Request(
            {
                "type": "http",
                "method": "GET",
                "path": "/dashboard/api/me",
                "query_string": b"",
                "headers": [(b"cookie", f"{COOKIE_NAME}={session_token}".encode())],
                "scheme": "http",
                "server": ("testserver", 80),
                "client": ("testclient", 123),
            }
        )
        membership_result = MagicMock()
        membership_result.scalar_one_or_none.return_value = SimpleNamespace(role="admin")
        db = AsyncMock()
        db.execute.return_value = membership_result

        auth = await get_auth(request, db, None, None, None)

        assert auth.github_login == "alice"
        assert auth.email == "alice@example.com"
        assert auth.role == "admin"
    finally:
        clear_settings_cache()
