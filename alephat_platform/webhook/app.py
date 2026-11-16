"""Webhook FastAPI application backed by Temporal."""

import asyncio
import json
import logging
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from urllib.parse import parse_qs

from fastapi import FastAPI, Header, Request
from fastapi.responses import JSONResponse
from pydantic import JsonValue, TypeAdapter
from sqlalchemy import text

from alephat_platform.common.config import get_settings
from alephat_platform.common.db import dispose_engine, session_scope
from alephat_platform.common.errors import PlatformError, UnauthorizedError
from alephat_platform.common.temporal import TemporalPlatformClient
from alephat_platform.webhook.handlers import IngressCommandService
from alephat_platform.webhook.normalize import (
    normalize_github,
    normalize_slack,
    verify_github_signature,
    verify_slack_signature,
)
from alephat_platform.webhook.slack_actions import SlackActionHandler

logger = logging.getLogger(__name__)


class SlackSocketModeClient:
    """Outbound Slack Socket Mode WebSocket listener (``apps.connections.open``)."""

    def __init__(
        self,
        *,
        app_token: str,
        org_id: str,
        temporal: TemporalPlatformClient,
        stop_event: asyncio.Event,
    ) -> None:
        self.app_token = app_token
        self.org_id = org_id
        self.temporal = temporal
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
        if frame.get("type") == "interactive":
            await SlackActionHandler(self.org_id, temporal=self.temporal).handle(
                TypeAdapter(dict[str, JsonValue]).validate_python(payload)
            )
            return
        event_obj = payload.get("event")
        channel = event_obj.get("channel") if isinstance(event_obj, dict) else None
        msg_ts = event_obj.get("ts") if isinstance(event_obj, dict) else None
        event_type = event_obj.get("type") if isinstance(event_obj, dict) else None
        client_msg_id = event_obj.get("client_msg_id") if isinstance(event_obj, dict) else None
        delivery_id = (
            f"{channel}:{msg_ts}"
            if isinstance(channel, str) and isinstance(msg_ts, str)
            else str(
                payload.get("event_id") or client_msg_id or frame.get("envelope_id") or uuid.uuid4()
            )
        )
        raw_body = json.dumps(payload, separators=(",", ":")).encode()
        async with session_scope() as session:
            ingress = IngressCommandService(session, self.temporal)
            is_new = await ingress.accept_delivery(
                provider="slack", delivery_id=delivery_id, raw_body=raw_body
            )
            if not is_new:
                logger.info(
                    "Duplicate Slack event skipped",
                    extra={"delivery_id": delivery_id, "slack_event_type": event_type},
                )
                return
            command = normalize_slack(
                payload,
                org_id=self.org_id,
                delivery_id=delivery_id,
            )
            result = await ingress.apply(command)
            logger.info(
                "Processed Slack Socket Mode event",
                extra={
                    "delivery_id": delivery_id,
                    "slack_event_type": event_type,
                    "result": result,
                    "event_details": json.dumps(
                        {"event_type": event_type, "delivery_id": delivery_id, "result": result}
                    ),
                },
            )

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
                        payload = frame.get("payload")
                        event = payload.get("event") if isinstance(payload, dict) else None
                        logger.info(
                            "Received Slack Socket Mode frame",
                            extra={
                                "event_details": json.dumps(
                                    {
                                        "frame_type": frame.get("type"),
                                        "event_type": (
                                            event.get("type") if isinstance(event, dict) else None
                                        ),
                                    }
                                )
                            },
                        )
                        envelope_id = frame.get("envelope_id")
                        if (
                            isinstance(envelope_id, str)
                            and envelope_id
                            and frame.get("type") != "events_api"
                        ):
                            await ws.send(json.dumps({"envelope_id": envelope_id}))
                        if frame.get("type") == "disconnect":
                            logger.info("Slack Socket Mode requested reconnect")
                            break
                        if frame.get("type") in {"events_api", "interactive"}:
                            await self._handle_envelope(frame)
                            if (
                                frame.get("type") == "events_api"
                                and isinstance(envelope_id, str)
                                and envelope_id
                            ):
                                await ws.send(json.dumps({"envelope_id": envelope_id}))
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.warning("Slack Socket Mode connection error; retrying in 5s", exc_info=True)
                await asyncio.sleep(5)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    handler = logging.StreamHandler()
    handler.setFormatter(
        logging.Formatter(
            "%(asctime)s %(levelname)s [webhook] %(message)s %(event_details)s",
            defaults={"event_details": ""},
        )
    )
    logging.basicConfig(level=logging.INFO, handlers=[handler])
    settings = get_settings()
    stop = asyncio.Event()
    temporal = TemporalPlatformClient(settings=settings)
    app.state.temporal = temporal
    supervisor_task = asyncio.create_task(temporal.supervisor_loop(stop))

    socket_task: asyncio.Task[None] | None = None
    app_token = (settings.slack_app_token or "").strip()
    if app_token.startswith("xapp-") and app_token != "xapp-your-app-level-token":
        client = SlackSocketModeClient(
            app_token=app_token,
            org_id=settings.default_org_id,
            temporal=temporal,
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
    supervisor_task.cancel()
    try:
        await supervisor_task
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
            await session.execute(text("SELECT 1"))
        temporal = TemporalPlatformClient.from_request(request)
        if not await temporal.check_health():
            raise PlatformError(
                "Not ready",
                status=503,
                detail="Temporal service is not reachable",
                error_code="temporal_unavailable",
            )
        return {"status": "ready"}

    @app.post("/hooks/github")
    async def github_hook(
        request: Request,
        x_github_event: str | None = Header(default=None, alias="X-GitHub-Event"),
        x_github_delivery: str | None = Header(default=None, alias="X-GitHub-Delivery"),
        x_hub_signature_256: str | None = Header(default=None, alias="X-Hub-Signature-256"),
    ) -> dict[str, object]:
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
        temporal = TemporalPlatformClient.from_request(request)

        async with session_scope() as session:
            ingress = IngressCommandService(session, temporal)
            is_new = await ingress.accept_delivery(
                provider="github", delivery_id=delivery_id, raw_body=body
            )
            if not is_new:
                return {"duplicate": True, "accepted": True}

            command = normalize_github(
                event,
                payload,
                delivery_id=delivery_id,
                org_id=settings.default_org_id,
            )
            return await ingress.apply(command)

    @app.post("/hooks/slack")
    @app.post("/webhooks/slack")
    async def slack_hook(
        request: Request,
        x_slack_signature: str | None = Header(default=None, alias="X-Slack-Signature"),
        x_slack_request_timestamp: str | None = Header(
            default=None, alias="X-Slack-Request-Timestamp"
        ),
    ) -> dict[str, object]:
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

        if request.headers.get("content-type", "").startswith("application/x-www-form-urlencoded"):
            payload = json.loads(parse_qs(body.decode()).get("payload", ["{}"])[0])
        else:
            payload = json.loads(body.decode() or "{}")

        temporal = TemporalPlatformClient.from_request(request)
        if payload.get("type") == "block_actions":
            if not settings.slack_signing_secret:
                raise UnauthorizedError("Interactive Slack requests require signature verification")
            handled = await SlackActionHandler(settings.default_org_id, temporal=temporal).handle(
                TypeAdapter(dict[str, JsonValue]).validate_python(payload)
            )
            return dict(handled)
        if payload.get("type") == "url_verification":
            return {"challenge": payload.get("challenge")}

        raw_event = payload.get("event")
        event_dict = raw_event if isinstance(raw_event, dict) else {}
        delivery_id = (
            (payload.get("event_id") if isinstance(payload.get("event_id"), str) else None)
            or event_dict.get("client_msg_id")
            or str(uuid.uuid4())
        )

        async with session_scope() as session:
            ingress = IngressCommandService(session, temporal)
            is_new = await ingress.accept_delivery(
                provider="slack", delivery_id=str(delivery_id), raw_body=body
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
            return await ingress.apply(command)

    return app


app = create_app()
