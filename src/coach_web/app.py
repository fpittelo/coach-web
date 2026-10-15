"""FastAPI application factory for Coach Web."""

import logging
import os
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

from coach_web.agent import (
    DEFAULT_SYSTEM_PROMPT,
    AgentEvent,
    CoachAgent,
    apply_temporal_anchor,
    create_agent,
)
from coach_web.auth.middleware import (
    SESSION_COOKIE,
    AuthMiddleware,
    validate_auth_config,
)
from coach_web.auth.router import router as auth_router
from coach_web.auth.tokens import TokenError, verify_session_token
from coach_web.config import Settings, get_settings
from coach_web.db import build_db_engine, build_session_factory
from coach_web.debrief import (
    DEFAULT_ATHLETE_ID,
    DebriefMirrorOutcome,
    DebriefSaveRequest,
    DebriefSaveResponse,
    PendingDebriefResponse,
    apply_pending_debrief_digest,
    athlete_id_from_email,
    build_debrief_cipher,
    list_pending_debriefs,
    save_debrief,
)
from coach_web.errors import (
    MIRROR_REASON_MESSAGES,
    EncryptionUnavailableError,
    MirrorReason,
    SettingsValidationError,
    encryption_reason_payload,
    settings_reason_from_errors,
    settings_reason_payload,
)
from coach_web.mcp_hub import MCPClientHub, MCPHubError
from coach_web.microcycle import (
    DayWriteResult,
    PlanDraftRow,
    WeekApprovalRequest,
    WeekApprovalResponse,
    apply_approved_week_digest,
    build_week_markdown,
    list_drafts_by_week,
    past_days_render_identically,
    transition_draft_status,
    upsert_week_draft,
    week_bounds,
    write_week_days,
)
from coach_web.mirror import mirror_debrief_to_icu
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
    PeriodizationApprovalRequest,
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
"""Static 422 detail — validation errors stay server-side (nLPD, #142).

Chat/model-facing surfaces keep this strict posture UNCHANGED (#181 AC3);
the owner-facing settings paths answer with the structured reason body.
"""

SETTINGS_REASON_PATHS = frozenset({"/api/objectives", "/api/periodization/approve"})
"""The owner-facing settings endpoints whose 422s carry the reason body (#181).

The #142 threat model is the LLM/prompt-injection surface — not the
authenticated owner's own settings form, where the requester IS the data
owner and a validation reason is not sensitive data. Only these paths get
the structured ``{code, message}`` body; every other 422 (agent stream, plan
approval, week approval) keeps the generic detail verbatim.
"""

GENERIC_PERSISTENCE_DETAIL = "Service temporarily unavailable"
"""Static 503 detail for persistence outages — no internals echoed (nLPD)."""

WEEK_ADJUSTMENT_PAST_DAYS_DETAIL = "Week adjustment rejected: past days are immutable"
"""Static 422 detail for an adjustment touching a past day (#225 C2, AC3).

Past-day immutability is a security property: the body stays value-free
(no dates, no titles, no draft content) per the GENERIC_VALIDATION_DETAIL
precedent.
"""

WEEK_ADJUSTMENT_AVAILABILITY_DETAIL = "Week adjustment rejected: weekly availability exceeded"
"""Static 422 detail for an adjustment over the availability budget (#225 C2, D2)."""

WEEK_ADJUSTMENT_EMPTY_WRITE_SET_DETAIL = "Week adjustment rejected: no future days to adjust"
"""Static 422 detail for an adjustment whose write set is empty (#225 C2, D3.4)."""

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


def _athlete_id_for_session(settings: Settings, session_cookie: str | None) -> int:
    """Resolve the debrief athlete id from the request context (#189 AC4).

    Auth-disabled lanes (dev/qa) map to the single local athlete. Auth-enabled
    requests derive the id from the verified session email — the middleware
    has already admitted the request, so the defensive branches below are a
    fail-safe against a crash, never an access-control decision: an
    unverifiable cookie degrades to the default athlete instead of raising.
    """
    if not settings.AUTH_ENABLED:
        return DEFAULT_ATHLETE_ID
    if not session_cookie:
        return DEFAULT_ATHLETE_ID
    try:
        claims = verify_session_token(session_cookie, settings.AUTH_SESSION_SECRET)
    except TokenError:
        return DEFAULT_ATHLETE_ID
    return athlete_id_from_email(claims.get("email"))


async def _default_debrief_mirror(
    settings: Settings,
    *,
    plan_session_key: str,
    rpe: int,
    notes: str | None,
) -> DebriefMirrorOutcome:
    """Mirror one saved debrief to Intervals.icu through the real hub (#190 AC3).

    Thin adapter over :func:`coach_web.mirror.mirror_debrief_to_icu` matching
    the ``app.state.debrief_mirror`` hook signature, so tests can inject a
    fake (ZERO network) while production routes through the MCP hub. The
    resolved activity id is deliberately dropped: linking the debrief row to
    its ICU mirror is not required by any #190 AC (KIS/YAGNI) — the outcome
    alone rides the response.
    """
    outcome, _activity_id = await mirror_debrief_to_icu(
        MCPClientHub.from_settings,
        settings,
        plan_session_key=plan_session_key,
        rpe=rpe,
        notes=notes,
    )
    return outcome


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


VERSION_ENV_OVERRIDE = "COACH_WEB_VERSION"
"""Build-time version override (#198): the single source of truth for images.

CI derives the value from the git ref — the release tag with the leading
``v`` stripped for release builds, ``<lane>-<short-sha>`` for dev/qa lane
builds — and passes it as the ``COACH_WEB_VERSION`` Docker build-arg, which
the Dockerfile bakes into the runtime environment. No manual ``pyproject``
bump ever participates, so the reported version can never drift from the
built ref again.
"""


def resolve_version() -> str:
    """Resolve the reported version: build override, metadata, honest fallback.

    Resolution order (#198):
    1. the ``COACH_WEB_VERSION`` env override (set at image build time),
    2. the installed distribution metadata (local/editable installs),
    3. ``0.0.0`` — the honest "unknown" sentinel for bare source checkouts.
    """
    override = os.environ.get(VERSION_ENV_OVERRIDE, "")
    if override:
        return override
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
    application.state.debrief_mirror = _default_debrief_mirror

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
        """Reject malformed payloads with a static 422 body (nLPD, #142 + #181).

        FastAPI's default validation handler echoes the rejected input back to
        the client; the nLPD posture keeps validation detail server-side only —
        the full errors are logged, the response carries a static string. On
        the owner-facing settings paths (#181) the body is instead the
        structured reason ``{code, message}``: the reason is threaded through
        the validators as a typed exception attribute (never string-matched)
        and unattributed errors map to the ``invalid_values`` catch-all, so
        the body shape stays consistent for the UI contract.
        """
        errors = exc.errors()
        logger.warning("Rejected malformed request on %s: %s", request.url.path, errors)
        if request.url.path in SETTINGS_REASON_PATHS:
            reason = settings_reason_from_errors(errors)
            return JSONResponse(status_code=422, content=settings_reason_payload(reason))
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
        #
        # Pending-debrief digest (MADR-0010 greeting hook, #189 AC3): the
        # prompt also gains the athlete's pending debriefs (completed plan
        # sessions without a debrief row — metadata only: date + title) so
        # the coach opens the debrief flow with the request_rpe tool. The
        # same failure boundary applies: an unreadable store yields no
        # pending digest and never breaks the turn.
        #
        # Approved-week digest (D4, #225): when the CURRENT ISO week already
        # has an approved draft, the prompt gains its compact metadata
        # digest (week id, title, per-day date/title/TSS/rest marker) so a
        # mid-week adjustment request can be grounded in what is already on
        # the calendar (past days are locked server-side). The identical
        # failure boundary applies: an unreadable store yields no digest at
        # all and never breaks the turn.
        #
        # Temporal anchor (#220): the model is never told today's date by any
        # digest, so it infers the date from tool payloads and can anchor on
        # the wrong time of year. The anchor is applied to the base prompt
        # UNCONDITIONALLY — before the digest boundary below — so a
        # persistence failure (no digest at all) still leaves the coach with
        # the correct date on every turn. The clock is resolved ONCE here
        # (N3, PR #173) — the same request-scoped boundary the digests use —
        # and passed down; the agent never reads the clock itself.
        today = date.today()
        # Duck-typed agents (test stubs) may not carry the attribute; the
        # default prompt is the base every real agent is built with.
        base_prompt = getattr(agent, "system_prompt", DEFAULT_SYSTEM_PROMPT)
        agent.system_prompt = apply_temporal_anchor(base_prompt, today)
        iso_year, iso_week, _ = today.isocalendar()
        current_week_start, _ = week_bounds(f"{iso_year}-W{iso_week:02d}")
        approved_week_row: PlanDraftRow | None = None
        try:
            async with request.app.state.db_session_factory() as session:
                profile = await get_objective_profile(session)
                objective_row = await get_active_objective_row(session)
                phases = (
                    await list_phases(session, objective_row.id)
                    if objective_row is not None
                    else []
                )
                pending_debriefs = await list_pending_debriefs(
                    session,
                    athlete_id=_athlete_id_for_session(
                        settings, request.cookies.get(SESSION_COOKIE)
                    ),
                    today=today,
                )
                approved_rows = [
                    row
                    for row in await list_drafts_by_week(session, current_week_start)
                    if row.status == "approved"
                ]
                approved_week_row = approved_rows[-1] if approved_rows else None
        except (OSError, SQLAlchemyError, ValidationError) as exc:
            logger.warning("Objectives digest unavailable: %s", exc)
            profile = None
            phases = []
            pending_debriefs = []
            approved_week_row = None
        if profile is not None:
            # The digest's phase resolution takes the same explicit ``today``
            # so the call site owns the clock boundary (N3, PR #173).
            agent.system_prompt = apply_objectives_digest(
                agent.system_prompt, profile, phases=phases, today=today
            )
        if pending_debriefs:
            agent.system_prompt = apply_pending_debrief_digest(
                agent.system_prompt, pending_debriefs
            )
        if approved_week_row is not None:
            agent.system_prompt = apply_approved_week_digest(agent.system_prompt, approved_week_row)

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
        """Approve a weekly plan and write its days as one batch upsert (#225).

        The draft is persisted first (MADR-008 ``plan_drafts``: create-or-reuse
        per week, replacement rule, ``submitted`` while the write is in
        flight), then the requested days are written through the confirm-
        gated ``coach-mcp__icu_batch_create_events`` upsert (D1 — idempotent
        per ``external_id``, so re-approvals never duplicate events). Every
        day yields its own :class:`~coach_web.microcycle.DayWriteResult`;
        the draft only transitions to ``approved`` when every requested day
        succeeded, so a partial failure stays ``submitted`` for the
        retry-failed-days affordance (AC5: no silent partial calendar).

        Adjusting an already-approved week (D3): the current approved row is
        captured BEFORE the upsert supersedes it, and a changed incoming
        draft is validated as a mid-week adjustment — past days must render
        byte-identically to the stored rows, the write set is restricted to
        days strictly after today (both 422 with a static body, C2), and the
        adjusted week must fit the active objective's weekly availability
        budget (D2). ``today`` is resolved ONCE here and passed down (D3.5).
        The explicit POST is the approval gate — human-in-the-loop preserved
        (#87 C7).
        """
        settings: Settings = request.app.state.settings
        factory = getattr(request.app.state, "hub_factory", MCPClientHub.from_settings)
        hub = factory(settings)
        session_factory = request.app.state.db_session_factory

        # Clock boundary (D3.5): resolved ONCE per request, passed down —
        # helpers never read the clock themselves.
        today = date.today()

        # 1. Capture the CURRENT approved draft BEFORE the upsert supersedes
        # it (D3.1), plus the active objective profile for the availability
        # gate (D2). The profiles read degrades to None on failure (graceful,
        # same posture as the agent_stream digests); the drafts read is
        # required — a persistence outage answers the static 503.
        week_start, _ = week_bounds(payload.week.week_id)
        try:
            async with session_factory() as session:
                stored_rows = await list_drafts_by_week(session, week_start)
                try:
                    profile = await get_objective_profile(session)
                except (OSError, SQLAlchemyError, ValidationError):
                    logger.warning("Week adjustment availability gate unavailable")
                    profile = None
        except (OSError, SQLAlchemyError) as exc:
            logger.warning("Week draft persistence failed: %s", exc)
            raise HTTPException(status_code=503, detail=GENERIC_PERSISTENCE_DETAIL) from exc
        approved_rows = [row for row in stored_rows if row.status == "approved"]
        stored = approved_rows[-1] if approved_rows else None

        # 2. Adjustment detection (D3.2): an approved row whose stored
        # content DIFFERS from the incoming draft is a mid-week adjustment;
        # identical content keeps the unchanged approval path below.
        write_dates = payload.dates
        if stored is not None and build_week_markdown(payload.week) != stored.content:
            # 2a. Past-day immutability (D3.3, AC3): every past day must
            # render byte-identically to its stored day-table row.
            if not past_days_render_identically(payload.week, stored.content, today):
                raise HTTPException(status_code=422, detail=WEEK_ADJUSTMENT_PAST_DAYS_DETAIL)
            # 2b. Write-set exclusion (D3.4): the batch may touch ONLY days
            # strictly after today; an explicit request targeting a past day
            # is rejected outright.
            if payload.dates is not None and any(
                requested_date <= today.isoformat() for requested_date in payload.dates
            ):
                raise HTTPException(status_code=422, detail=WEEK_ADJUSTMENT_PAST_DAYS_DETAIL)
            requested_days = (
                payload.week.days
                if payload.dates is None
                else [day for day in payload.week.days if day.date in set(payload.dates)]
            )
            write_set = [day for day in requested_days if day.date > today.isoformat()]
            if not write_set:
                raise HTTPException(status_code=422, detail=WEEK_ADJUSTMENT_EMPTY_WRITE_SET_DETAIL)
            # 2c. Availability gate (D2, AC6): the adjusted week must fit the
            # athlete's weekly availability budget; no active objective
            # degrades to no gate (the digests' graceful posture).
            if (
                profile is not None
                and sum(day.duration_minutes for day in payload.week.days)
                > profile.weekly_availability_hours * 60
            ):
                logger.warning("Rejected week adjustment over the availability budget")
                raise HTTPException(status_code=422, detail=WEEK_ADJUSTMENT_AVAILABILITY_DETAIL)
            write_dates = [day.date for day in write_set]

        # 3. Persist the draft before any side effect (MADR-008, AC4) — on
        # the adjustment path the upsert SUPERSEDES the captured approved row
        # and lands the replacement as ``submitted`` (AC2).
        try:
            async with session_factory() as session:
                row = await upsert_week_draft(session, payload.week)
                draft_id = row.id
        except (OSError, SQLAlchemyError) as exc:
            logger.warning("Week draft persistence failed: %s", exc)
            raise HTTPException(status_code=503, detail=GENERIC_PERSISTENCE_DETAIL) from exc

        # Terminal-state short-circuit (PR #174 F3): an already-APPROVED
        # matching draft means the week was fully written to the calendar —
        # re-issuing the events would create duplicates. Report the existing
        # success without any tool call. A REJECTED draft was promoted back
        # to submitted by the upsert, so the write proceeds below.
        if row.status == "approved":
            requested = (
                payload.week.days
                if payload.dates is None
                else [day for day in payload.week.days if day.date in set(payload.dates)]
            )
            return WeekApprovalResponse(
                results=[DayWriteResult(date=day.date, success=True) for day in requested]
            )

        # 4. Two-phase batch calendar writes (D1) — on the adjustment path
        # the batch already excludes every day on or before today (2b).
        try:
            async with hub:
                results = await write_week_days(hub, payload.week, write_dates)
        except MCPHubError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc

        # 5. Transition on outcome: approved only when EVERY requested day
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

    @application.post(
        "/api/periodization/approve",
        response_model=PeriodizationPlanResponse,
        tags=["objectives"],
    )
    async def approve_periodization_endpoint(
        request: Request,
        payload: PeriodizationApprovalRequest,
    ) -> PeriodizationPlanResponse | JSONResponse:
        """Approve the coach-proposed periodization plan and persist it (#182).

        The explicit POST is the approval gate — human-in-the-loop preserved
        (#87 C7): no phase reaches the database without the athlete's
        explicit approval of the inline card. The plan is re-validated at the
        boundary (strict Pydantic — the browser round-trip is as untrusted as
        the agent) and the #167 coverage cross-check runs against the active
        objective's target date before the repository replaces the phase set
        wholesale. Validation failures answer the owner with the structured
        reason body (#181): the threaded ``SettingsValidationError`` (coverage)
        is caught in the endpoint and mapped to its fixed reason, while
        unattributed boundary errors map to the ``invalid_values`` catch-all.

        The athlete-facing GET/PUT ``/api/periodization`` endpoints were
        removed with the manual settings editor (#182 PO pivot): the coach
        proposes, the athlete approves — there is no athlete write path.
        """
        session_factory = request.app.state.db_session_factory
        async with session_factory() as session:
            objective_row = await get_active_objective_row(session)
            if objective_row is None:
                # Phases hang off the active objective; without one the plan
                # has nowhere to live. No #181 reason fits (the plan is not
                # invalid — there is nothing to attach it to), so this stays
                # the generic detail; the UI's unknown-code fallback renders
                # the generic text (fail-safe, no state echo — nLPD).
                raise HTTPException(status_code=422, detail=GENERIC_VALIDATION_DETAIL)
            try:
                validate_plan_coverage(payload.plan, objective_row.target_date)
            except SettingsValidationError as exc:
                # The threaded coverage reason (#181): the typed condition
                # becomes the owner-safe body — no string-matching, no
                # exception text in the response (nLPD).
                logger.warning("Rejected periodization approval: %s", exc)
                return JSONResponse(status_code=422, content=settings_reason_payload(exc.reason))
            objective_id = objective_row.id
            phases = await replace_phases(session, objective_id, payload.plan)
        return PeriodizationPlanResponse(objective_id=objective_id, phases=phases)

    @application.post(
        "/api/debrief",
        response_model=DebriefSaveResponse,
        response_model_exclude_none=True,
        status_code=201,
        tags=["debrief"],
    )
    async def post_debrief_endpoint(
        request: Request,
        payload: DebriefSaveRequest,
    ) -> DebriefSaveResponse | JSONResponse:
        """Persist one post-session debrief with C2-encrypted notes (#189 AC1).

        The Fernet cipher is resolved from ``DATA_ENCRYPTION_KEY`` BEFORE any
        persistence: a missing or invalid key answers 503 with the fixed
        ``{code, message}`` vocabulary (#189 AC2) and nothing is written —
        there is no plaintext fallback. The notes are encrypted inside the
        repository call, so no plaintext bytes ever reach a database file
        (AC1); the 201 confirmation carries metadata only — the notes are
        never echoed (nLPD). The debrief row is scoped to the request's
        athlete (AC4): derived from the verified session email on auth-enabled
        lanes, the single local athlete otherwise. When the athlete consented
        (``mirror_to_icu``), the saved debrief is mirrored to Intervals.icu as
        a session comment (#190 AC3) — non-blocking: a mirror failure keeps
        the 201 and rides the fixed soft warning in the response body.
        """
        settings: Settings = request.app.state.settings
        try:
            cipher = build_debrief_cipher(settings)
        except EncryptionUnavailableError as exc:
            # Fail-safe (#189 AC2): the typed reason becomes the fixed 503
            # body — no exception text, no key material, nothing persisted.
            logger.warning("Debrief save unavailable: %s", exc)
            return JSONResponse(status_code=503, content=encryption_reason_payload(exc.reason))
        athlete_id = _athlete_id_for_session(settings, request.cookies.get(SESSION_COOKIE))
        session_factory = request.app.state.db_session_factory
        try:
            async with session_factory() as session:
                row = await save_debrief(
                    session, athlete_id=athlete_id, payload=payload, cipher=cipher
                )
        except (OSError, SQLAlchemyError) as exc:
            logger.warning("Debrief persistence failed: %s", exc)
            raise HTTPException(status_code=503, detail=GENERIC_PERSISTENCE_DETAIL) from exc
        # ICU session-comment mirror (#190 AC3): strictly opt-in and
        # NON-BLOCKING on the save — the debrief row is already durable when
        # the mirror starts, and every mirror failure degrades to the fixed
        # soft warning while the 201 stands. The hook is injectable
        # (app.state.debrief_mirror) so tests fake it with zero network.
        mirror: DebriefMirrorOutcome | None = None
        if payload.mirror_to_icu:
            mirror_hook = getattr(request.app.state, "debrief_mirror", _default_debrief_mirror)
            try:
                mirror = await mirror_hook(
                    settings,
                    plan_session_key=payload.plan_session_key,
                    rpe=payload.rpe,
                    notes=payload.notes,
                )
            except Exception:  # noqa: BLE001 - the mirror must never break the save (AC3)
                logger.warning("Debrief ICU mirror hook raised — degrading to the soft warning")
                mirror = DebriefMirrorOutcome(
                    status="failed",
                    code=MirrorReason.POST_FAILED,
                    message=MIRROR_REASON_MESSAGES[MirrorReason.POST_FAILED],
                )
        return DebriefSaveResponse(
            status="saved", plan_session_key=row.plan_session_key, rpe=row.rpe, mirror=mirror
        )

    @application.get(
        "/api/debrief/pending",
        response_model=PendingDebriefResponse,
        tags=["debrief"],
    )
    async def get_pending_debriefs_endpoint(request: Request) -> PendingDebriefResponse:
        """List the athlete's completed plan sessions awaiting a debrief (#189 AC3).

        The derivation is per-athlete (AC4) and metadata-only (MADR-0010
        privacy posture: date + title, never notes or RPE). A persistence
        failure degrades to an empty list — the pending-debrief banner is a
        passive reminder and must never break the page (MADR-0010 latency
        contract: degrade to silence, never to a slow or failing page).
        """
        settings: Settings = request.app.state.settings
        athlete_id = _athlete_id_for_session(settings, request.cookies.get(SESSION_COOKIE))
        session_factory = request.app.state.db_session_factory
        try:
            async with session_factory() as session:
                pending = await list_pending_debriefs(
                    session, athlete_id=athlete_id, today=date.today()
                )
        except (OSError, SQLAlchemyError) as exc:
            logger.warning("Pending debrief derivation failed: %s", exc)
            pending = []
        return PendingDebriefResponse(pending=pending)

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
