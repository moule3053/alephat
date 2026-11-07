"""Slack transport and the stateless json-render boundary."""

import base64
import hashlib
import hmac
import json
import logging
import time
import uuid
from urllib.parse import urlparse

import httpx2
from pydantic import BaseModel, Field, JsonValue, TypeAdapter

from openswe.utils.http_pool import pooled_transport

logger = logging.getLogger(__name__)


class SlackMessage(BaseModel):
    text: str
    blocks: list[dict[str, JsonValue]] = Field(default_factory=list)


class SlackRenderResult(BaseModel):
    messages: list[SlackMessage]


class SlackClient:
    def __init__(self, token: str) -> None:
        self.token = token

    async def _call(
        self, method: str, body: dict[str, JsonValue], *, form: bool = False
    ) -> dict[str, JsonValue]:
        async with httpx2.AsyncClient(transport=pooled_transport(), timeout=15) as client:
            response = await client.post(
                f"https://slack.com/api/{method}",
                headers={"Authorization": f"Bearer {self.token}"},
                json=None if form else body,
                data={key: str(value) for key, value in body.items()} if form else None,
            )
            response.raise_for_status()
            result = TypeAdapter(dict[str, JsonValue]).validate_json(response.content)
        if result.get("ok") is not True:
            raise RuntimeError(f"Slack {method} failed: {result.get('error', 'unknown_error')}")
        return result

    async def post(self, channel: str, thread_ts: str, message: SlackMessage) -> str:
        result = await self._call(
            "chat.postMessage",
            {
                "channel": channel,
                "thread_ts": thread_ts,
                **message.model_dump(mode="json"),
                "unfurl_links": False,
                "unfurl_media": False,
            },
        )
        timestamp = result.get("ts")
        if not isinstance(timestamp, str):
            raise RuntimeError("Slack did not return a message timestamp")
        return timestamp

    async def update(self, channel: str, timestamp: str, message: SlackMessage) -> None:
        await self._call(
            "chat.update",
            {"channel": channel, "ts": timestamp, **message.model_dump(mode="json")},
        )

    async def react(self, channel: str, timestamp: str, name: str) -> None:
        try:
            await self._call(
                "reactions.add", {"channel": channel, "timestamp": timestamp, "name": name}
            )
        except RuntimeError as exc:
            if "already_reacted" not in str(exc):
                raise
            logger.debug("Slack reaction is already present")

    async def method(self, name: str, body: dict[str, JsonValue] | None = None) -> dict[str, JsonValue]:
        return await self._call(name, body or {}, form=True)

    async def ephemeral(self, channel: str, user: str, text: str) -> None:
        await self._call("chat.postEphemeral", {"channel": channel, "user": user, "text": text})

    async def upload(
        self, channel: str, thread_ts: str, *, filename: str, content: bytes, title: str
    ) -> str:
        result = await self._call(
            "files.getUploadURLExternal", {"filename": filename, "length": len(content)}, form=True
        )
        url, file_id = result.get("upload_url"), result.get("file_id")
        if not isinstance(url, str) or not isinstance(file_id, str):
            raise RuntimeError("Slack did not return an upload destination")
        parsed = urlparse(url)
        if parsed.scheme != "https" or not (parsed.hostname or "").endswith(".slack.com"):
            raise RuntimeError("Unexpected Slack upload destination")
        async with httpx2.AsyncClient(transport=pooled_transport(), timeout=60) as client:
            response = await client.post(
                url, content=content, headers={"Content-Type": "application/octet-stream"}
            )
            response.raise_for_status()
        await self._call(
            "files.completeUploadExternal",
            {
                "files": [{"id": file_id, "title": title}],
                "channel_id": channel,
                "thread_ts": thread_ts,
            },
        )
        return file_id


class SlackRendererClient:
    def __init__(self, url: str, token: str = "") -> None:
        self.url = url.rstrip("/")
        self.token = token

    async def catalog(self) -> str:
        async with httpx2.AsyncClient(transport=pooled_transport(), timeout=5) as client:
            response = await client.get(
                f"{self.url}/catalog",
                headers={"Authorization": f"Bearer {self.token}"} if self.token else {},
            )
            response.raise_for_status()
            result = TypeAdapter(dict[str, JsonValue]).validate_json(response.content)
        prompt = result.get("prompt")
        if not isinstance(prompt, str):
            raise ValueError("Renderer did not return a catalog prompt")
        return prompt

    async def render(
        self,
        *,
        markdown: str | None = None,
        spec: dict[str, JsonValue] | None = None,
        partial: bool = False,
    ) -> SlackRenderResult:
        body: dict[str, JsonValue] = {"partial": partial}
        if spec is not None:
            body["spec"] = spec
        else:
            body["markdown"] = markdown or "Task completed."
        async with httpx2.AsyncClient(transport=pooled_transport(), timeout=10) as client:
            response = await client.post(
                f"{self.url}/render",
                headers={"Authorization": f"Bearer {self.token}"} if self.token else {},
                json=body,
            )
            if response.status_code == 422:
                result = TypeAdapter(dict[str, JsonValue]).validate_json(response.content)
                raise ValueError(str(result.get("error", "Invalid card")))
            response.raise_for_status()
        return SlackRenderResult.model_validate_json(response.content)


class SlackAction(BaseModel):
    task_id: uuid.UUID
    run_id: uuid.UUID
    channel: str
    thread_ts: str
    action: str
    label: str
    value: str = ""
    expires_at: int


class SlackActionSigner:
    def __init__(self, secret: str) -> None:
        self.secret = secret.encode()

    async def sign(self, action: SlackAction) -> str:
        payload = base64.urlsafe_b64encode(action.model_dump_json().encode()).decode().rstrip("=")
        signature = hmac.new(self.secret, payload.encode(), hashlib.sha256).hexdigest()
        token = f"{payload}.{signature}"
        if len(token) > 2000:
            raise ValueError("Button value exceeds Slack's limit")
        return token

    async def verify(self, token: str) -> SlackAction:
        payload, separator, signature = token.partition(".")
        expected = hmac.new(self.secret, payload.encode(), hashlib.sha256).hexdigest()
        if not separator or not hmac.compare_digest(signature, expected):
            raise ValueError("Invalid button signature")
        try:
            action = SlackAction.model_validate_json(
                base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4))
            )
        except (ValueError, json.JSONDecodeError) as exc:
            raise ValueError("Invalid button data") from exc
        if action.expires_at < time.time():
            raise ValueError("This button has expired")
        return action
