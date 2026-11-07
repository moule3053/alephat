"""Webhook FastAPI application."""

import asyncio
import json
import logging
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Header, Request
from fastapi.responses import JSONResponse

from alephat_platform.common.config import get_settings
from alephat_platform.common.db import dispose_engine, get_session_factory, session_scope
from alephat_platform.common.errors import PlatformError, UnauthorizedError
from alephat_platform.common.messaging import NatsBus
from alephat_platform.common.outbox import outbox_supervisor_loop
from alephat_platform.webhook.handlers import accept_delivery, apply_ingress_command
from alephat_platform.webhook.normalize import (
    normalize_github,
    normalize_slack,
    verify_github_signature,
    verify_slack_signature,
)

logger = logging.getLogger(__name__)


class SlackSocketModeClient:
    """Outbound Slack Socket Mode WebSocket listener (`apps.connections.open`)."""

    def __init__(self, *, app_token: str, org_id: str, stop_event: asyncio.Event) -> None:
        self.app_token = app_token
        self.org_id = org_id
        self.stop_event = stop_event

    async def _open_ws_url(self) -> str:
        import httpx

        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.post(
                "https://slack.com/api/apps.connections.open",
                headers={"Authorization": f"Bearer {self.app_token}"},
            )
            resp.raise_for_status()
            data = resp.json()
        if not isinstance(data, dict) or not data.get("ok") or not isinstance(data.get("url"), str):
            raise RuntimeError(f"Slack apps.connections.open failed: {data}")
        return data["url"]

    async def _handle_envelope(self, frame: dict[str, object]) -> None:
        payload = frame.get("payload")
        if not isinstance(payload, dict):
            return
        event_obj = payload.get("event")
        client_msg_id = event_obj.get("client_msg_id") if isinstance(event_obj, dict) else None
        delivery_id = str(
            payload.get("event_id") or client_msg_id or frame.get("envelope_id") or uuid.uuid4()
        )
        raw_body = json.dumps(payload, separators=(",", ":")).encode()
        async with session_scope() as session:
            is_new = await accept_delivery(
                session, provider="slack", delivery_id=delivery_id, raw_body=raw_body
            )
            if not is_new:
                return
            command = normalize_slack(
                payload,
                org_id=self.org_id,
                delivery_id=delivery_id,
            )
            await apply_ingress_command(session, command)

    async def run(self) -> None:
        import websockets

        while not self.stop_event.is_set():
            try:
                ws_url = await self._open_ws_url()
                async with websockets.connect(ws_url, ping_interval=20, ping_timeout=20) as ws:
                    logger.info("Connected to Slack Socket Mode WebSocket")
                    async for raw in ws:
                        if self.stop_event.is_set():
                            break
                        frame = json.loads(raw if isinstance(raw, str) else raw.decode())
                        if not isinstance(frame, dict):
                            continue
                        envelope_id = frame.get("envelope_id")
                        if isinstance(envelope_id, str) and envelope_id:
                            await ws.send(json.dumps({"envelope_id": envelope_id}))
                        if frame.get("type") == "disconnect":
                            logger.info("Slack Socket Mode requested reconnect")
                            break
                        if frame.get("type") == "events_api":
                            await self._handle_envelope(frame)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.warning("Slack Socket Mode connection error; retrying in 5s", exc_info=True)
                await asyncio.sleep(5)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s [webhook] %(message)s",
    )
    settings = get_settings()
    stop = asyncio.Event()
    app.state.nats = None

    def _set_connection(bus: NatsBus | None) -> None:
        app.state.nats = bus

    publisher_task = asyncio.create_task(
        outbox_supervisor_loop(
            get_session_factory(),
            nats_url=settings.nats_url,
            interval=settings.outbox_poll_interval_seconds,
            stop_event=stop,
            on_connection=_set_connection,
        )
    )
    socket_task: asyncio.Task[None] | None = None
    app_token = (settings.slack_app_token or "").strip()
    if app_token.startswith("xapp-") and app_token != "xapp-your-app-level-token":
        client = SlackSocketModeClient(
            app_token=app_token,
            org_id=settings.default_org_id,
            stop_event=stop,
        )
        socket_task = asyncio.create_task(client.run())

    yield
    stop.set()
    if socket_task is not None:
        socket_task.cancel()
        try:
            await socket_task
        except asyncio.CancelledError:
            pass
    publisher_task.cancel()
    try:
        await publisher_task
    except asyncio.CancelledError:
        pass
    await dispose_engine()


def create_app() -> FastAPI:
    app = FastAPI(title="Alephat Webhooks", version="0.1.0", lifespan=lifespan)

    @app.exception_handler(PlatformError)
    async def _err(_req: Request, exc: PlatformError) -> JSONResponse:
        return JSONResponse(status_code=exc.status, content=exc.to_dict())

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/readyz")
    async def readyz(request: Request) -> dict[str, str]:
        async with session_scope() as session:
            await session.execute(__import__("sqlalchemy", fromlist=["text"]).text("SELECT 1"))
        bus = getattr(request.app.state, "nats", None)
        if bus is None or not bus.connected:
            raise PlatformError(
                "Not ready",
                status=503,
                detail="NATS publisher is not connected",
                error_code="nats_unavailable",
            )
        return {"status": "ready"}

    @app.post("/hooks/github")
    async def github_hook(
        request: Request,
        x_github_event: str | None = Header(default=None, alias="X-GitHub-Event"),
        x_github_delivery: str | None = Header(default=None, alias="X-GitHub-Delivery"),
        x_hub_signature_256: str | None = Header(default=None, alias="X-Hub-Signature-256"),
    ) -> dict:
        settings = get_settings()
        body = await request.body()
        if settings.webhook_signatures_required and not settings.github_webhook_secret:
            raise PlatformError(
                "Webhook verification unavailable",
                status=503,
                detail="GITHUB_WEBHOOK_SECRET is required",
                error_code="webhook_secret_not_configured",
            )
        if not verify_github_signature(settings.github_webhook_secret, body, x_hub_signature_256):
            raise UnauthorizedError("invalid github signature")

        delivery_id = x_github_delivery or str(uuid.uuid4())
        event = x_github_event or "unknown"
        payload = json.loads(body.decode() or "{}")

        async with session_scope() as session:
            is_new = await accept_delivery(
                session, provider="github", delivery_id=delivery_id, raw_body=body
            )
            if not is_new:
                return {"duplicate": True, "accepted": True}

            command = normalize_github(
                event,
                payload,
                delivery_id=delivery_id,
                org_id=settings.default_org_id,
            )
            result = await apply_ingress_command(session, command)
            return result

    @app.post("/hooks/slack")
    @app.post("/webhooks/slack")
    async def slack_hook(
        request: Request,
        x_slack_signature: str | None = Header(default=None, alias="X-Slack-Signature"),
        x_slack_request_timestamp: str | None = Header(
            default=None, alias="X-Slack-Request-Timestamp"
        ),
    ) -> dict:
        settings = get_settings()
        body = await request.body()
        if settings.webhook_signatures_required and not settings.slack_signing_secret:
            raise PlatformError(
                "Webhook verification unavailable",
                status=503,
                detail="SLACK_SIGNING_SECRET is required",
                error_code="webhook_secret_not_configured",
            )
        if not verify_slack_signature(
            settings.slack_signing_secret,
            body,
            x_slack_request_timestamp,
            x_slack_signature,
        ):
            raise UnauthorizedError("invalid slack signature")

        payload = json.loads(body.decode() or "{}")
        if payload.get("type") == "url_verification":
            return {"challenge": payload.get("challenge")}

        delivery_id = (
            (payload.get("event_id") if isinstance(payload.get("event_id"), str) else None)
            or payload.get("event", {}).get("client_msg_id")
            or str(uuid.uuid4())
        )

        async with session_scope() as session:
            is_new = await accept_delivery(
                session, provider="slack", delivery_id=str(delivery_id), raw_body=body
            )
            if not is_new:
                return {"duplicate": True, "accepted": True}

            command = normalize_slack(
                payload,
                org_id=settings.default_org_id,
                delivery_id=str(delivery_id),
            )
            if command.get("challenge"):
                return {"challenge": command["challenge"]}
            result = await apply_ingress_command(session, command)
            return result

    return app


app = create_app()
