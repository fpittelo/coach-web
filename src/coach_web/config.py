"""Application configuration via Pydantic Settings."""

from functools import lru_cache

from pydantic import Field
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

    OPENROUTER_MODEL: str = "anthropic/claude-3.5-sonnet"
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
    """Google OAuth web client ID (Cloud Run injects GOOGLE_OIDC_CLIENT_ID, #66)."""

    GOOGLE_OIDC_CLIENT_SECRET: str = ""
    """Google OAuth client secret (env var only; never logged or persisted)."""

    GOOGLE_OIDC_ISSUER: str = "https://accounts.google.com"
    """Expected issuer of Google ID tokens (aligns with the Cloud Run spec, #66)."""

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

    @property
    def service_name(self) -> str:
        """Canonical service identifier reported by the healthcheck."""
        return "coach-web"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the cached Settings singleton."""
    return Settings()
