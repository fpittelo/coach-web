"""Async GitHub API client for fetching training plan issues."""

import re
from types import TracebackType
from typing import Any

import httpx

from coach_web.models import TrainingPlan


class GitHubClient:
    """Async client for fetching training plan issues from GitHub."""

    _WEEK_ID_RE: re.Pattern[str] = re.compile(r"(\d{4}-W\d{2})")
    _BASE_URL: str = "https://api.github.com"

    def __init__(self, token: str, repo: str = "fpittelo/coach") -> None:
        """Store the GitHub token and target repository."""
        self.token = token
        self.repo = repo
        self._client = httpx.AsyncClient(
            base_url=self._BASE_URL,
            headers={
                "Authorization": f"token {token}",
                "Accept": "application/vnd.github+json",
            },
            timeout=30.0,
        )

    async def fetch_training_plans(self, max_weeks: int = 6) -> list[TrainingPlan]:
        """Fetch recent training plan issues from the configured repository."""
        response = await self._client.get(
            f"/repos/{self.repo}/issues",
            params={
                "labels": "agent::coach,type::task",
                "state": "open",
                "sort": "created",
                "direction": "desc",
                "per_page": "100",
            },
        )
        response.raise_for_status()

        issues: list[dict[str, Any]] = response.json()
        plans = [
            self._issue_to_plan(issue)
            for issue in issues
            if str(issue.get("title", "")).startswith("Training Plan: ")
        ]
        plans.sort(key=lambda plan: plan.created_at, reverse=True)
        return plans[:max_weeks]

    @classmethod
    def _extract_week_id(cls, title: str) -> str:
        """Extract the ISO week identifier from a training plan title."""
        match = cls._WEEK_ID_RE.search(title)
        if not match:
            raise ValueError(f"No ISO week identifier found in title: {title!r}")
        return match.group(1)

    @classmethod
    def _issue_to_plan(cls, issue: dict[str, Any]) -> TrainingPlan:
        """Convert a GitHub issue JSON object into a TrainingPlan model."""
        title = str(issue.get("title", ""))
        week_id = cls._extract_week_id(title)
        return TrainingPlan(
            number=int(issue.get("number", 0)),
            title=title,
            body=str(issue.get("body", "") or ""),
            created_at=str(issue.get("created_at", "")),
            week_id=week_id,
        )

    async def close(self) -> None:
        """Close the underlying HTTP client."""
        await self._client.aclose()

    async def __aenter__(self) -> "GitHubClient":
        """Enter the async context manager."""
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> None:
        """Exit the async context manager."""
        await self.close()
