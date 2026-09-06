"""Streamlit entry point for Coach Web."""

import streamlit as st

from coach_web.chat import render_chat
from coach_web.dashboard import render_dashboard
from coach_web.fonts import inject_fonts
from coach_web.plans import render_plans


def main() -> None:
    """Run the Coach Web Streamlit application."""
    st.set_page_config(
        page_title="Coach Web",
        page_icon="🚴",
        layout="wide",
    )

    inject_fonts()

    dashboard_tab, plans_tab, chat_tab = st.tabs(["📊 Dashboard", "📋 Plans", "💬 Chat"])

    with dashboard_tab:
        render_dashboard()

    with plans_tab:
        render_plans()

    with chat_tab:
        render_chat()


if __name__ == "__main__":
    main()
