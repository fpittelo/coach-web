"""Swiss minimalist CSS base layer — EPFL-inspired.

Injects static CSS via st.html() (sandboxed iframe, no unsafe_allow_html).
All CSS is static — no user-controlled content (XSS-safe).
"""

import streamlit as st

from coach_web.theme import (
    ACCENT,
    ACCENT_DARK,
    BG_PRIMARY,
    BG_SECONDARY,
    BORDER,
    BORDER_RADIUS,
    FONT_FAMILY,
    TEXT_PRIMARY,
    TEXT_SECONDARY,
)

_SWISS_CSS = f"""
/* === Swiss Minimalist Base Layer (EPFL-inspired) === */

/* Typography scale */
h1 {{
    font-size: 2.5rem !important;
    font-weight: 700 !important;
    color: {TEXT_PRIMARY} !important;
}}

h2 {{
    font-size: 2rem !important;
    font-weight: 600 !important;
    color: {TEXT_PRIMARY} !important;
}}

h3 {{
    font-size: 1.5rem !important;
    font-weight: 600 !important;
    color: {TEXT_PRIMARY} !important;
}}

p {{
    font-size: 1.25rem !important;
    color: {TEXT_PRIMARY} !important;
    line-height: 1.6 !important;
}}

.caption {{
    font-size: 0.875rem !important;
    color: {TEXT_SECONDARY} !important;
}}

/* Font family */
* {{
    font-family: "{FONT_FAMILY}", sans-serif !important;
}}

/* Border radius — EPFL signature 2px */
.stButton > button,
.stTextInput > div > input,
.stTextArea > div > textarea,
.stSelectbox > div > div,
.stExpander,
.stMetric,
[data-testid="stMetricContainer"] {{
    border-radius: {BORDER_RADIUS} !important;
}}

/* Hairline borders — EPFL gray-200 */
.stExpander {{
    border: 1px solid {BORDER} !important;
}}

/* EPFL uses structure, not shadows — no shadow declarations */

/* Background colors */
.stApp {{
    background-color: {BG_PRIMARY} !important;
}}

/* Secondary background for cards */
[data-testid="stMetricContainer"] {{
    background-color: {BG_SECONDARY} !important;
    padding: 1.5rem !important;
}}

/* Link colors */
a {{
    color: {ACCENT} !important;
}}

a:hover {{
    color: {ACCENT_DARK} !important;
}}
"""


def inject_styles() -> None:
    """Inject Swiss minimalist CSS via st.html() (sandboxed, no unsafe_allow_html)."""
    st.html(f"<style>{_SWISS_CSS}</style>")
