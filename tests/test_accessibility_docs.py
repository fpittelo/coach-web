"""Docs contract: docs/accessibility.md records the AT verification state.

Issue #149 AC1: actual screen-reader verification of the ``role="log"`` /
``role="status"`` live-region posture requires human AT execution — it
cannot be automated by the source-assertion suite. The doc carries the
exact procedure and must stay marked PENDING EXECUTION (pending-PO) until
the PO runs it; these tests pin that state so the document can neither
silently claim an execution that never happened nor lose the procedure.
"""

from pathlib import Path

DOCS_DIR = Path(__file__).resolve().parent.parent / "docs"
ACCESSIBILITY_DOC = DOCS_DIR / "accessibility.md"


class TestAtVerificationProcedureDocumented:
    """#149 AC1: the procedure exists and is honestly marked pending."""

    def _doc(self) -> str:
        return ACCESSIBILITY_DOC.read_text(encoding="utf-8")

    def test_at_verification_section_exists_with_tool_matrix(self) -> None:
        """The procedure names the AT matrix and the exact live-region checks."""
        doc = self._doc()

        assert "AT verification procedure" in doc
        for tool in ("NVDA", "Firefox", "VoiceOver", "Safari"):
            assert tool in doc, tool
        for check in ('role="status"', 'role="log"', "exactly once"):
            assert check in doc, check

    def test_verification_is_marked_pending_not_executed(self) -> None:
        """PENDING EXECUTION (pending-PO): no fabricated execution claim."""
        doc = self._doc()

        assert "**Status: PENDING EXECUTION (pending-PO).**" in doc
        assert "Status: EXECUTED" not in doc
        assert "Status: PASSED" not in doc

    def test_outcome_is_routed_to_the_issue_closing_comment(self) -> None:
        """AC1: the recorded outcome belongs in the #149 closing comment."""
        doc = self._doc()

        assert "closing comment" in doc
