"""Dashboard tab rendering and data fetching."""

import asyncio

import streamlit as st

from coach_web.config import get_settings
from coach_web.mcp_client import MCPClient, MCPConnectionError
from coach_web.models import ReadinessMetrics


def render_dashboard() -> None:
    """Render the readiness dashboard tab."""
    st.title("📊 Readiness Cockpit")

    try:
        metrics = asyncio.run(get_readiness_metrics())
    except MCPConnectionError:
        st.warning("Connect your Coach MCP server")
        return

    col1, col2, col3 = st.columns(3)
    with col1:
        st.metric("FTP", f"{metrics.ftp} W")
        st.metric("Resting HR", f"{metrics.resting_hr} bpm")
    with col2:
        st.metric("HRV RMSSD", f"{metrics.hrv_rmssd} ms")
        st.metric("Sleep", f"{metrics.sleep_hours} h")
    with col3:
        st.metric("CTL", f"{metrics.ctl}")
        st.metric("ATL", f"{metrics.atl}")
        st.metric("TSB", f"{metrics.tsb}")


async def get_readiness_metrics() -> ReadinessMetrics:
    """Fetch readiness metrics from the configured Coach MCP server."""
    settings = get_settings()
    async with MCPClient(settings.COACH_MCP_URL) as client:
        return await client.get_readiness_dashboard()
