"""Tests for coach_web.chat."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from coach_web.chat import send_message
from coach_web.mcp_client import MCPConnectionError


class TestSendMessage:
    """Chat message sending logic."""

    async def test_returns_string_response(self, settings: object) -> None:
        """send_message returns a string response."""
        mock_client = MagicMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)
        mock_client.call_tool = AsyncMock(
            return_value=MagicMock(content=[MagicMock(type="text", text="Hello, rider!")])
        )

        with patch("coach_web.chat.MCPClient", return_value=mock_client):
            response = await send_message("Hi coach")

        assert isinstance(response, str)
        assert "coach" in response.lower() or "rider" in response.lower()
        mock_client.call_tool.assert_awaited_once()

    async def test_raises_on_connection_failure(self, settings: object) -> None:
        """send_message propagates MCPConnectionError."""
        mock_client = MagicMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)
        mock_client.call_tool = AsyncMock(side_effect=MCPConnectionError("refused"))

        with patch("coach_web.chat.MCPClient", return_value=mock_client):
            with pytest.raises(MCPConnectionError):
                await send_message("Hi coach")
