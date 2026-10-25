"""Application configuration via Pydantic Settings."""

from functools import lru_cache

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Coach Web runtime settings loaded from environment variables."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_prefix="",
        extra="ignore",
    )

    COACH_MCP_URL: str = "http://coach-mcp:8000/sse"
    """Coach MCP server endpoint (Docker service discovery on coach-net)."""

    GITHUB_MCP_URL: str = "http://github-mcp:8001/"
    """GitHub MCP server endpoint (streamable HTTP on coach-net)."""

    GITHUB_TOKEN: str = ""
    """GitHub PAT for training plan issues (empty default for local dev)."""

    GITHUB_REPO: str = "fpittelo/coach"
    """Repository containing training plan issues."""

    GITHUB_PLAN_BRANCH: str = "main"
    """Branch receiving approved Markdown training plans."""

    GITHUB_PLAN_DIR: str = "plans"
    """Directory inside the repository holding approved Markdown plans."""

    OPENROUTER_API_KEY: str = ""
    """OpenRouter API key (env var only; never logged or persisted)."""

    OPENROUTER_BASE_URL: str = "https://openrouter.ai/api/v1"
    """OpenRouter API base URL."""

    OPENROUTER_MODEL: str = "anthropic/claude-sonnet-4.5"
    """Default model routed through OpenRouter for the coach agent."""

    OPENROUTER_TIMEOUT_SECONDS: float = 60.0
    """Timeout for a single OpenRouter completion request."""

    OPENROUTER_APP_TITLE: str = "Coach Web"
    """X-Title attribution header sent to OpenRouter."""

    OPENROUTER_REFERER: str = "https://github.com/fpittelo/coach-web"
    """HTTP-Referer attribution header sent to OpenRouter."""

    AGENT_MAX_TOOL_ITERATIONS: int = 8
    """Hard cap on agent tool-calling iterations per user turn."""

    CACHE_TTL_SECONDS: int = 60
    """Default cache TTL for fetched data."""

    LOG_LEVEL: str = "INFO"
    """Logging level."""

    APP_HOST: str = "0.0.0.0"
    """Host the ASGI server binds to."""

    APP_PORT: int = 8000
    """Port the ASGI server binds to."""

    CORS_ORIGINS: list[str] = Field(default_factory=lambda: ["http://localhost:8000"])
    """Allowed browser origins for cross-origin requests."""

    AUTH_ENABLED: bool = False
    """Enable Google OIDC authentication & whitelist middleware (opt-in, #65)."""

    GOOGLE_OIDC_CLIENT_ID: str = ""
    """Google OAuth web client ID (lane env files wire GOOGLE_OIDC_CLIENT_ID, #66)."""

    GOOGLE_OIDC_CLIENT_SECRET: str = ""
    """Google OAuth client secret (env var only; never logged or persisted)."""

    GOOGLE_OIDC_ISSUER: str = "https://accounts.google.com"
    """Expected issuer of Google ID tokens (Google accounts issuer, #66)."""

    AUTH_WHITELIST_EMAILS: list[str] = Field(
        default_factory=lambda: ["frederic.pitteloud@gmail.com"]
    )
    """Application-level access-control whitelist — THE security boundary (#65)."""

    AUTH_SESSION_SECRET: str = ""
    """HS256 signing key for stateless session tokens (env var only; never logged)."""

    AUTH_SESSION_TTL_SECONDS: int = 3600
    """Stateless session token lifetime in seconds (short-lived by design)."""

    AUTH_REDIRECT_URI: str = ""
    """Explicit OAuth redirect URI; derived from the request base URL when empty."""

    AUTH_COOKIE_SECURE: bool = True
    """Set the Secure flag on session & OIDC-state cookies (default true).

    Chromium >=89 and Firefox >=75 treat ``http://localhost`` as a trustworthy
    origin and DO send Secure cookies over plain-HTTP loopback; Safari has no
    localhost exception and drops them. Only set false for a plain-HTTP
    non-loopback deployment — never in the local loopback topology.
    """

    TRUSTED_HOSTS: list[str] = Field(default_factory=lambda: ["localhost", "127.0.0.1"])
    """Host header allowlist enforced by TrustedHostMiddleware (DNS rebinding)."""

    COACH_DB_PATH: str = "/data/coach.db"
    """SQLite database file (MADR-008, #166).

    Defaults inside the container to the lane volume mountpoint /data; the
    WAL side files live alongside. Override per lane via the env files only
    when a different volume path is chosen — no secret material.
    """

    DATA_ENCRYPTION_KEY: SecretStr = SecretStr("")
    """Fernet key encrypting C2 health-data fields at rest (MADR-0008, #189).

    A 32-byte urlsafe-base64 Fernet key, distinct per lane (same discipline
    as ``AUTH_SESSION_SECRET``: env var only, ``chmod 600`` env files, never
    logged, never committed). Loaded as :class:`pydantic.SecretStr` per
    security condition C5 — ``str()``/``repr()`` of the settings mask it.
    Missing or invalid key → debrief saves answer 503 with the fixed
    ``{code, message}`` vocabulary (#189); there is NO plaintext fallback.
    """

    CONVERSATION_MESSAGE_RETENTION_DAYS: int = 90
    """Raw conversation messages older than this are purged (MADR-0025 C6, #348).

    Bounded retention is a privacy feature: raw turns age out of every
    thread (the stored summary layer is what survives). Configurable per
    lane; the ``maintenance purge-conversations`` sweep consumes it.
    """

    CONVERSATION_THREAD_RETENTION_DAYS: int = 365
    """Threads inactive longer than this are purged (MADR-0025 C6, #348).

    "Inactive" covers both shapes: archived threads (``last_active_at``
    freezes at archive time) and active-but-stale threads. The FK CASCADE
    removes the thread's remaining messages with it. Configurable per
    lane; the ``maintenance purge-conversations`` sweep consumes it.
    """

    MEMORY_CONTEXT_MAX_CHARS: int = 200_000
    """Total character bound on the assembled conversation-memory context (#350 AC5).

    MADR-0025: "total assembled context bounded by a config constant,
    enforced server-side (worst-case-tested)". The documented worst case —
    20 × 8 000-char window messages + 250-word summary + fences + directive
    + orientation block + digests + re-assert clause — measures ≈ 172 000
    chars, so the default leaves headroom. The context builder
    (``conversation_context.build_memory_block``) enforces the bound:
    beyond it the memory window truncates OLDEST-FIRST (a contiguous
    newest run is kept) and the summary section yields last.
    """

    @property
    def service_name(self) -> str:
        """Canonical service identifier reported by the healthcheck."""
        return "coach-web"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the cached Settings singleton."""
    return Settings()
