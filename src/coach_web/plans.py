"""Training plans tab rendering and data fetching."""

import asyncio
from datetime import date

import httpx
import streamlit as st

from coach_web.config import get_settings
from coach_web.github_client import GitHubClient
from coach_web.models import TrainingPlan


@st.cache_data(ttl=300, show_spinner=False)
def _get_cached_plans_sync() -> list[TrainingPlan]:
    """Fetch training plans with Streamlit caching (5-minute TTL)."""
    return asyncio.run(get_training_plans())


def _get_current_week_id() -> str:
    """Return the current ISO week identifier (YYYY-WNN)."""
    today = date.today()
    iso = today.isocalendar()
    return f"{iso.year}-W{iso.week:02d}"


def render_plans() -> None:
    """Render the weekly training plans tab with caching and current-week highlighting."""
    st.title("Weekly Training Plans")
    st.markdown("Past 5 weeks + current week from the Coach GitHub repository.")

    try:
        plans = _get_cached_plans_sync()
    except httpx.HTTPStatusError:
        st.warning("Configure GITHUB_TOKEN to load training plans.")
        return
    except httpx.ConnectError:
        st.error("Unable to connect to GitHub. Check your network connection.")
        return
    except Exception:  # noqa: BLE001
        st.error("Unable to fetch training plans. Check your configuration.")
        return

    if not plans:
        st.info(
            "No training plans found. Create issues with label `agent::coach` in your repository."
        )
        return

    current_week = _get_current_week_id()

    for plan in plans:
        is_current = plan.week_id == current_week
        label = f"{plan.title} ({plan.week_id})"

        with st.expander(label, expanded=is_current):
            st.markdown(plan.body)
            st.caption(f"Created: {plan.created_at}")
            if is_current:
                st.markdown("*(Current week)*")


async def get_training_plans() -> list[TrainingPlan]:
    """Fetch training plans from the configured GitHub repository."""
    settings = get_settings()
    async with GitHubClient(settings.GITHUB_TOKEN, settings.GITHUB_REPO) as client:
        return await client.fetch_training_plans()
