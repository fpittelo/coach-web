"""Docs contract: docs/security.md reflects the implemented control state.

Issue #84 ships the CSP middleware (STRIDE #87 conditions C2/C5) and
refreshes the B9 boundary text: sanitized markdown rendering merged in #81,
so the "PLANNED / not yet implemented" framing is retired and the C1–C7
conditions are recorded as implemented-and-verified. These tests pin the
refresh so the document cannot silently regress to describing shipped
controls as plans (and vice versa: a planned control must never read as
shipped).
"""

import re
from pathlib import Path

DOCS_DIR = Path(__file__).resolve().parent.parent / "docs"
SECURITY_DOC = DOCS_DIR / "security.md"

# The exact minimal policy decided in the STRIDE #87 sign-off (conditions
# C2/C5) and shipped as middleware in #84.
CSP_POLICY = (
    "default-src 'self'; script-src 'self' 'unsafe-eval'; style-src 'self'; "
    "img-src 'self' data:; font-src 'self'; connect-src 'self'; object-src 'none'; "
    "base-uri 'self'; frame-ancestors 'none'; form-action 'self'"
)


class TestB9ImplementedState:
    """B9 records the merged #81 state, not a plan."""

    def _doc(self) -> str:
        return SECURITY_DOC.read_text(encoding="utf-8")

    def test_b9_heading_marks_the_boundary_implemented(self) -> None:
        """The B9 heading says IMPLEMENTED and anchors the merged #81."""
        match = re.search(r"### Boundary B9[^\n]*", self._doc())

        assert match is not None, "B9 boundary section missing"
        assert "IMPLEMENTED" in match.group(0)
        assert "#81" in match.group(0)

    def test_planned_framing_is_retired(self) -> None:
        """No 'PLANNED / not yet implemented' framing survives the refresh."""
        doc = self._doc()

        assert "not yet implemented" not in doc
        assert "PLANNED" not in doc
        assert "planned, #81" not in doc

    def test_c1_to_c7_conditions_are_retained(self) -> None:
        """The C1–C7 condition references stay, now implemented-and-verified."""
        doc = self._doc()

        for condition in ("C1", "C2", "C3", "C4", "C5", "C6", "C7"):
            assert condition in doc, condition


class TestC6C7ImplementedState:
    """B10 records the #87 replayed-history conditions as implemented (#148).

    The #87 sign-off gated Surface 3 (replayed history, #79) on two
    conditions: C6 — ``ChatMessage.role`` must remain
    ``Literal["user", "assistant"]`` (never widen to ``system``/``tool``) —
    and C7 — the history entry/length caps and the human approval gate must
    remain; any future server-side persistence requires a new STRIDE review.
    Both are verified in code but were not pinned in ``docs/security.md``;
    these tests lock the IMPLEMENTED records with their evidence anchors.
    """

    def _doc(self) -> str:
        return SECURITY_DOC.read_text(encoding="utf-8")

    def _b10_section(self) -> str:
        """Return the B10 boundary section of the security doc."""
        doc = self._doc()
        start = doc.index("### Boundary B10")
        end = doc.index("### ", start + 1)
        return doc[start:end]

    def test_b10_records_c6_as_implemented_with_evidence(self) -> None:
        """C6 (role Literal) is recorded IMPLEMENTED with its code anchor."""
        b10 = self._b10_section()

        assert "C6" in b10
        assert "IMPLEMENTED" in b10
        assert 'Literal["user", "assistant"]' in b10
        assert "models.py" in b10
        assert "test_models.py" in b10

    def test_b10_records_c7_as_implemented_with_evidence(self) -> None:
        """C7 (caps + human approval gate) is recorded IMPLEMENTED."""
        b10 = self._b10_section()

        assert "C7" in b10
        assert "IMPLEMENTED" in b10
        assert "AgentStreamRequest" in b10
        assert "approval gate" in b10
        assert "STRIDE review" in b10
        assert "test_plan_approval.py" in b10


class TestCspPostureShipped:
    """The CSP posture section documents the shipped middleware (#84)."""

    def _doc(self) -> str:
        return SECURITY_DOC.read_text(encoding="utf-8")

    def test_csp_section_names_the_middleware(self) -> None:
        """The section anchors the shipped middleware and the C2/C5 conditions."""
        doc = self._doc()

        assert "ContentSecurityPolicyMiddleware" in doc
        assert "C2" in doc
        assert "C5" in doc
        assert "#84" in doc

    def test_csp_section_carries_the_exact_policy(self) -> None:
        """The documented header equals the policy decided in #87 verbatim."""
        assert CSP_POLICY in self._doc()

    def test_no_csp_header_claim_is_retired(self) -> None:
        """The retired 'no CSP header today' claim is gone."""
        assert "ships **no CSP header** today" not in self._doc()
