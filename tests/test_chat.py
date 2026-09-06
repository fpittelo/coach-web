"""Tests for coach_web.chat."""

from collections.abc import Iterator
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from coach_web.chat import render_chat, send_message
from coach_web.mcp_client import MCPConnectionError


class MockSessionState:
    """Mock Streamlit session_state with attribute and item access."""

    def __init__(self, initial: dict[str, object] | None = None) -> None:
        self._data: dict[str, object] = initial or {}

    def __contains__(self, key: str) -> bool:
        return key in self._data

    def __setitem__(self, key: str, value: object) -> None:
        self._data[key] = value

    def __getitem__(self, key: str) -> object:
        return self._data[key]

    def __iter__(self) -> Iterator[object]:
        return iter(self._data.values())

    def __getattr__(self, key: str) -> object:
        try:
            return self._data[key]
        except KeyError as exc:
            raise AttributeError(key) from exc

    def __setattr__(self, key: str, value: object) -> None:
        if key == "_data":
            super().__setattr__(key, value)
        else:
            self._data[key] = value


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

    async def test_returns_default_on_empty_content(self, settings: object) -> None:
        """send_message returns default message when MCP result has no text content."""
        mock_client = MagicMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)
        mock_client.call_tool = AsyncMock(return_value=MagicMock(content=[]))

        with patch("coach_web.chat.MCPClient", return_value=mock_client):
            response = await send_message("Hi coach")

        assert isinstance(response, str)
        assert len(response) > 0

    async def test_returns_default_on_no_content_attr(self, settings: object) -> None:
        """send_message returns default when result has no content attribute."""
        mock_client = MagicMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)
        mock_result = MagicMock(spec=[])
        mock_client.call_tool = AsyncMock(return_value=mock_result)

        with patch("coach_web.chat.MCPClient", return_value=mock_client):
            response = await send_message("Hi coach")

        assert isinstance(response, str)
        assert len(response) > 0


class TestRenderChat:
    """Chat rendering and session state logic."""

    @patch("coach_web.chat.st")
    def test_initializes_session_state(self, mock_st: MagicMock) -> None:
        """render_chat initializes messages in session state if not present."""
        mock_st.session_state = MockSessionState()
        mock_st.chat_input.return_value = None
        mock_st.button.return_value = False

        render_chat()

        assert "messages" in mock_st.session_state
        assert mock_st.session_state.messages == []
        mock_st.title.assert_called_once()

    @patch("coach_web.chat.st")
    def test_renders_existing_messages(self, mock_st: MagicMock) -> None:
        """render_chat renders all existing messages from session state."""
        mock_st.session_state = MockSessionState(
            {
                "messages": [
                    {"role": "user", "content": "Hello"},
                    {"role": "assistant", "content": "Hi there!"},
                ]
            }
        )
        mock_st.chat_input.return_value = None
        mock_st.button.return_value = False

        render_chat()

        assert mock_st.chat_message.call_count >= 2

    @patch("coach_web.chat.send_message")
    @patch("coach_web.chat.st")
    def test_sends_message_on_input(self, mock_st: MagicMock, mock_send: MagicMock) -> None:
        """render_chat sends message when user types in chat_input."""

        async def fake_send(message: str) -> str:
            return "Your FTP is 250W"

        mock_send.side_effect = fake_send
        mock_st.session_state = MockSessionState({"messages": []})
        mock_st.chat_input.return_value = "What's my FTP?"
        mock_st.button.return_value = False
        mock_st.chat_message.return_value.__enter__ = MagicMock(return_value=MagicMock())
        mock_st.chat_message.return_value.__exit__ = MagicMock(return_value=None)

        render_chat()

        mock_send.assert_called_once_with("What's my FTP?")

    @patch("coach_web.chat.st")
    def test_clear_chat_button_resets_state(self, mock_st: MagicMock) -> None:
        """Clear chat button resets session state messages."""
        mock_st.session_state = MockSessionState(
            {"messages": [{"role": "user", "content": "test"}]}
        )
        mock_st.chat_input.return_value = None
        mock_st.button.return_value = True

        render_chat()

        assert mock_st.session_state.messages == []

    @patch("coach_web.chat.send_message", side_effect=MCPConnectionError("refused"))
    @patch("coach_web.chat.st")
    def test_handles_mcp_connection_error(self, mock_st: MagicMock, mock_send: MagicMock) -> None:
        """render_chat handles MCPConnectionError gracefully."""
        mock_st.session_state = MockSessionState({"messages": []})
        mock_st.chat_input.return_value = "Test message"
        mock_st.button.return_value = False
        mock_st.chat_message.return_value.__enter__ = MagicMock(return_value=MagicMock())
        mock_st.chat_message.return_value.__exit__ = MagicMock(return_value=None)

        render_chat()

        # Verify st.write was called with the error message
        assert mock_st.write.called
