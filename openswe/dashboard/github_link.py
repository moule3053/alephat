"""Linking a GitHub account to a LangSmith sign-in, which unlocks the coding features."""

import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Self

from fastapi import HTTPException
from fastapi.responses import RedirectResponse
from pydantic import ValidationError

from openswe.dashboard.oauth import (
    GithubUser,
    enforce_github_login_gate,
    exchange_code,
    fetch_github_user,
    issue_session,
)
from openswe.dashboard.principal import DashboardPrincipal
from openswe.dashboard.profiles import upsert_access_token_from_github_response
from openswe.encryption import decrypt_token, encrypt_token
from openswe.users import User


@dataclass(frozen=True)
class GitHubLink:
    """A GitHub account a person proved through the GitHub App's OAuth, with its tokens."""

    account: GithubUser
    email: str
    tokens: Mapping[str, object]

    @staticmethod
    def authorize(
        *,
        authorize_url: str = "https://github.com/login/oauth/authorize",
        redirect_to: str | None = None,
        desktop_handoff: str | None = None,
        desktop_port: int | None = None,
    ) -> RedirectResponse:
        from urllib.parse import urlencode

        from fastapi.responses import RedirectResponse

        from openswe.config import ENV
        from openswe.dashboard.oauth import (
            GITHUB_CALLBACK_PATH,
            frontend_base_url,
            hash_state_nonce,
            issue_state,
            new_state_nonce,
            sanitize_redirect_to,
            set_state_cookie,
            valid_handoff_challenge,
        )
        from openswe.utils.dashboard_links import dashboard_api_base_url

        client_id = ENV.GITHUB_APP_CLIENT_ID.get()
        if not client_id:
            raise HTTPException(500, "GITHUB_APP_CLIENT_ID not configured")
        nonce = new_state_nonce()
        state = issue_state(
            redirect_to=sanitize_redirect_to(redirect_to) or frontend_base_url(),
            nonce_hash=hash_state_nonce(nonce),
            handoff_challenge=valid_handoff_challenge(desktop_handoff),
            handoff_port=desktop_port,
        )
        query = urlencode(
            {
                "client_id": client_id,
                "redirect_uri": f"{dashboard_api_base_url()}{GITHUB_CALLBACK_PATH}",
                "state": state,
            }
        )
        response = RedirectResponse(f"{authorize_url}?{query}", status_code=302)
        set_state_cookie(response, nonce)
        return response

    @classmethod
    async def from_code(cls, code: str) -> Self:
        tokens = await exchange_code(code)
        access_token = tokens.get("access_token")
        if not isinstance(access_token, str):
            raise HTTPException(400, "oauth exchange missing access_token")
        account, email = await fetch_github_user(access_token)
        if not account.login:
            raise HTTPException(400, "could not resolve GitHub login")
        await enforce_github_login_gate(account.login)
        return cls(account, email or "", tokens)

    def handoff_claims(self) -> dict[str, object]:
        """Claims for a desktop handoff code, whose URL a browser records: tokens stay encrypted."""
        return {
            "github": self.account.model_dump(),
            "email": self.email,
            "encrypted_tokens": encrypt_token(json.dumps(dict(self.tokens))),
        }

    @classmethod
    def from_handoff(cls, claims: Mapping[str, object]) -> Self:
        email = claims.get("email")
        encrypted = claims.get("encrypted_tokens")
        tokens = decrypt_token(encrypted) if isinstance(encrypted, str) else None
        try:
            account = GithubUser.model_validate(claims.get("github"))
            parsed: object = json.loads(tokens) if tokens else None
        except (ValidationError, ValueError) as exc:
            raise HTTPException(400, "malformed handoff code") from exc
        if not isinstance(parsed, dict) or not isinstance(email, str):
            raise HTTPException(400, "malformed handoff code")
        return cls(account, email, parsed)

    async def join(self, principal: DashboardPrincipal) -> User:
        """Join the account to the signed-in LangSmith person and keep its tokens for them."""
        if principal.provider != "langsmith":
            raise HTTPException(403, "Sign in with LangSmith to link a GitHub account")
        user = await principal.user()
        if user is None:
            raise HTTPException(401, "Alephat account not found — sign in again")
        joined = await user.join_github(
            str(self.account.id), login=self.account.login, email=self.email
        )
        await upsert_access_token_from_github_response(
            self.account.login, self.email, dict(self.tokens)
        )
        return joined

    async def sign_in(self) -> str:
        """Sign in directly with this GitHub account and issue a GitHub session."""
        from openswe.dashboard.admin import configured_admins, is_admin

        signed_in = await User.sign_in(
            "github",
            str(self.account.id),
            login=self.account.login,
            email=self.email,
            display_name=self.account.name or "",
            avatar_url=self.account.avatar_url or "",
            admin=is_admin(self.email or None, login=self.account.login)
            if configured_admins()
            else None,
        )
        await upsert_access_token_from_github_response(
            self.account.login, self.email, dict(self.tokens)
        )
        return issue_session(
            login=self.account.login,
            email=self.email or None,
            avatar_url=self.account.avatar_url,
            user_id=str(signed_in.id),
            provider="github",
        )

    @staticmethod
    def session(principal: DashboardPrincipal, user: User, email: str | None) -> str:
        """The LangSmith session for ``user``, acting as their GitHub login once linked."""
        return issue_session(
            login=user.session_login,
            email=email,
            avatar_url=user.avatar_url or None,
            user_id=str(user.id),
            provider="langsmith",
            langsmith_subject=principal.langsmith_subject,
        )
