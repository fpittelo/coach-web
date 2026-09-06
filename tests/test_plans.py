"""Tests for coach_web.plans."""

import re
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from coach_web.models import TrainingPlan
from coach_web.plans import (
    _get_current_week_id,
    get_training_plans,
    render_plans,
)


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


class TestRenderPlans:
    """Streamlit rendering behaviour for the plans tab."""

    @patch("coach_web.plans.st")
    @patch("coach_web.plans._get_current_week_id", return_value="2026-W37")
    @patch("coach_web.plans._get_cached_plans_sync")
    def test_render_with_plans(
        self,
        mock_get: MagicMock,
        mock_week: MagicMock,
        mock_st: MagicMock,
        sample_training_plans: list[TrainingPlan],
    ) -> None:
        """render_plans displays an expander for each training plan."""
        mock_get.return_value = sample_training_plans
        mock_st.expander.return_value.__enter__ = MagicMock(return_value=MagicMock())
        mock_st.expander.return_value.__exit__ = MagicMock(return_value=None)

        render_plans()

        assert mock_st.title.call_args[0][0] == "Weekly Training Plans"
        assert mock_st.expander.call_count == len(sample_training_plans)
        assert mock_st.markdown.called
        mock_week.assert_called_once()

    @patch("coach_web.plans.st")
    @patch("coach_web.plans._get_cached_plans_sync", return_value=[])
    def test_render_empty_plans(self, mock_get: MagicMock, mock_st: MagicMock) -> None:
        """render_plans shows an info message when no plans are found."""
        render_plans()

        mock_st.info.assert_called_once()
        mock_st.expander.assert_not_called()

    @patch("coach_web.plans.st")
    @patch(
        "coach_web.plans._get_cached_plans_sync",
        side_effect=httpx.HTTPStatusError(
            "Unauthorized",
            request=MagicMock(),
            response=MagicMock(status_code=401),
        ),
    )
    def test_render_on_http_error(self, mock_get: MagicMock, mock_st: MagicMock) -> None:
        """render_plans warns on HTTP status errors."""
        render_plans()

        mock_st.warning.assert_called_once()
        mock_st.error.assert_not_called()

    @patch("coach_web.plans.st")
    @patch(
        "coach_web.plans._get_cached_plans_sync",
        side_effect=httpx.ConnectError("Connection refused"),
    )
    def test_render_on_connect_error(self, mock_get: MagicMock, mock_st: MagicMock) -> None:
        """render_plans errors on connection failures."""
        render_plans()

        mock_st.error.assert_called_once()
        mock_st.warning.assert_not_called()


class TestGetCurrentWeekId:
    """Current ISO week identifier helper."""

    def test_returns_valid_iso_week(self) -> None:
        """_get_current_week_id returns a YYYY-WNN formatted string."""
        week_id = _get_current_week_id()

        assert re.match(r"^\d{4}-W\d{2}$", week_id)

    def test_format_is_string(self) -> None:
        """_get_current_week_id returns a string."""
        week_id = _get_current_week_id()

        assert isinstance(week_id, str)
