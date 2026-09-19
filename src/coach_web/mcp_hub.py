"""Dual MCP client hub aggregating the coach-mcp and github-mcp tool servers.

The hub owns one :class:`~coach_web.mcp_client.MCPClient` per upstream server,
merges their tool catalogues into a single OpenAI/OpenRouter-compatible schema
list, and routes tool executions back to the owning server. Failures are
degraded gracefully: a single unreachable server never takes the hub down, and
every runtime error is surfaced to the agent loop as a structured JSON string.
"""

import asyncio
import json
import logging
from types import TracebackType
from typing import Any

from mcp.types import Tool

from coach_web.config import Settings
from coach_web.mcp_client import MCPClient, MCPConnectionError

logger = logging.getLogger(__name__)

COACH_SERVER = "coach-mcp"
"""Logical name of the Intervals.icu gateway server."""

GITHUB_SERVER = "github-mcp"
"""Logical name of the GitHub gateway server."""

NAMESPACE_SEPARATOR = "__"
"""Separator used to namespace tool names as ``<server><sep><tool>``."""

COACH_DEFAULT_ARGUMENTS: dict[str, Any] = {"response_format": "json"}
"""Arguments injected into coach-mcp tool calls when not explicitly provided."""


class MCPHubError(Exception):
    """Raised when the hub cannot establish a connection to any MCP server."""


class MCPClientHub:
    """Manage concurrent MCP sessions for coach-mcp and github-mcp."""

    def __init__(
        self,
        coach_url: str,
        github_url: str,
        *,
        github_token: str | None = None,
        call_timeout: float = 30.0,
    ) -> None:
        """Create the hub and its per-server clients."""
        self.coach_url = coach_url
        self.github_url = github_url
        self.call_timeout = call_timeout
        self._clients: dict[str, MCPClient] = {
            COACH_SERVER: MCPClient(coach_url),
            GITHUB_SERVER: MCPClient(github_url, auth_token=github_token),
        }
        self._connected: set[str] = set()
        self._connection_errors: dict[str, str] = {}
        self._tools: dict[str, Tool] = {}
        self._tool_servers: dict[str, str] = {}
        self._raw_to_namespaced: dict[str, str] = {}

    @classmethod
    def from_settings(cls, settings: Settings) -> "MCPClientHub":
        """Build a hub from application settings."""
        return cls(
            settings.COACH_MCP_URL,
            settings.GITHUB_MCP_URL,
            github_token=settings.GITHUB_TOKEN,
        )

    @property
    def connected_servers(self) -> set[str]:
        """Return the set of currently connected server names."""
        return set(self._connected)

    @property
    def connection_errors(self) -> dict[str, str]:
        """Return the last connection error per unreachable server."""
        return dict(self._connection_errors)

    async def connect(self) -> None:
        """Connect to every configured server, tolerating partial failures.

        Raises:
            MCPHubError: if no server could be reached at all.
        """
        self._connection_errors.clear()
        for name, client in self._clients.items():
            try:
                await client.connect()
            except MCPConnectionError as exc:
                self._connection_errors[name] = str(exc)
                logger.warning("MCP server %s unavailable: %s", name, exc)
            else:
                self._connected.add(name)

        if not self._connected:
            details = "; ".join(f"{name}: {err}" for name, err in self._connection_errors.items())
            raise MCPHubError(f"Failed to connect to any MCP server: {details}")

        await self._refresh_tools()

    async def reconnect(self) -> None:
        """Tear down and re-establish every server session."""
        await self.close()
        await self.connect()

    async def close(self) -> None:
        """Close every client session and clear the tool registry."""
        for name, client in reversed(list(self._clients.items())):
            try:
                await client.close()
            except Exception as exc:  # noqa: BLE001 - shutdown must never raise
                logger.warning("Error closing MCP client %s: %s", name, exc)
        self._connected.clear()
        self._tools.clear()
        self._tool_servers.clear()
        self._raw_to_namespaced.clear()

    async def _refresh_tools(self) -> None:
        """Rebuild the merged tool registry from the connected servers."""
        self._tools.clear()
        self._tool_servers.clear()
        self._raw_to_namespaced.clear()

        raw_servers: dict[str, str] = {}
        duplicates: set[str] = set()

        for name in sorted(self._connected):
            client = self._clients[name]
            try:
                tools = await client.list_tools()
            except Exception as exc:  # noqa: BLE001 - one bad server must not break the hub
                logger.warning("Failed to list tools for %s: %s", name, exc)
                continue

            for tool in tools:
                namespaced = f"{name}{NAMESPACE_SEPARATOR}{tool.name}"
                self._tools[namespaced] = tool
                self._tool_servers[namespaced] = name
                if tool.name in raw_servers:
                    duplicates.add(tool.name)
                else:
                    raw_servers[tool.name] = namespaced

        self._raw_to_namespaced = {
            raw: namespaced for raw, namespaced in raw_servers.items() if raw not in duplicates
        }

    def list_all_tools(self) -> list[dict[str, Any]]:
        """Return every registered tool as an OpenAI/OpenRouter function schema."""
        schemas: list[dict[str, Any]] = []
        for namespaced, tool in self._tools.items():
            schemas.append(
                {
                    "type": "function",
                    "function": {
                        "name": namespaced,
                        "description": tool.description or "",
                        "parameters": tool.input_schema or {"type": "object", "properties": {}},
                    },
                }
            )
        return schemas

    async def execute_tool(
        self,
        name: str,
        arguments: dict[str, Any] | None = None,
    ) -> str:
        """Route a tool call to its owning server and return a JSON string.

        The returned string is always valid JSON: either the upstream tool
        payload or an ``{"error": ...}`` object describing a graceful failure.
        """
        namespaced = self._resolve_tool(name)
        if namespaced is None:
            return self._error(f"Unknown tool: {name}")

        server = self._tool_servers[namespaced]
        if server not in self._connected:
            return self._error(f"MCP server '{server}' is not connected")

        raw_name = namespaced.split(NAMESPACE_SEPARATOR, 1)[1]
        args: dict[str, Any] = dict(arguments) if arguments else {}
        if server == COACH_SERVER:
            for key, value in COACH_DEFAULT_ARGUMENTS.items():
                args.setdefault(key, value)

        try:
            result = await asyncio.wait_for(
                self._clients[server].call_tool(raw_name, args),
                timeout=self.call_timeout,
            )
        except TimeoutError:
            return self._error(f"Tool '{name}' timed out after {self.call_timeout}s")
        except MCPConnectionError as exc:
            return self._error(f"Tool '{name}' failed: {exc}")
        except Exception as exc:  # noqa: BLE001 - surface every failure to the agent loop
            return self._error(f"Tool '{name}' failed: {exc}")

        return self._serialize(result)

    def _resolve_tool(self, name: str) -> str | None:
        """Resolve a namespaced or unique raw tool name to its registry key."""
        if name in self._tool_servers:
            return name
        return self._raw_to_namespaced.get(name)

    @classmethod
    def _serialize(cls, result: Any) -> str:
        """Serialise an MCP tool result into a JSON string."""
        structured = getattr(result, "structured_content", None)
        if isinstance(structured, dict) and structured:
            return json.dumps(structured)

        text = cls._extract_text(result)
        if getattr(result, "is_error", False):
            return cls._error(text or "Unknown MCP tool error")
        return text or "{}"

    @staticmethod
    def _extract_text(result: Any) -> str:
        """Extract the first text content block from an MCP tool result."""
        content = getattr(result, "content", None) or []
        for item in content:
            if getattr(item, "type", None) == "text":
                return str(item.text)
        return ""

    @staticmethod
    def _error(message: str) -> str:
        """Build a JSON error payload."""
        return json.dumps({"error": message})

    async def __aenter__(self) -> "MCPClientHub":
        """Connect and return the hub."""
        await self.connect()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> None:
        """Close every session on context exit."""
        await self.close()
