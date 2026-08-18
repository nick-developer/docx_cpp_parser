"""Markdown review report.

Standard library only, so this one works on a bare ``pip install
docx-comment-parser`` with no extras — the same guarantee CSV and JSON export
give.  It reads the identical aggregates as the HTML report, so the two can
never disagree about how many comments are open.

Markdown is the format that pastes: into a pull request, a Jira ticket, a
Confluence page, or a chat message.  It is also the shape an LLM reads best,
which is why it lands here rather than waiting for the LLM phase.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Iterable, List, Optional, Union

from ..models import Comment
from .analytics import ReportData, Thread, build_report_data

__all__ = ["render_markdown_report", "export_markdown_report"]

PathLike = Union[str, "Path"]

_MAX_THREADS = 200
"""Threads listed in full. Past this the detail section would dwarf the summary,
so it is truncated with an explicit note rather than silently cut."""


def _escape(text: str) -> str:
    """Neutralise the Markdown characters that would corrupt a table cell.

    Pipes end a cell and newlines end a row, so both have to go; the rest of
    Markdown's punctuation is harmless in this context and is left alone so the
    comment still reads naturally.
    """
    return " ".join(text.split()).replace("|", "\\|")


def _clip(text: str, width: int) -> str:
    flat = " ".join(text.split())
    return flat if len(flat) <= width else flat[: width - 1] + "…"


def _when(stamp: Optional[datetime], fallback: str = "—") -> str:
    return stamp.strftime("%Y-%m-%d") if stamp is not None else fallback


def render_markdown_report(
    comments: Iterable[Comment],
    title: Optional[str] = None,
    generated_at: Optional[datetime] = None,
    include_threads: bool = True,
) -> str:
    """Render a Markdown review report and return it as a string.

    Parameters
    ----------
    title:
        Overrides the heading, which otherwise names the source document(s).
    generated_at:
        Timestamp for the footer.  Defaults to now (UTC).
    include_threads:
        Include the full conversation transcript after the summary tables.
        Set False for a summary-only digest.
    """
    data: ReportData = build_report_data(comments, generated_at=generated_at)
    overview = data.overview
    heading = title if title is not None else overview.title

    out: List[str] = [f"# Comment review — {heading}", ""]

    if overview.total_comments == 0:
        out += ["No comments were found in this document.", ""]
        out += _footer(data)
        return "\n".join(out)

    # ── Overview ────────────────────────────────────────────────────────────
    out += [
        "## Overview",
        "",
        "| Metric | Value |",
        "| --- | --- |",
        f"| Total comments | {overview.total_comments:,} |",
        f"| Root comments | {overview.root_comments:,} |",
        f"| Replies | {overview.replies:,} |",
        f"| Conversations | {overview.threads:,} |",
        f"| Resolved | {overview.resolved:,} ({overview.resolution_percent:.0f}%) |",
        f"| Still open | {overview.unresolved:,} |",
        f"| Reviewers | {overview.unique_authors:,} |",
        f"| First comment | {_when(overview.earliest)} |",
        f"| Last comment | {_when(overview.latest)} |",
        "",
    ]

    if len(overview.documents) > 1:
        out += ["**Documents**", ""]
        out += [f"- `{document}`" for document in overview.documents]
        out += [""]

    # ── Reviewers ───────────────────────────────────────────────────────────
    out += [
        "## Reviewers",
        "",
        "| Reviewer | Comments | Resolved | Open | Resolution rate |",
        "| --- | ---: | ---: | ---: | ---: |",
    ]
    for author in data.authors:
        out.append(
            f"| {_escape(author.author) or '—'} | {author.total:,} | "
            f"{author.resolved:,} | {author.unresolved:,} | "
            f"{author.resolution_percent:.0f}% |"
        )
    out.append("")

    # ── Activity ────────────────────────────────────────────────────────────
    active_weeks = [bucket for bucket in data.weekly if bucket.total]
    if active_weeks:
        out += [
            "## Activity by week",
            "",
            "| Week | Comments | Resolved | Open |",
            "| --- | ---: | ---: | ---: |",
        ]
        for bucket in active_weeks:
            out.append(
                f"| {bucket.label} | {bucket.total:,} | "
                f"{bucket.resolved:,} | {bucket.unresolved:,} |"
            )
        out.append("")

    # ── Open items first: this is what a reviewer acts on ───────────────────
    open_threads = [thread for thread in data.threads if not thread.resolved]
    if open_threads:
        out += [f"## Open items ({len(open_threads):,})", ""]
        for thread in open_threads[:_MAX_THREADS]:
            root = thread.root
            out.append(
                f"- **[#{root.comment_id}] {_escape(root.author)}** "
                f"({_when(thread.started)}): {_escape(_clip(root.text, 160))}"
            )
            still_open = sum(1 for c in thread.comments if not c.resolved)
            if thread.size > 1:
                out.append(
                    f"  - {thread.reply_count} repl"
                    f"{'y' if thread.reply_count == 1 else 'ies'}, "
                    f"{still_open} still open"
                )
        if len(open_threads) > _MAX_THREADS:
            out.append(f"- _…and {len(open_threads) - _MAX_THREADS:,} more._")
        out.append("")

    # ── Full transcript ─────────────────────────────────────────────────────
    if include_threads:
        out += [f"## All conversations ({len(data.threads):,})", ""]
        for thread in data.threads[:_MAX_THREADS]:
            out += _render_thread(thread)
        if len(data.threads) > _MAX_THREADS:
            out += [
                f"_Showing the first {_MAX_THREADS:,} of "
                f"{len(data.threads):,} conversations._",
                "",
            ]

    out += _footer(data)
    return "\n".join(out)


def _render_thread(thread: Thread) -> List[str]:
    """One conversation as a nested Markdown list."""
    root = thread.root
    status = "resolved" if thread.resolved else "open"
    lines = [
        f"### #{root.comment_id} — {_escape(root.author)} "
        f"({_when(thread.started)}) · {status}",
        "",
    ]
    if root.referenced_text:
        # Blockquote the anchored document text so it is visually distinct from
        # the comment about it.
        lines += [f"> {_escape(_clip(root.referenced_text, 300))}", ""]

    for comment in thread.comments:
        indent = "  " * min(comment.thread_depth, 4)
        flag = "x" if comment.resolved else " "
        lines.append(
            f"{indent}- [{flag}] **{_escape(comment.author)}** "
            f"({_when(comment.date_parsed, comment.date or '—')}): "
            f"{_escape(comment.text)}"
        )
    lines.append("")
    return lines


def _footer(data: ReportData) -> List[str]:
    from .. import __version__

    stamp = data.overview.generated_at.strftime("%Y-%m-%d %H:%M UTC")
    return [
        "---",
        "",
        f"_Generated by docx-comment-parser {__version__} on {stamp}._",
    ]


def export_markdown_report(
    comments: Iterable[Comment],
    path: PathLike,
    title: Optional[str] = None,
    generated_at: Optional[datetime] = None,
    include_threads: bool = True,
) -> "Path":
    """Write a Markdown review report and return the path written.

    Parent directories are created if missing.
    """
    target = Path(path)
    if target.parent and not target.parent.exists():
        target.parent.mkdir(parents=True, exist_ok=True)

    target.write_text(
        render_markdown_report(
            comments,
            title=title,
            generated_at=generated_at,
            include_threads=include_threads,
        ),
        encoding="utf-8",
    )
    return target
