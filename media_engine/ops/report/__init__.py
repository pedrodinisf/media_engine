"""``report.*`` ops — render typed artifacts to MarkdownArtifact via Jinja2.

Phase 5 commit 37 ships two: ``report.session`` (one SessionAnalysis ->
one MarkdownArtifact) and ``report.zeitgeist`` (variadic list of
SessionAnalysis -> one aggregate MarkdownArtifact).
"""

from .session import ReportSession, SessionReportParams
from .zeitgeist import ReportZeitgeist, ZeitgeistReportParams

__all__ = [
    "ReportSession",
    "ReportZeitgeist",
    "SessionReportParams",
    "ZeitgeistReportParams",
]
