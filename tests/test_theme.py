"""Tests for the Swiss minimalist design token system."""

import tomllib
from pathlib import Path

from coach_web import theme


def test_theme_module_imports() -> None:
    """Verify the theme module exports expected constants."""
    assert theme.BG_PRIMARY is not None
    assert theme.BG_SECONDARY is not None
    assert theme.TEXT_PRIMARY is not None
    assert theme.TEXT_SECONDARY is not None
    assert theme.ACCENT is not None
    assert theme.ACCENT_DARK is not None
    assert theme.BORDER is not None
    assert theme.FONT_FAMILY is not None
    assert theme.BORDER_RADIUS is not None


def test_design_tokens_exist() -> None:
    """Verify all seven color token constants exist with correct EPFL values."""
    assert theme.BG_PRIMARY == "#FFFFFF"
    assert theme.BG_SECONDARY == "#F5F5F5"
    assert theme.TEXT_PRIMARY == "#212121"
    assert theme.TEXT_SECONDARY == "#707070"
    assert theme.ACCENT == "#FF0000"
    assert theme.ACCENT_DARK == "#B51F1F"
    assert theme.BORDER == "#D5D5D5"


def _repo_root() -> Path:
    """Return the repository root directory."""
    return Path(__file__).resolve().parent.parent


def test_config_toml_exists() -> None:
    """Verify the Streamlit config.toml file exists in the repository."""
    config_path = _repo_root() / ".streamlit" / "config.toml"
    assert config_path.exists()
    assert config_path.is_file()


def test_config_toml_theme_section() -> None:
    """Verify config.toml contains the expected [theme] section values."""
    config_path = _repo_root() / ".streamlit" / "config.toml"
    config_text = config_path.read_text(encoding="utf-8")
    assert "[theme]" in config_text
    assert 'base = "light"' in config_text
    assert 'primaryColor = "#FF0000"' in config_text
    assert 'backgroundColor = "#FFFFFF"' in config_text
    assert 'secondaryBackgroundColor = "#F5F5F5"' in config_text
    assert 'textColor = "#212121"' in config_text
    assert 'font = "sans serif"' in config_text


def test_theme_tokens_match_config() -> None:
    """Verify Python constants are the single source of truth for config.toml values."""
    config_path = _repo_root() / ".streamlit" / "config.toml"
    with config_path.open("rb") as config_file:
        parsed = tomllib.load(config_file)

    theme_section = parsed["theme"]
    assert theme_section["base"] == "light"
    assert theme_section["primaryColor"] == theme.ACCENT
    assert theme_section["backgroundColor"] == theme.BG_PRIMARY
    assert theme_section["secondaryBackgroundColor"] == theme.BG_SECONDARY
    assert theme_section["textColor"] == theme.TEXT_PRIMARY
    assert theme_section["font"] == "sans serif"
