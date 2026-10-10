"""FastAPI application factory for Coach Web."""

import logging
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Any

import uvicorn
from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ValidationError
from sqlalchemy.exc import SQLAlchemyError
from sse_starlette import EventSourceResponse, ServerSentEvent
from starlette.datastructures import MutableHeaders
from starlette.middleware.trustedhost import TrustedHostMiddleware
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from coach_web.agent import DEFAULT_SYSTEM_PROMPT, AgentEvent, CoachAgent, create_agent
from coach_web.auth.middleware import AuthMiddleware, validate_auth_config
from coach_web.auth.router import router as auth_router
from coach_web.config import Settings, get_settings
from coach_web.db import build_db_engine, build_session_factory
from coach_web.mcp_hub import MCPClientHub, MCPHubError
from coach_web.models import (
    AgentStreamRequest,
    PlanApprovalRequest,
    PlanApprovalResponse,
)
from coach_web.objectives import (
    ObjectiveProfile,
    ObjectiveProfileResponse,
    apply_objectives_digest,
    get_objective_profile,
    save_objective_profile,
)
from coach_web.plan_approval import approve_plan

logger = logging.getLogger("coach_web")

STATIC_DIR = Path(__file__).parent / "static"
SERVICE_NAME = "coach-web"
PACKAGE_NAME = "coach-web"
FALLBACK_VERSION = "0.0.0"

GENERIC_VALIDATION_DETAIL = "Invalid request payload"
"""Static 422 detail — validation errors stay server-side (nLPD, #142)."""

# Minimal v0.7 Content-Security-Policy (STRIDE #87 sign-off, conditions
# C2/C5 — shipped as middleware in #84). 'unsafe-eval' is required by the
# vendored Alpine standard build (it compiles x- expressions via AsyncFunction
# at runtime) and is contained: script-src has no 'unsafe-inline', so injected
# inline scripts are blocked, and DOMPurify strips every Alpine directive
# (#81). The Alpine CSP build remains a future hardening item.
CSP_POLICY = (
    "default-src 'self'; script-src 'self' 'unsafe-eval'; style-src 'self'; "
    "img-src 'self' data:; font-src 'self'; connect-src 'self'; object-src 'none'; "
    "base-uri 'self'; frame-ancestors 'none'; form-action 'self'"
)


class ContentSecurityPolicyMiddleware:
    """Stamp every response with the minimal v0.7 CSP header (#84, C2/C5).

    Pure ASGI middleware (no ``BaseHTTPMiddleware``) so the header is applied
    to every response — pages, static assets, API/SSE routes and
    middleware-generated error responses alike — without buffering or
    otherwise touching the SSE stream. Registered by :class:`CoachWebApp`
    around the ENTIRE ASGI stack (#148), so the 500 that Starlette's
    ``ServerErrorMiddleware`` renders for an unhandled exception is stamped
    too.
    """

    def __init__(self, app: ASGIApp, policy: str = CSP_POLICY) -> None:
        self.app = app
        self.policy = policy

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        async def send_with_csp(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                headers["Content-Security-Policy"] = self.policy
            await send(message)

        await self.app(scope, receive, send_with_csp)


class CoachWebApp(FastAPI):
    """FastAPI app with the CSP stamp outside the entire Starlette stack (#148).

    Starlette always builds ``ServerErrorMiddleware`` as the outermost layer
    of its middleware stack, so a middleware registered via ``add_middleware``
    runs inside it and cannot stamp the response ``ServerErrorMiddleware``
    renders for an unhandled exception — the bare 500 shipped without the CSP
    header (#127 review advisory). Overriding the ASGI entrypoint wraps the
    fully built stack with :class:`ContentSecurityPolicyMiddleware`, so every
    response — 200, SSE stream, 401/403, TrustedHost 400 and the
    unhandled-exception 500 — carries the exact #87 policy from a single
    stamping point, and the pure-ASGI send wrapper keeps the SSE stream
    unbuffered.
    """

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._csp_wrapped: ASGIApp = ContentSecurityPolicyMiddleware(super().__call__)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        await self._csp_wrapped(scope, receive, send)


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
    # Release the persistence engine's connection pool (MADR-008, #166).
    await app.state.db_engine.dispose()


def create_app(settings: Settings | None = None) -> FastAPI:
    """Create and configure the Coach Web FastAPI application."""
    resolved = settings or get_settings()

    # Fail closed: refuse to boot with auth enabled but incomplete credentials.
    if resolved.AUTH_ENABLED:
        validate_auth_config(resolved)

    application = CoachWebApp(
        title="Coach Web",
        version=resolve_version(),
        lifespan=lifespan,
    )
    application.state.settings = resolved
    application.state.started_at = time.monotonic()
    application.state.agent_factory = create_agent
    application.state.hub_factory = MCPClientHub.from_settings

    # Persistence (MADR-008, #166): one async engine per app instance over the
    # configured SQLite file. Engine construction is lazy — no filesystem
    # access happens until a connection is opened — and the lifespan disposes
    # the pool on shutdown.
    db_engine = build_db_engine(resolved.COACH_DB_PATH)
    application.state.db_engine = db_engine
    application.state.db_session_factory = build_session_factory(db_engine)

    application.add_middleware(
        CORSMiddleware,
        allow_origins=resolved.CORS_ORIGINS,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    if resolved.AUTH_ENABLED:
        # Auth middleware is added last => outermost: the whitelist boundary
        # is enforced before any route, mount or CORS logic runs.
        application.include_router(auth_router)
        application.add_middleware(AuthMiddleware)

    # Host allowlist (DNS-rebinding mitigation, #112): a malicious page that
    # resolves a hostname to 127.0.0.1 must not reach the loopback service
    # same-origin. Added last => outermost, so a bad Host is rejected before
    # auth/CORS. Critical while dev/qa run AUTH_ENABLED=false.
    application.add_middleware(
        TrustedHostMiddleware,
        allowed_hosts=resolved.TRUSTED_HOSTS,
        www_redirect=False,
    )

    # Content-Security-Policy (STRIDE #87 conditions C2/C5, #84; #148): the
    # minimal v0.7 policy is stamped by the CoachWebApp ASGI wrapper, which
    # sits OUTSIDE the entire Starlette stack — so every response, including
    # the TrustedHost 400, the auth 401/403 and the ServerErrorMiddleware 500,
    # carries the header from a single stamping point.
    application.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    @application.exception_handler(RequestValidationError)
    async def validation_error_handler(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        """Reject malformed payloads with a generic 422 detail (nLPD, #142).

        FastAPI's default validation handler echoes the rejected input back to
        the client; the nLPD posture keeps validation detail server-side only —
        the full errors are logged, the response carries a static string.
        """
        logger.warning("Rejected malformed request on %s: %s", request.url.path, exc.errors())
        return JSONResponse(status_code=422, content={"detail": GENERIC_VALIDATION_DETAIL})

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

    @application.post("/api/agent/stream", tags=["agent"])
    async def agent_stream(
        request: Request,
        payload: AgentStreamRequest,
    ) -> EventSourceResponse:
        """Stream the coach agent's reasoning, tool calls and plan over SSE.

        The conversation is client-owned (nLPD ephemeral posture): the request
        body carries the current ``message`` plus the replayed ``history``
        (validated and capped by :class:`AgentStreamRequest`) and nothing is
        persisted server-side. Message content travels in the POST body —
        never in the URL or query string (#79). Native ``fetch``/ReadableStream
        clients consume the typed events emitted by
        :meth:`coach_web.agent.CoachAgent.run`.
        """
        settings: Settings = request.app.state.settings
        factory = getattr(request.app.state, "agent_factory", create_agent)
        agent: CoachAgent = factory(settings)

        # Objectives digest (AC3, #166): the coach system prompt gains a
        # compact, token-budgeted digest of the persisted athlete objectives
        # when they exist. Absent objectives — or a persistence layer that is
        # momentarily unreadable, or a stored row tampered into a
        # Pydantic-invalid shape (raw SQL bypasses the CHECK constraints) —
        # degrade to no digest, so the chat never depends on the database's
        # health (graceful empty state).
        try:
            async with request.app.state.db_session_factory() as session:
                profile = await get_objective_profile(session)
        except (OSError, SQLAlchemyError, ValidationError) as exc:
            logger.warning("Objectives digest unavailable: %s", exc)
            profile = None
        if profile is not None:
            # Duck-typed agents (test stubs) may not carry the attribute; the
            # default prompt is the base every real agent is built with.
            base_prompt = getattr(agent, "system_prompt", DEFAULT_SYSTEM_PROMPT)
            agent.system_prompt = apply_objectives_digest(base_prompt, profile)

        async def event_generator() -> AsyncIterator[ServerSentEvent]:
            try:
                async with agent:
                    async for event in agent.run(payload.message, history=payload.history):
                        yield ServerSentEvent(event=event.type, data=event.model_dump_json())
            except Exception as exc:  # noqa: BLE001 - the SSE boundary must never leak
                logger.warning("Agent stream failed: %s", exc)
                failure = AgentEvent(type="error", data={"message": str(exc)})
                yield ServerSentEvent(event=failure.type, data=failure.model_dump_json())

        return EventSourceResponse(event_generator())

    @application.post("/api/plan/approve", response_model=PlanApprovalResponse, tags=["plan"])
    async def approve_plan_endpoint(
        request: Request,
        payload: PlanApprovalRequest,
    ) -> PlanApprovalResponse:
        """Approve a plan proposal and execute its scheduling side effects.

        Schedules the workout on Intervals.icu through ``coach-mcp`` and commits
        the Markdown plan to the training repository through ``github-mcp``.
        """
        settings: Settings = request.app.state.settings
        factory = getattr(request.app.state, "hub_factory", MCPClientHub.from_settings)
        hub = factory(settings)
        try:
            async with hub:
                return await approve_plan(
                    hub,
                    payload.plan,
                    repo=settings.GITHUB_REPO,
                    branch=settings.GITHUB_PLAN_BRANCH,
                    directory=settings.GITHUB_PLAN_DIR,
                )
        except MCPHubError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc

    @application.get(
        "/api/objectives",
        response_model=ObjectiveProfileResponse,
        tags=["objectives"],
    )
    async def get_objectives_endpoint(request: Request) -> ObjectiveProfileResponse:
        """Return the persisted athlete objective profile (null when absent).

        Auth-gated by the whitelist middleware when AUTH_ENABLED; the profile
        is the coach's strategic anchor (epic #161 story 1.1, MADR-008 C1).
        """
        session_factory = request.app.state.db_session_factory
        async with session_factory() as session:
            profile = await get_objective_profile(session)
        return ObjectiveProfileResponse(profile=profile)

    @application.put(
        "/api/objectives",
        response_model=ObjectiveProfileResponse,
        tags=["objectives"],
    )
    async def put_objectives_endpoint(
        request: Request,
        payload: ObjectiveProfile,
    ) -> ObjectiveProfileResponse:
        """Validate and persist the athlete objective profile (PUT replaces).

        Strict Pydantic validation runs at the boundary; malformed payloads
        are rejected 422 with a generic detail (nLPD #142 precedent). The
        profile upserts the single active ``athlete_objectives`` row.
        """
        session_factory = request.app.state.db_session_factory
        async with session_factory() as session:
            saved = await save_objective_profile(session, payload)
        return ObjectiveProfileResponse(profile=saved)

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
