"""Training plans tab rendering and data fetching."""

import asyncio

import httpx
import streamlit as st

from coach_web.config import get_settings
from coach_web.github_client import GitHubClient
from coach_web.models import TrainingPlan


def render_plans() -> None:
    """Render the weekly training plans tab."""
    st.title("📋 Weekly Training Plans")

    try:
        plans = asyncio.run(get_training_plans())
    except httpx.HTTPStatusError:
        st.warning("Configure GITHUB_TOKEN to load training plans")
        return

    for plan in plans:
        with st.expander(f"{plan.title} ({plan.week_id})"):
            st.markdown(plan.body)
            st.caption(f"Created: {plan.created_at}")


async def get_training_plans() -> list[TrainingPlan]:
    """Fetch training plans from the configured GitHub repository."""
    settings = get_settings()
    async with GitHubClient(settings.GITHUB_TOKEN, settings.GITHUB_REPO) as client:
        return await client.fetch_training_plans()
