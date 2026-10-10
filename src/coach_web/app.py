"""FastAPI application factory for Coach Web."""

import logging
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import date
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Any

import uvicorn
from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ValidationError
from sqlalchemy.exc import SQLAlchemyError
from sse_starlette import EventSourceResponse, ServerSentEvent
from starlette.datastructures import MutableHeaders
from starlette.middleware.trustedhost import TrustedHostMiddleware
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from coach_web.agent import DEFAULT_SYSTEM_PROMPT, AgentEvent, CoachAgent, create_agent
from coach_web.auth.middleware import (
    SESSION_COOKIE,
    AuthMiddleware,
    validate_auth_config,
)
from coach_web.auth.router import router as auth_router
from coach_web.auth.tokens import verify_session_token
from coach_web.config import Settings, get_settings
from coach_web.db import build_db_engine, build_session_factory
from coach_web.mcp_hub import MCPClientHub, MCPHubError
from coach_web.microcycle import (
    DayWriteResult,
    PlanDraftRow,
    WeekApprovalRequest,
    WeekApprovalResponse,
    transition_draft_status,
    upsert_week_draft,
    write_week_days,
)
from coach_web.models import (
    AgentStreamRequest,
    PlanApprovalRequest,
    PlanApprovalResponse,
)
from coach_web.objectives import (
    ObjectiveProfile,
    ObjectiveProfileResponse,
    apply_objectives_digest,
    get_active_objective_row,
    get_objective_profile,
    save_objective_profile,
)
from coach_web.periodization import (
    PeriodizationPlan,
    PeriodizationPlanResponse,
    list_phases,
    replace_phases,
    validate_plan_coverage,
)
from coach_web.plan_approval import approve_plan

logger = logging.getLogger("coach_web")

STATIC_DIR = Path(__file__).parent / "static"
SERVICE_NAME = "coach-web"
PACKAGE_NAME = "coach-web"
FALLBACK_VERSION = "0.0.0"

GENERIC_VALIDATION_DETAIL = "Invalid request payload"
"""Static 422 detail — validation errors stay server-side (nLPD, #142)."""

GENERIC_PERSISTENCE_DETAIL = "Service temporarily unavailable"
"""Static 503 detail for persistence outages — no internals echoed (nLPD)."""

_SIGNIN_SLOT = (
    "<!-- #180 signin-slot: swapped for the sign-in link by the / route"
    " when it renders an unauthenticated session (auth enabled). -->"
)
"""Inert marker comment in index.html's identity bar (the injection anchor).

The raw static file keeps the comment, so /static/index.html and
auth-disabled lanes stay byte-identical to the pre-#180 page (AC3).
"""

_SIGNIN_LINK_HTML = '<a class="signin-link" href="/auth/login">Sign in</a>'
"""The unauthenticated identity-bar affordance (#180): a real link, no JS."""


def _has_valid_session(request: Request, settings: Settings) -> bool:
    """Return True when the request carries a verifiable session cookie (#180).

    Verification reuses the auth session logic (HS256
    :func:`coach_web.auth.tokens.verify_session_token` — the same check the
    whitelist middleware applies). Fail-safe by contract: ANY error reading
    or verifying the cookie (missing cookie, bad signature, expiry, malformed
    header) resolves to False, so the public landing page renders the
    unauthenticated variant instead of ever raising.
    """
    try:
        token = request.cookies.get(SESSION_COOKIE)
        if not token:
            return False
        verify_session_token(token, settings.AUTH_SESSION_SECRET)
    except Exception:  # noqa: BLE001 - fail-safe: the landing page must never 500
        return False
    return True


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
    async def index(request: Request) -> Response:
        """Serve the single-page interface with a conditional sign-in affordance (#180).

        The landing page is public (AC4 #65) but must not dead-end an
        unauthenticated visitor (#180): with auth enabled and no valid
        session cookie, the identity bar gains a visible "Sign in" link to
        /auth/login. Authenticated sessions and AUTH_ENABLED=false lanes
        (dev/qa) get the static file byte-for-byte as today. Fail-safe: any
        cookie or validation error resolves to the unauthenticated variant —
        the landing page can never 500. No auto-redirect: the public-landing
        posture is preserved (rejected-by-default option 3, #180).
        """
        settings: Settings = request.app.state.settings
        if settings.AUTH_ENABLED and not _has_valid_session(request, settings):
            html = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
            if _SIGNIN_SLOT in html:
                return Response(
                    content=html.replace(_SIGNIN_SLOT, _SIGNIN_LINK_HTML),
                    media_type="text/html",
                )
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

        # Objectives digest (AC3, #166; phase extension #167): the coach
        # system prompt gains a compact, token-budgeted digest of the
        # persisted athlete objectives when they exist, plus the calendar-
        # computed periodization state (current phase, target countdown,
        # load hint) when phases exist. Absent objectives — or a persistence
        # layer that is momentarily unreadable, or a stored row tampered into
        # a Pydantic-invalid shape (raw SQL bypasses the CHECK constraints) —
        # degrade to no digest, so the chat never depends on the database's
        # health (graceful empty state). Both reads share one boundary: any
        # failure yields no digest at all.
        try:
            async with request.app.state.db_session_factory() as session:
                profile = await get_objective_profile(session)
                objective_row = await get_active_objective_row(session)
                phases = (
                    await list_phases(session, objective_row.id)
                    if objective_row is not None
                    else []
                )
        except (OSError, SQLAlchemyError, ValidationError) as exc:
            logger.warning("Objectives digest unavailable: %s", exc)
            profile = None
            phases = []
        if profile is not None:
            # Duck-typed agents (test stubs) may not carry the attribute; the
            # default prompt is the base every real agent is built with.
            base_prompt = getattr(agent, "system_prompt", DEFAULT_SYSTEM_PROMPT)
            # The clock is resolved once here (N3, PR #173): the digest's
            # phase resolution takes an explicit ``today`` so the call site
            # owns the clock boundary instead of the domain default.
            agent.system_prompt = apply_objectives_digest(
                base_prompt, profile, phases=phases, today=date.today()
            )

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

    @application.post("/api/week/approve", response_model=WeekApprovalResponse, tags=["plan"])
    async def approve_week_endpoint(
        request: Request,
        payload: WeekApprovalRequest,
    ) -> WeekApprovalResponse:
        """Approve a weekly plan and write one Intervals.icu event per day.

        The draft is persisted first (MADR-008 ``plan_drafts``: create-or-reuse
        per week, replacement rule, ``submitted`` while the write is in
        flight), then the days are written SEQUENTIALLY through ``coach-mcp``
        (#163: no batch tool — the write is not atomic). Every day yields its
        own :class:`~coach_web.microcycle.DayWriteResult`; the draft only
        transitions to ``approved`` when every requested day succeeded, so a
        partial failure stays ``submitted`` for the retry-failed-days
        affordance (AC5: no silent partial calendar). The explicit POST is
        the approval gate — human-in-the-loop preserved (#87 C7).
        """
        settings: Settings = request.app.state.settings
        factory = getattr(request.app.state, "hub_factory", MCPClientHub.from_settings)
        hub = factory(settings)
        session_factory = request.app.state.db_session_factory

        # 1. Persist the draft before any side effect (MADR-008, AC4).
        try:
            async with session_factory() as session:
                row = await upsert_week_draft(session, payload.week)
                draft_id = row.id
        except (OSError, SQLAlchemyError) as exc:
            logger.warning("Week draft persistence failed: %s", exc)
            raise HTTPException(status_code=503, detail=GENERIC_PERSISTENCE_DETAIL) from exc

        # Terminal-state short-circuit (PR #174 F3): an already-APPROVED
        # matching draft means the week was fully written to the calendar —
        # re-issuing the events would create duplicates (intervals_create_event
        # is not idempotent, #163). Report the existing success without any
        # tool call. A REJECTED draft was promoted back to submitted by the
        # upsert, so the write proceeds below.
        if row.status == "approved":
            requested = (
                payload.week.days
                if payload.dates is None
                else [day for day in payload.week.days if day.date in set(payload.dates)]
            )
            return WeekApprovalResponse(
                results=[DayWriteResult(date=day.date, success=True) for day in requested]
            )

        # 2. Sequential per-day calendar writes (#163).
        try:
            async with hub:
                results = await write_week_days(hub, payload.week, payload.dates)
        except MCPHubError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc

        # 3. Transition on outcome: approved only when EVERY requested day
        # succeeded; a partial failure stays submitted for retry. A
        # transition failure after successful writes must not mask the
        # per-day results — it is logged and the response still reports them.
        if all(result.success for result in results):
            try:
                async with session_factory() as session:
                    stored = await session.get(PlanDraftRow, draft_id)
                    if stored is not None and stored.status == "submitted":
                        await transition_draft_status(session, stored, "approved")
            except (OSError, SQLAlchemyError) as exc:
                logger.warning("Week draft approval transition failed: %s", exc)

        return WeekApprovalResponse(results=results)

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

    @application.get(
        "/api/periodization",
        response_model=PeriodizationPlanResponse,
        tags=["objectives"],
    )
    async def get_periodization_endpoint(request: Request) -> PeriodizationPlanResponse:
        """Return the active objective's periodization phases (empty when absent).

        Auth-gated by the whitelist middleware when AUTH_ENABLED; the phases
        are the macrocycle plan anchored on the active objective (epic #161
        story 1.2, MADR-008 C1).
        """
        session_factory = request.app.state.db_session_factory
        async with session_factory() as session:
            objective_row = await get_active_objective_row(session)
            if objective_row is None:
                return PeriodizationPlanResponse(objective_id=None, phases=[])
            phases = await list_phases(session, objective_row.id)
            return PeriodizationPlanResponse(objective_id=objective_row.id, phases=phases)

    @application.put(
        "/api/periodization",
        response_model=PeriodizationPlanResponse,
        tags=["objectives"],
    )
    async def put_periodization_endpoint(
        request: Request,
        payload: PeriodizationPlan,
    ) -> PeriodizationPlanResponse:
        """Validate and persist the active objective's periodization phases.

        Strict Pydantic validation runs at the boundary (phase shape, enum,
        ISO dates, monotonic non-overlapping ranges); the coverage
        cross-check against the objective's target date runs here — it needs
        the persisted parent row. Malformed plans are rejected 422 with a
        generic detail (nLPD #142 precedent); the plan replaces the
        objective's phase set wholesale (PUT semantics).
        """
        session_factory = request.app.state.db_session_factory
        async with session_factory() as session:
            objective_row = await get_active_objective_row(session)
            if objective_row is None:
                # Phases hang off the active objective; without one the plan
                # has nowhere to live. Generic 422 — no state echo (nLPD).
                raise HTTPException(status_code=422, detail=GENERIC_VALIDATION_DETAIL)
            try:
                validate_plan_coverage(payload, objective_row.target_date)
            except ValueError as exc:
                logger.warning("Rejected periodization plan: %s", exc)
                raise HTTPException(status_code=422, detail=GENERIC_VALIDATION_DETAIL) from exc
            objective_id = objective_row.id
            phases = await replace_phases(session, objective_id, payload)
        return PeriodizationPlanResponse(objective_id=objective_id, phases=phases)

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
