"""API FastAPI application."""

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from alephat_platform.api.routes_dashboard import router as dashboard_router
from alephat_platform.api.routes_mcp import router as mcp_router
from alephat_platform.api.routes_meta import router as meta_router
from alephat_platform.api.routes_oauth import router as oauth_router
from alephat_platform.api.routes_slack_bots import router as slack_bots_router
from alephat_platform.api.routes_tasks import router as tasks_router
from alephat_platform.common.config import get_settings
from alephat_platform.common.db import dispose_engine
from alephat_platform.common.errors import PlatformError
from alephat_platform.common.temporal import TemporalPlatformClient

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    stop = asyncio.Event()
    temporal = TemporalPlatformClient(settings=settings)
    app.state.temporal = temporal
    supervisor_task = asyncio.create_task(temporal.supervisor_loop(stop))

    yield

    stop.set()
    supervisor_task.cancel()
    try:
        await supervisor_task
    except asyncio.CancelledError:
        pass
    await dispose_engine()


def create_app() -> FastAPI:
    app = FastAPI(title="Alephat API", version="0.1.0", lifespan=lifespan)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=list(get_settings().cors_origins),
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.exception_handler(PlatformError)
    async def platform_error_handler(_request: Request, exc: PlatformError) -> JSONResponse:
        return JSONResponse(status_code=exc.status, content=exc.to_dict())

    app.include_router(meta_router)
    app.include_router(tasks_router)
    app.include_router(mcp_router)
    app.include_router(oauth_router)
    app.include_router(dashboard_router)
    app.include_router(slack_bots_router)
    return app


app = create_app()
