import time
import uuid
from unittest.mock import AsyncMock

import httpx2
import pytest

from alephat_platform.common.config import clear_settings_cache
from alephat_platform.common.models import OrgMembership, Task, User
from alephat_platform.common.slack import SlackAction, SlackActionSigner, SlackClient
from alephat_platform.webhook.slack_actions import SlackActionHandler


@pytest.mark.asyncio
async def test_slack_buttons_reject_tampering_and_expiry():
    signer = SlackActionSigner("test-secret")
    action = SlackAction(
        task_id=uuid.uuid4(),
        run_id=uuid.uuid4(),
        channel="C1",
        thread_ts="1.2",
        action="approve",
        label="Approve",
        expires_at=int(time.time()) + 60,
    )
    token = await signer.sign(action)
    assert await signer.verify(token) == action
    with pytest.raises(ValueError, match="signature"):
        await signer.verify(token + "0")
    expired = await signer.sign(action.model_copy(update={"expires_at": 1}))
    with pytest.raises(ValueError, match="expired"):
        await signer.verify(expired)


@pytest.mark.asyncio
async def test_slack_actions_require_linked_member_and_task_ownership(monkeypatch):
    org_id, creator, other_user = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    monkeypatch.setenv("SLACK_USER_MAP", f'{{"T1:U1":"{creator}","T1:U2":"{other_user}"}}')
    clear_settings_cache()
    try:
        handler = SlackActionHandler(str(org_id))
        task = Task(task_id=uuid.uuid4(), org_id=org_id, created_by=creator)
        session = AsyncMock()
        session.get.side_effect = [
            User(user_id=creator),
            OrgMembership(org_id=org_id, user_id=creator, role="member"),
        ]
        assert (await handler.authorize(session, "T1", "U1", task)).user_id == creator
        with pytest.raises(ValueError, match="not linked"):
            await handler.authorize(session, "T1", "U3", task)
        session.get.side_effect = [
            User(user_id=other_user),
            OrgMembership(org_id=org_id, user_id=other_user, role="member"),
        ]
        with pytest.raises(ValueError, match="task creator"):
            await handler.authorize(session, "T1", "U2", task)
        session.get.side_effect = [User(user_id=creator), None]
        with pytest.raises(ValueError, match="not a member"):
            await handler.authorize(session, "T1", "U1", task)
    finally:
        clear_settings_cache()


@pytest.mark.asyncio
async def test_slack_http_success_with_api_failure_is_not_reported_as_delivery(monkeypatch):
    from alephat_platform.common import slack

    transport = httpx2.MockTransport(
        lambda request: httpx2.Response(200, json={"ok": False, "error": "not_in_channel"})
    )
    monkeypatch.setattr(slack, "pooled_transport", lambda: transport)
    with pytest.raises(RuntimeError, match="not_in_channel"):
        await SlackClient("test-token").react("C1", "1.2", "eyes")


@pytest.mark.asyncio
async def test_private_upload_uses_slack_form_handshake_without_forwarding_the_bot_token(
    monkeypatch,
):
    from alephat_platform.common import slack

    class UploadEndpoint:
        def __init__(self):
            self.content = b""

        async def __call__(self, request):
            if request.url.path.endswith("files.getUploadURLExternal"):
                if "application/x-www-form-urlencoded" not in request.headers.get(
                    "content-type", ""
                ):
                    return httpx2.Response(200, json={"ok": False, "error": "invalid_arguments"})
                return httpx2.Response(
                    200,
                    json={
                        "ok": True,
                        "file_id": "F1",
                        "upload_url": "https://files.slack.com/upload",
                    },
                )
            if request.url.path == "/upload":
                assert "authorization" not in request.headers
                self.content = request.content
            return httpx2.Response(200, json={"ok": True})

    endpoint = UploadEndpoint()
    monkeypatch.setattr(slack, "pooled_transport", lambda: httpx2.MockTransport(endpoint))
    assert (
        await SlackClient("test-token").upload(
            "C1", "1.2", filename="report.png", content=b"image", title="Report"
        )
        == "F1"
    )
    assert endpoint.content == b"image"
