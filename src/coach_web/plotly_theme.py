"""Swiss minimalist Plotly template — EPFL-inspired.

Custom Plotly template matching the coach-web Swiss minimalist theme.
Uses design tokens from theme.py for consistency.
"""

import plotly.graph_objects as go

from coach_web.theme import (
    ACCENT,
    ACCENT_DARK,
    BG_PRIMARY,
    BORDER,
    FONT_FAMILY,
    TEXT_PRIMARY,
    TEXT_SECONDARY,
)

SWISS_MINIMAL_TEMPLATE_NAME = "swiss_minimal"


def create_swiss_minimal_template() -> go.layout.Template:
    """Create and return the Swiss minimalist Plotly template."""
    template = go.layout.Template(
        layout=go.Layout(
            paper_bgcolor=BG_PRIMARY,
            plot_bgcolor=BG_PRIMARY,
            font=go.layout.Font(
                family=FONT_FAMILY,
                size=14,
                color=TEXT_PRIMARY,
            ),
            colorway=[ACCENT, TEXT_SECONDARY, ACCENT_DARK, BORDER],
            margin=go.layout.Margin(l=40, r=20, t=40, b=40),
            xaxis=go.layout.XAxis(
                gridcolor=BORDER,
                zerolinecolor=BORDER,
                linecolor=BORDER,
            ),
            yaxis=go.layout.YAxis(
                gridcolor=BORDER,
                zerolinecolor=BORDER,
                linecolor=BORDER,
            ),
            hoverlabel=go.layout.Hoverlabel(
                bgcolor="#F5F5F5",
                font=go.layout.hoverlabel.Font(color=TEXT_PRIMARY),
            ),
        ),
    )
    return template


def register_template() -> None:
    """Register the Swiss minimalist template with Plotly."""
    import plotly.io as pio

    pio.templates[SWISS_MINIMAL_TEMPLATE_NAME] = create_swiss_minimal_template()
