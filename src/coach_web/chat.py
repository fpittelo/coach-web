"""Chat tab rendering and message sending."""

import asyncio

import streamlit as st

from coach_web.config import get_settings
from coach_web.mcp_client import MCPClient, MCPConnectionError


def render_chat() -> None:
    """Render the chat interface tab with session state and error handling."""
    st.title("Chat with Your Coach")
    st.markdown("Ask your coach about training, readiness, or today's workout.")

    # Initialize session state
    if "messages" not in st.session_state:
        st.session_state.messages = []

    # Render existing messages
    for message in st.session_state.messages:
        with st.chat_message(message["role"]):
            st.write(message["content"])

    # Chat input
    if prompt := st.chat_input("Type a message..."):
        # Append and render user message
        st.session_state.messages.append({"role": "user", "content": prompt})
        with st.chat_message("user"):
            st.write(prompt)

        # Send to MCP and render response
        try:
            response = asyncio.run(send_message(prompt))
        except MCPConnectionError:
            response = (
                "Coach MCP server is not available. "
                "Please start your MCP server and try again."
            )
        except Exception:  # noqa: BLE001
            response = (
                "Unable to reach the Coach MCP server. Please check your configuration."
            )

        st.session_state.messages.append({"role": "assistant", "content": response})
        with st.chat_message("assistant"):
            st.write(response)

    # Clear chat button
    if st.button("Clear chat"):
        st.session_state.messages = []
        st.rerun()


async def send_message(message: str) -> str:
    """Send a message to the Coach MCP server and return the response."""
    settings = get_settings()
    async with MCPClient(settings.COACH_MCP_URL) as client:
        result = await client.call_tool(
            "coach_chat",
            {"message": message},
        )
        # Extract first text content from MCP result
        if hasattr(result, "content") and result.content:
            for item in result.content:
                if getattr(item, "type", None) == "text":
                    return str(item.text)
        return "I'm here! How can I help with your training today?"
