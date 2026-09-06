"""Tests for Inter local font bundling and CSS injection."""

from pathlib import Path
from unittest.mock import patch

from coach_web.fonts import INTER_FONTS, _build_font_css, inject_fonts


def test_inject_fonts_function_exists() -> None:
    """inject_fonts can be imported and is callable."""
    assert callable(inject_fonts)


def test_inject_fonts_calls_st_html() -> None:
    """inject_fonts calls st.html with CSS containing @font-face."""
    with patch("coach_web.fonts.st.html", return_value=None) as mock_html:
        inject_fonts()
        mock_html.assert_called_once()
        call_args = mock_html.call_args
        assert call_args is not None
        call_str = call_args[0][0]
        assert "<style>" in call_str
        assert "</style>" in call_str


def test_font_files_exist() -> None:
    """All declared Inter WOFF2 placeholder files exist."""
    fonts_dir = Path(".streamlit/static/fonts")
    assert fonts_dir.exists(), f"Fonts directory not found: {fonts_dir}"
    for _, _, filename in INTER_FONTS:
        font_path = fonts_dir / filename
        assert font_path.exists(), f"Missing font file: {font_path}"


def test_fonts_module_has_font_paths() -> None:
    """INTER_FONTS exposes all four Inter weights."""
    assert len(INTER_FONTS) == 4
    weights = {weight for _, weight, _ in INTER_FONTS}
    assert weights == {400, 500, 600, 700}


def test_css_contains_font_face() -> None:
    """Generated CSS contains @font-face for each weight and global font-family."""
    css = _build_font_css()
    for _, weight, filename in INTER_FONTS:
        assert f"font-weight: {weight}" in css
        assert filename in css
    assert "@font-face" in css
    assert 'font-family: "Inter"' in css
    assert '* { font-family: "Inter", sans-serif !important; }' in css
