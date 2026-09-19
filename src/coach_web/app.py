"""FastAPI application factory for Coach Web."""

import logging
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

import uvicorn
from fastapi import FastAPI, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from sse_starlette import EventSourceResponse, ServerSentEvent

from coach_web.agent import AgentEvent, CoachAgent, create_agent
from coach_web.config import Settings, get_settings

logger = logging.getLogger("coach_web")

STATIC_DIR = Path(__file__).parent / "static"
SERVICE_NAME = "coach-web"
PACKAGE_NAME = "coach-web"
FALLBACK_VERSION = "0.0.0"


def resolve_version() -> str:
    """Return the installed package version, falling back for bare source checkouts."""
    try:
        return version(PACKAGE_NAME)
    except PackageNotFoundError:
        return FALLBACK_VERSION


class HealthResponse(BaseModel):
    """Healthcheck payload returned by ``/health`` and ``/healthz``."""

    status: str
    service: str
    version: str
    uptime_seconds: float


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Manage application startup and shutdown."""
    settings: Settings = app.state.settings
    app.state.started_at = time.monotonic()
    logger.info(
        "Starting %s v%s on %s:%s",
        SERVICE_NAME,
        app.version,
        settings.APP_HOST,
        settings.APP_PORT,
    )
    yield
    logger.info("Stopping %s", SERVICE_NAME)


def create_app(settings: Settings | None = None) -> FastAPI:
    """Create and configure the Coach Web FastAPI application."""
    resolved = settings or get_settings()

    application = FastAPI(
        title="Coach Web",
        version=resolve_version(),
        lifespan=lifespan,
    )
    application.state.settings = resolved
    application.state.started_at = time.monotonic()
    application.state.agent_factory = create_agent

    application.add_middleware(
        CORSMiddleware,
        allow_origins=resolved.CORS_ORIGINS,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    application.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    @application.get("/", include_in_schema=False)
    async def index() -> FileResponse:
        """Serve the single-page interface."""
        return FileResponse(STATIC_DIR / "index.html")

    @application.get("/health", response_model=HealthResponse, tags=["ops"])
    @application.get("/healthz", response_model=HealthResponse, include_in_schema=False)
    async def health(request: Request) -> HealthResponse:
        """Report service liveness and uptime."""
        started_at: float = getattr(request.app.state, "started_at", time.monotonic())
        return HealthResponse(
            status="healthy",
            service=SERVICE_NAME,
            version=request.app.version,
            uptime_seconds=round(time.monotonic() - started_at, 3),
        )

    @application.get("/api/agent/stream", tags=["agent"])
    @application.get("/api/chat/stream", include_in_schema=False)
    async def agent_stream(
        request: Request,
        message: str = Query(..., min_length=1, description="User message for the coach agent"),
    ) -> EventSourceResponse:
        """Stream the coach agent's reasoning, tool calls and plan over SSE.

        Native ``EventSource`` clients consume the typed events emitted by
        :meth:`coach_web.agent.CoachAgent.run`.
        """
        settings: Settings = request.app.state.settings
        factory = getattr(request.app.state, "agent_factory", create_agent)
        agent: CoachAgent = factory(settings)

        async def event_generator() -> AsyncIterator[ServerSentEvent]:
            try:
                async with agent:
                    async for event in agent.run(message):
                        yield ServerSentEvent(event=event.type, data=event.model_dump_json())
            except Exception as exc:  # noqa: BLE001 - the SSE boundary must never leak
                logger.warning("Agent stream failed: %s", exc)
                failure = AgentEvent(type="error", data={"message": str(exc)})
                yield ServerSentEvent(event=failure.type, data=failure.model_dump_json())

        return EventSourceResponse(event_generator())

    return application


def run() -> None:
    """Run the Coach Web ASGI application with uvicorn."""
    settings = get_settings()
    uvicorn.run(
        "coach_web.app:create_app",
        factory=True,
        host=settings.APP_HOST,
        port=settings.APP_PORT,
        log_level=settings.LOG_LEVEL.lower(),
    )
