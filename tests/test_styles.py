"""Tests for Swiss minimalist CSS base layer injection."""

from unittest.mock import patch

from coach_web.styles import inject_styles


def test_inject_styles_function_exists() -> None:
    """inject_styles can be imported and is callable."""
    assert callable(inject_styles)


def test_inject_styles_calls_st_html() -> None:
    """inject_styles calls st.html with CSS wrapped in a style tag."""
    with patch("coach_web.styles.st.html", return_value=None) as mock_html:
        inject_styles()
        mock_html.assert_called_once()
        call_args = mock_html.call_args
        assert call_args is not None
        css_str = call_args[0][0]
        assert "<style>" in css_str
        assert "</style>" in css_str


def test_css_contains_border_radius() -> None:
    """CSS includes the EPFL signature 2px border radius."""
    with patch("coach_web.styles.st.html", return_value=None) as mock_html:
        inject_styles()
        css_str = mock_html.call_args[0][0]
        assert "border-radius: 2px" in css_str


def test_css_contains_font_family() -> None:
    """CSS applies the Inter font family globally."""
    with patch("coach_web.styles.st.html", return_value=None) as mock_html:
        inject_styles()
        css_str = mock_html.call_args[0][0]
        assert 'font-family: "Inter"' in css_str


def test_css_contains_no_box_shadow() -> None:
    """CSS does not introduce box shadows (EPFL uses structure, not shadows)."""
    with patch("coach_web.styles.st.html", return_value=None) as mock_html:
        inject_styles()
        css_str = mock_html.call_args[0][0]
        assert "box-shadow" not in css_str


def test_css_contains_hairline_border() -> None:
    """CSS uses the EPFL gray-200 hairline border color."""
    with patch("coach_web.styles.st.html", return_value=None) as mock_html:
        inject_styles()
        css_str = mock_html.call_args[0][0]
        assert "#D5D5D5" in css_str
