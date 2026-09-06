"""Tests for the Swiss minimalist Plotly template."""

import plotly.graph_objects as go
import plotly.io as pio

from coach_web.plotly_theme import (
    SWISS_MINIMAL_TEMPLATE_NAME,
    create_swiss_minimal_template,
    register_template,
)


def test_template_name() -> None:
    """Template name is 'swiss_minimal'."""
    assert SWISS_MINIMAL_TEMPLATE_NAME == "swiss_minimal"


def test_template_is_go_template() -> None:
    """create_swiss_minimal_template returns a go.layout.Template."""
    template = create_swiss_minimal_template()
    assert isinstance(template, go.layout.Template)


def test_template_has_white_bg() -> None:
    """Template uses white background."""
    template = create_swiss_minimal_template()
    assert template.layout.paper_bgcolor == "#FFFFFF"
    assert template.layout.plot_bgcolor == "#FFFFFF"


def test_template_has_inter_font() -> None:
    """Template uses Inter font family."""
    template = create_swiss_minimal_template()
    assert template.layout.font.family == "Inter"


def test_template_font_color_and_size() -> None:
    """Template uses #212121 font color and size 14."""
    template = create_swiss_minimal_template()
    assert template.layout.font.color == "#212121"
    assert template.layout.font.size == 14


def test_template_has_gray_grid() -> None:
    """Template uses #D5D5D5 grid color."""
    template = create_swiss_minimal_template()
    # Access xaxis or yaxis gridcolor
    assert template.layout.xaxis.gridcolor == "#D5D5D5"
    assert template.layout.yaxis.gridcolor == "#D5D5D5"
    assert template.layout.xaxis.zerolinecolor == "#D5D5D5"
    assert template.layout.yaxis.zerolinecolor == "#D5D5D5"


def test_template_has_red_colorway() -> None:
    """Template colorway starts with #FF0000."""
    template = create_swiss_minimal_template()
    assert template.layout.colorway[0] == "#FF0000"


def test_template_colorway() -> None:
    """Template colorway matches the Swiss palette."""
    template = create_swiss_minimal_template()
    assert list(template.layout.colorway) == ["#FF0000", "#707070", "#B51F1F", "#D5D5D5"]


def test_template_margins() -> None:
    """Template uses the expected margins."""
    template = create_swiss_minimal_template()
    assert template.layout.margin.l == 40
    assert template.layout.margin.r == 20
    assert template.layout.margin.t == 40
    assert template.layout.margin.b == 40


def test_template_hoverlabel() -> None:
    """Template hoverlabel uses light gray bg and dark text."""
    template = create_swiss_minimal_template()
    assert template.layout.hoverlabel.bgcolor == "#F5F5F5"
    assert template.layout.hoverlabel.font.color == "#212121"


def test_register_template() -> None:
    """register_template adds the swiss_minimal template to plotly.io.templates."""
    register_template()
    assert "swiss_minimal" in pio.templates
    assert isinstance(pio.templates["swiss_minimal"], go.layout.Template)
