"""Dashboard tab rendering and data fetching."""

import asyncio

import plotly.graph_objects as go
import streamlit as st

from coach_web.config import get_settings
from coach_web.mcp_client import MCPClient, MCPConnectionError
from coach_web.models import FitnessTrend, ReadinessMetrics


def render_dashboard() -> None:
    """Render the readiness dashboard tab with metric cards and trend charts."""
    st.title("📊 Readiness Cockpit")
    st.markdown("Daily readiness metrics from Intervals.icu via the Coach MCP server.")

    try:
        metrics = asyncio.run(get_readiness_metrics())
    except MCPConnectionError:
        st.warning("⚠️ Connect your Coach MCP server to see your readiness data.")
        return
    except Exception:  # noqa: BLE001
        st.error("⚠️ Unable to fetch readiness data. Check your MCP server configuration.")
        return

    # --- Metric cards (7 metrics in a 4-column grid) ---
    col1, col2, col3, col4 = st.columns(4)
    with col1:
        st.metric("FTP", f"{metrics.ftp} W", delta=None)
    with col2:
        st.metric("Resting HR", f"{metrics.resting_hr} bpm")
    with col3:
        st.metric("HRV (rMSSD)", f"{metrics.hrv_rmssd:.1f} ms")
    with col4:
        st.metric("Sleep", f"{metrics.sleep_hours:.1f} h")

    col5, col6, col7, col8 = st.columns(4)
    with col5:
        st.metric("CTL (Fitness)", f"{metrics.ctl:.1f}")
    with col6:
        st.metric("ATL (Fatigue)", f"{metrics.atl:.1f}")
    with col7:
        tsb_delta = f"{metrics.tsb:+.1f}"
        st.metric("TSB (Form)", f"{metrics.tsb:.1f}", delta=tsb_delta)
    with col8:
        # Ramp rate = today's CTL - 7 days ago CTL (simplified)
        ramp = metrics.atl - metrics.ctl if metrics.atl > metrics.ctl else 0.0
        st.metric("Ramp Rate", f"{ramp:+.1f}/wk")

    st.divider()

    # --- Trend charts ---
    st.subheader("📈 Fitness Trends (42-day)")

    try:
        trend = asyncio.run(get_fitness_trend())
    except (MCPConnectionError, Exception):  # noqa: BLE001
        trend = None

    if trend and trend.points:
        _render_trend_charts(trend)
    else:
        st.info(
            "No fitness trend data available. Start your Coach MCP server with historical data."
        )


async def get_readiness_metrics() -> ReadinessMetrics:
    """Fetch readiness metrics from the configured Coach MCP server."""
    settings = get_settings()
    async with MCPClient(settings.COACH_MCP_URL) as client:
        return await client.get_readiness_dashboard()


async def get_fitness_trend() -> FitnessTrend:
    """Fetch 42-day fitness trend (CTL/ATL/TSB) from the Coach MCP server."""
    settings = get_settings()
    async with MCPClient(settings.COACH_MCP_URL) as client:
        return await client.get_fitness_summary()


def _render_trend_charts(trend: FitnessTrend) -> None:
    """Render 3 Plotly trend charts for CTL, ATL, and TSB."""
    dates = [point.date for point in trend.points]
    ctl_values = [point.ctl for point in trend.points]
    atl_values = [point.atl for point in trend.points]
    tsb_values = [point.tsb for point in trend.points]

    # 42-day Fitness (CTL) line chart
    fig_ctl = go.Figure(
        data=[
            go.Scatter(
                x=dates,
                y=ctl_values,
                mode="lines+markers",
                name="CTL",
                line={"color": "blue"},
            ),
        ],
    )
    fig_ctl.update_layout(
        title="Fitness (CTL) — 42 Day Trend",
        xaxis_title="Date",
        yaxis_title="CTL",
        template="plotly_dark",
    )
    st.plotly_chart(fig_ctl, use_container_width=True)

    # 7-day Fatigue (ATL) line chart
    last_7_dates = dates[-7:]
    last_7_atl = atl_values[-7:]
    fig_atl = go.Figure(
        data=[
            go.Scatter(
                x=last_7_dates,
                y=last_7_atl,
                mode="lines+markers",
                name="ATL",
                line={"color": "red"},
            ),
        ],
    )
    fig_atl.update_layout(
        title="Fatigue (ATL) — 7 Day Trend",
        xaxis_title="Date",
        yaxis_title="ATL",
        template="plotly_dark",
    )
    st.plotly_chart(fig_atl, use_container_width=True)

    # Form Battery (TSB) line chart
    fig_tsb = go.Figure(
        data=[
            go.Scatter(
                x=dates,
                y=tsb_values,
                mode="lines+markers",
                name="TSB",
                line={"color": "green"},
            ),
        ],
    )
    fig_tsb.update_layout(
        title="Form (TSB) — Training Stress Balance",
        xaxis_title="Date",
        yaxis_title="TSB",
        template="plotly_dark",
    )
    st.plotly_chart(fig_tsb, use_container_width=True)
