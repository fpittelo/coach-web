"""Async MCP client wrapper for the Coach MCP server."""

import json
from types import TracebackType
from typing import Any

from mcp.client.session import ClientSession
from mcp.client.streamable_http import streamable_http_client

from coach_web.models import ReadinessMetrics


class MCPConnectionError(Exception):
    """Raised when the MCP server cannot be reached."""


class MCPClient:
    """Async client for interacting with a Coach MCP server."""

    def __init__(self, url: str) -> None:
        """Store the MCP server URL."""
        self.url = url
        self._session: ClientSession | None = None
        self._transport: Any | None = None

    async def connect(self) -> None:
        """Establish a streamable_http connection to the MCP server."""
        try:
            self._transport = streamable_http_client(self.url)
            read_stream, write_stream = await self._transport.__aenter__()
            self._session = ClientSession(read_stream, write_stream)
            await self._session.initialize()
        except Exception as exc:
            await self.close()
            raise MCPConnectionError(f"Failed to connect to MCP server at {self.url}") from exc

    async def call_tool(
        self,
        tool_name: str,
        arguments: dict[str, Any] | None = None,
    ) -> Any:
        """Call an MCP tool by name."""
        if self._session is None:
            raise MCPConnectionError("MCP client is not connected")
        return await self._session.call_tool(tool_name, arguments)

    async def close(self) -> None:
        """Close the MCP transport."""
        if self._transport is not None:
            await self._transport.__aexit__(None, None, None)
            self._transport = None
        self._session = None

    async def get_readiness_dashboard(self) -> ReadinessMetrics:
        """Fetch readiness metrics from the MCP server."""
        result = await self.call_tool("intervals_get_readiness_dashboard")
        text = self._extract_text(result)
        data = json.loads(text)
        return ReadinessMetrics(**data)

    @staticmethod
    def _extract_text(result: Any) -> str:
        """Extract the first text content from an MCP tool result."""
        if hasattr(result, "content") and result.content:
            for item in result.content:
                if getattr(item, "type", None) == "text":
                    return str(item.text)
        raise MCPConnectionError("Unexpected MCP tool result format")

    async def __aenter__(self) -> "MCPClient":
        """Enter the async context manager."""
        await self.connect()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> None:
        """Exit the async context manager."""
        await self.close()
