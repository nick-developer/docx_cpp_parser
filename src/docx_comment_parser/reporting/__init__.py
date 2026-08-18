"""Shareable review reports.

Two output formats, both driven by the same aggregates:

``export_html_report``
    A single self-contained HTML file — charts, filters, search and a thread
    explorer, with no external requests — for sending to someone who does not
    have Python.  Needs the ``report`` extra (Jinja2).

``export_markdown_report``
    A Markdown digest that pastes into a pull request, a ticket or a chat.
    Standard library only; works on a bare install.

The numbers behind both come from :mod:`.analytics`, which is importable on its
own if you want the aggregates without any rendering::

    from docx_comment_parser.reporting import build_report_data

    data = build_report_data(parser.to_comments())
    print(data.overview.unresolved, "still open")

Importing this package never imports Jinja2 — that happens inside
``render_html_report``, so the core package keeps its zero-dependency install.
"""

from __future__ import annotations

from .analytics import (
    AuthorStat,
    Overview,
    ReportData,
    Thread,
    TimelineBucket,
    build_report_data,
)
from .markdown_report import export_markdown_report, render_markdown_report
from .report_builder import build_payload, export_html_report, render_html_report

__all__ = [
    # HTML
    "render_html_report",
    "export_html_report",
    "build_payload",
    # Markdown
    "render_markdown_report",
    "export_markdown_report",
    # analytics
    "build_report_data",
    "ReportData",
    "Overview",
    "AuthorStat",
    "TimelineBucket",
    "Thread",
]
