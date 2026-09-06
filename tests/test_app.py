"""Smoke tests for coach_web.app."""

from unittest.mock import MagicMock, patch


class TestMain:
    """Smoke tests for the Streamlit app entry point."""

    @patch("coach_web.app.inject_fonts")
    @patch("coach_web.app.render_chat")
    @patch("coach_web.app.render_plans")
    @patch("coach_web.app.render_dashboard")
    @patch("coach_web.app.st")
    def test_main_initializes_page_config(
        self,
        mock_st: MagicMock,
        mock_render_dashboard: MagicMock,
        mock_render_plans: MagicMock,
        mock_render_chat: MagicMock,
        mock_inject_fonts: MagicMock,
    ) -> None:
        """main() calls st.set_page_config with correct title, icon, and layout."""
        mock_tabs: list[MagicMock] = [MagicMock(), MagicMock(), MagicMock()]
        mock_st.tabs.return_value = mock_tabs

        from coach_web.app import main

        main()

        mock_st.set_page_config.assert_called_once_with(
            page_title="Coach Web",
            page_icon="🚴",
            layout="wide",
        )
        mock_inject_fonts.assert_called_once()
        mock_render_dashboard.assert_called_once()
        mock_render_plans.assert_called_once()
        mock_render_chat.assert_called_once()

    @patch("coach_web.app.inject_fonts")
    @patch("coach_web.app.render_chat")
    @patch("coach_web.app.render_plans")
    @patch("coach_web.app.render_dashboard")
    @patch("coach_web.app.st")
    def test_main_creates_three_tabs(
        self,
        mock_st: MagicMock,
        mock_render_dashboard: MagicMock,
        mock_render_plans: MagicMock,
        mock_render_chat: MagicMock,
        mock_inject_fonts: MagicMock,
    ) -> None:
        """main() calls st.tabs with the three expected tab labels."""
        mock_tabs: list[MagicMock] = [MagicMock(), MagicMock(), MagicMock()]
        mock_st.tabs.return_value = mock_tabs

        from coach_web.app import main

        main()

        mock_inject_fonts.assert_called_once()
        mock_st.tabs.assert_called_once_with(["📊 Dashboard", "📋 Plans", "💬 Chat"])

    @patch("coach_web.app.inject_fonts")
    @patch("coach_web.app.render_chat")
    @patch("coach_web.app.render_plans")
    @patch("coach_web.app.render_dashboard")
    @patch("coach_web.app.st")
    def test_main_renders_each_tab_in_context(
        self,
        mock_st: MagicMock,
        mock_render_dashboard: MagicMock,
        mock_render_plans: MagicMock,
        mock_render_chat: MagicMock,
        mock_inject_fonts: MagicMock,
    ) -> None:
        """main() renders dashboard, plans, and chat inside their tab contexts."""
        dashboard_tab = MagicMock()
        plans_tab = MagicMock()
        chat_tab = MagicMock()
        mock_st.tabs.return_value = [dashboard_tab, plans_tab, chat_tab]

        from coach_web.app import main

        main()

        mock_inject_fonts.assert_called_once()
        dashboard_tab.__enter__.assert_called_once()
        plans_tab.__enter__.assert_called_once()
        chat_tab.__enter__.assert_called_once()
        dashboard_tab.__exit__.assert_called_once()
        plans_tab.__exit__.assert_called_once()
        chat_tab.__exit__.assert_called_once()
        mock_render_dashboard.assert_called_once()
        mock_render_plans.assert_called_once()
        mock_render_chat.assert_called_once()
