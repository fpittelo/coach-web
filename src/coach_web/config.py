"""Application configuration via Pydantic Settings."""

from functools import lru_cache

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

    STREAMLIT_SERVER_PORT: int = 8501
    """Port the Streamlit server binds to."""

    STREAMLIT_SERVER_ADDRESS: str = "0.0.0.0"
    """Address the Streamlit server binds to."""

    CACHE_TTL_SECONDS: int = 60
    """Default cache TTL for fetched data."""

    LOG_LEVEL: str = "INFO"
    """Logging level."""


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the cached Settings singleton."""
    return Settings()
