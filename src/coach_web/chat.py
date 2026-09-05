"""Chat tab rendering and message sending."""

import asyncio

import streamlit as st

from coach_web.config import get_settings
from coach_web.mcp_client import MCPClient


def render_chat() -> None:
    """Render the chat interface tab."""
    st.title("💬 Chat with Your Coach")

    if "messages" not in st.session_state:
        st.session_state.messages = []

    for message in st.session_state.messages:
        with st.chat_message(message["role"]):
            st.write(message["content"])

    if prompt := st.chat_input("Type a message..."):
        st.session_state.messages.append({"role": "user", "content": prompt})
        with st.chat_message("user"):
            st.write(prompt)

        response = asyncio.run(send_message(prompt))
        st.session_state.messages.append({"role": "assistant", "content": response})
        with st.chat_message("assistant"):
            st.write(response)

    if st.button("Clear chat"):
        st.session_state.messages = []
        st.rerun()


async def send_message(message: str) -> str:
    """Send a message to the Coach MCP server and return a placeholder response."""
    settings = get_settings()
    async with MCPClient(settings.COACH_MCP_URL) as client:
        result = await client.call_tool(
            "coach_chat",
            {"message": message},
        )
        for item in result.content:
            if getattr(item, "type", None) == "text":
                return str(item.text)
        return "🚴 Coach response placeholder"
