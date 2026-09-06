"""Inter font injection — Swiss nLPD compliant local bundling.

Fonts are bundled locally in .streamlit/static/fonts/ to avoid
external CDN requests (Swiss nLPD data minimization).
Download Inter WOFF2 from: https://github.com/rsms/inter/releases
"""

import streamlit as st

# Font definitions: (name, weight, filename)
INTER_FONTS: list[tuple[str, int, str]] = [
    ("Inter", 400, "Inter-Regular.woff2"),
    ("Inter", 500, "Inter-Medium.woff2"),
    ("Inter", 600, "Inter-Semibold.woff2"),
    ("Inter", 700, "Inter-Bold.woff2"),
]

_FONT_FACE_CSS_TEMPLATE = """
@font-face {{
    font-family: "Inter";
    font-weight: {weight};
    font-style: normal;
    font-display: swap;
    src: url("./fonts/{filename}") format("woff2");
}}
"""


def _build_font_css() -> str:
    """Build @font-face CSS for all Inter font weights."""
    css_parts = [
        _FONT_FACE_CSS_TEMPLATE.format(weight=weight, filename=filename)
        for _, weight, filename in INTER_FONTS
    ]
    css_parts.append("/* Apply Inter to all elements */")
    css_parts.append('* { font-family: "Inter", sans-serif !important; }')
    return "\n".join(css_parts)


def inject_fonts() -> None:
    """Inject Inter @font-face CSS via st.html() (sandboxed, no unsafe_allow_html)."""
    css = _build_font_css()
    st.html(f"<style>{css}</style>")  # type: ignore[attr-defined]
