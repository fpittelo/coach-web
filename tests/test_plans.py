"""Tests for coach_web.plans."""

from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from coach_web.models import TrainingPlan
from coach_web.plans import get_training_plans


class TestGetTrainingPlans:
    """Data-fetching logic for the plans tab."""

    async def test_returns_training_plans(self, settings: object) -> None:
        """get_training_plans returns a list of TrainingPlan models."""
        expected = [
            TrainingPlan(
                number=1,
                title="Training Plan: 2026-W37",
                body="Build phase.",
                created_at="2026-09-01T10:00:00Z",
                week_id="2026-W37",
            )
        ]
        mock_client = MagicMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)
        mock_client.fetch_training_plans = AsyncMock(return_value=expected)

        with patch("coach_web.plans.GitHubClient", return_value=mock_client):
            plans = await get_training_plans()

        assert plans == expected
        mock_client.fetch_training_plans.assert_awaited_once()

    async def test_raises_on_http_error(self, settings: object) -> None:
        """get_training_plans propagates HTTP errors."""
        mock_client = MagicMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)
        mock_client.fetch_training_plans = AsyncMock(
            side_effect=httpx.HTTPStatusError(
                "Unauthorized",
                request=MagicMock(),
                response=MagicMock(status_code=401),
            )
        )

        with patch("coach_web.plans.GitHubClient", return_value=mock_client):
            with pytest.raises(httpx.HTTPStatusError):
                await get_training_plans()
