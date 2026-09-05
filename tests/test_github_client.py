"""Tests for coach_web.github_client."""

import httpx
import pytest
import respx
from httpx import Response

from coach_web.github_client import GitHubClient
from coach_web.models import TrainingPlan


class TestGitHubClient:
    """GitHub API client behaviour."""

    def test_init_stores_token_and_repo(self) -> None:
        """The client stores the token and repository."""
        client = GitHubClient("ghp_test", "fpittelo/coach")

        assert client.token == "ghp_test"  # noqa: S105
        assert client.repo == "fpittelo/coach"

    @respx.mock
    async def test_fetch_training_plans(self) -> None:
        """fetch_training_plans returns parsed TrainingPlan models."""
        route = respx.get("https://api.github.com/repos/fpittelo/coach/issues").mock(
            return_value=Response(
                200,
                json=[
                    {
                        "number": 1,
                        "title": "Training Plan: 2026-W37",
                        "body": "Build phase.",
                        "created_at": "2026-09-01T10:00:00Z",
                        "labels": [{"name": "agent::coach"}, {"name": "type::task"}],
                    },
                    {
                        "number": 2,
                        "title": "Training Plan: 2026-W36",
                        "body": "Base phase.",
                        "created_at": "2026-08-25T10:00:00Z",
                        "labels": [{"name": "agent::coach"}, {"name": "type::task"}],
                    },
                ],
            )
        )

        async with GitHubClient("ghp_test", "fpittelo/coach") as client:
            plans = await client.fetch_training_plans(max_weeks=6)

        assert route.called
        assert len(plans) == 2
        assert all(isinstance(plan, TrainingPlan) for plan in plans)
        assert plans[0].week_id == "2026-W37"

    @respx.mock
    async def test_filters_by_title_prefix(self) -> None:
        """Only issues with the 'Training Plan: ' prefix are returned."""
        respx.get("https://api.github.com/repos/fpittelo/coach/issues").mock(
            return_value=Response(
                200,
                json=[
                    {
                        "number": 1,
                        "title": "Training Plan: 2026-W37",
                        "body": "Build phase.",
                        "created_at": "2026-09-01T10:00:00Z",
                        "labels": [],
                    },
                    {
                        "number": 2,
                        "title": "Bug: something else",
                        "body": "Not a plan.",
                        "created_at": "2026-09-02T10:00:00Z",
                        "labels": [],
                    },
                ],
            )
        )

        async with GitHubClient("ghp_test", "fpittelo/coach") as client:
            plans = await client.fetch_training_plans(max_weeks=6)

        assert len(plans) == 1
        assert plans[0].title == "Training Plan: 2026-W37"

    @respx.mock
    async def test_sorts_descending_and_limits(self) -> None:
        """Plans are sorted by created_at descending and limited to max_weeks."""
        respx.get("https://api.github.com/repos/fpittelo/coach/issues").mock(
            return_value=Response(
                200,
                json=[
                    {
                        "number": 1,
                        "title": "Training Plan: 2026-W37",
                        "body": "Week 37.",
                        "created_at": "2026-09-01T10:00:00Z",
                        "labels": [],
                    },
                    {
                        "number": 2,
                        "title": "Training Plan: 2026-W39",
                        "body": "Week 39.",
                        "created_at": "2026-09-15T10:00:00Z",
                        "labels": [],
                    },
                    {
                        "number": 3,
                        "title": "Training Plan: 2026-W38",
                        "body": "Week 38.",
                        "created_at": "2026-09-08T10:00:00Z",
                        "labels": [],
                    },
                ],
            )
        )

        async with GitHubClient("ghp_test", "fpittelo/coach") as client:
            plans = await client.fetch_training_plans(max_weeks=2)

        assert len(plans) == 2
        assert plans[0].week_id == "2026-W39"
        assert plans[1].week_id == "2026-W38"

    @respx.mock
    async def test_raises_on_http_error(self) -> None:
        """HTTP errors are raised via raise_for_status."""
        respx.get("https://api.github.com/repos/fpittelo/coach/issues").mock(
            return_value=Response(401, json={"message": "Bad credentials"})
        )

        async with GitHubClient("ghp_test", "fpittelo/coach") as client:
            with pytest.raises(httpx.HTTPStatusError):
                await client.fetch_training_plans()

    @respx.mock
    async def test_async_context_manager(self) -> None:
        """The client can be used as an async context manager."""
        respx.get("https://api.github.com/repos/fpittelo/coach/issues").mock(
            return_value=Response(200, json=[])
        )

        async with GitHubClient("ghp_test", "fpittelo/coach") as client:
            assert isinstance(client, GitHubClient)
        assert client.token == "ghp_test"  # noqa: S105

    def test_week_id_extraction(self) -> None:
        """week_id is extracted from the issue title."""
        title = "Training Plan: 2026-W37"
        match = GitHubClient._extract_week_id(title)

        assert match == "2026-W37"
