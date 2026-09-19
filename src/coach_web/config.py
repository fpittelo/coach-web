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

    COACH_MCP_URL: str = "http://localhost:8000/mcp"
    """Coach MCP server endpoint."""

    GITHUB_TOKEN: str = ""
    """GitHub PAT for training plan issues (empty default for local dev)."""

    GITHUB_REPO: str = "fpittelo/coach"
    """Repository containing training plan issues."""

    CACHE_TTL_SECONDS: int = 60
    """Default cache TTL for fetched data."""

    LOG_LEVEL: str = "INFO"
    """Logging level."""

    APP_HOST: str = "0.0.0.0"
    """Host the ASGI server binds to."""

    APP_PORT: int = 8080
    """Port the ASGI server binds to."""

    CORS_ORIGINS: list[str] = Field(default_factory=lambda: ["http://localhost:8080"])
    """Allowed browser origins for cross-origin requests."""

    @property
    def service_name(self) -> str:
        """Canonical service identifier reported by the healthcheck."""
        return "coach-web"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the cached Settings singleton."""
    return Settings()
