"""Build the self-contained interactive HTML review report.

The output is one file with no external references at all — the stylesheet, the
script, the charts and the comment data are inlined — so it opens from a file://
URL, from an email attachment, or from a machine with no network, and it keeps
working years after the toolchain that produced it is gone.

Rendering uses Jinja2, which lives behind the ``report`` extra.  Everything the
template *consumes* is computed by :mod:`.analytics` and :mod:`.charts`, both of
which are standard-library only, so the Markdown report and the tests can reuse
the same figures without Jinja2 present.

Scale strategy
--------------
A 5,000-comment document cannot be rendered as 5,000 table rows: the browser
would spend seconds on layout before showing anything.  Instead the comments are
embedded once as compact JSON and the page renders a single page of results at a
time, filtering client-side.  What Jinja2 renders is therefore small and roughly
constant no matter how large the document is — the aggregates, the charts, and
the reviewer table.
"""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Dict, Iterable, List, Optional, Sequence, Union

from ..models import Comment
from . import charts
from .analytics import ReportData, build_report_data

if TYPE_CHECKING:  # pragma: no cover - typing only
    import jinja2

__all__ = ["render_html_report", "export_html_report", "build_payload"]

PathLike = Union[str, "Path"]

_HERE = Path(__file__).parent
_TEMPLATE_DIR = _HERE / "templates"
_ASSET_DIR = _HERE / "assets"
_TEMPLATE_NAME = "report.html.j2"

#: Above this many daily buckets the per-week chart is the more readable default.
#: Roughly a quarter of a year — beyond that the daily columns get too thin to
#: compare by eye, though both charts are always available from the toggle.
_DAILY_DEFAULT_LIMIT = 92


def _require_jinja2() -> Any:
    """Import Jinja2, or raise an error that says exactly how to install it.

    Mirrors the pandas/polars behaviour in the export layer: a missing optional
    dependency produces an instruction, never a bare ``ModuleNotFoundError``.
    """
    try:
        import jinja2
    except ImportError as exc:  # pragma: no cover - depends on environment
        raise ImportError(
            "jinja2 is required for HTML reports but is not installed.\n"
            "Install it with:  pip install docx-comment-parser[report]"
        ) from exc
    return jinja2


def _asset(name: str) -> str:
    """Read one inlined asset from the package."""
    return (_ASSET_DIR / name).read_text(encoding="utf-8")


def _script_safe_json(payload: Dict[str, Any]) -> str:
    """Serialise the payload for embedding inside a ``<script>`` element.

    ``</script>`` appearing inside a comment would otherwise close the block
    early and inject the rest of the document as markup, so the three characters
    that can start a tag or an HTML comment are escaped to their ``\\u`` form.
    That is still valid JSON and parses back identically.  U+2028/U+2029 are
    escaped too: they are legal in JSON but are line terminators to a JavaScript
    parser.
    """
    raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    return (
        raw.replace("<", "\\u003c")
        .replace(">", "\\u003e")
        .replace("&", "\\u0026")
        .replace(" ", "\\u2028")
        .replace(" ", "\\u2029")
    )


def _display_date(comment: Comment) -> str:
    """Readable timestamp, falling back to the raw XML value if unparseable."""
    if comment.date_parsed is not None:
        return comment.date_parsed.strftime("%Y-%m-%d %H:%M")
    return comment.date


def build_payload(comments: Sequence[Comment]) -> Dict[str, Any]:
    """Pack comments into the compact structure the page's JavaScript reads.

    Authors and document names are interned into lookup tables and each comment
    becomes a positional array rather than an object.  On a 5,000-comment report
    that removes several hundred kilobytes of repeated key and author strings —
    the difference between a report that emails cleanly and one that does not.
    """
    authors: Dict[str, int] = {}
    documents: Dict[str, int] = {}
    rows: List[List[Any]] = []

    for comment in comments:
        author_index = authors.setdefault(comment.author, len(authors))
        document_index = documents.setdefault(comment.document_name, len(documents))
        rows.append([
            comment.comment_id,
            author_index,
            _display_date(comment),
            comment.date_parsed.isoformat() if comment.date_parsed else "",
            comment.text,
            comment.referenced_text,
            1 if comment.resolved else 0,
            1 if comment.is_reply else 0,
            comment.thread_depth,
            comment.root_id,
            document_index,
        ])

    return {
        "authors": list(authors),
        "documents": list(documents),
        "rows": rows,
    }


def _environment(template: Optional[PathLike]) -> "jinja2.Environment":
    """Build the Jinja2 environment, optionally rooted at a custom template."""
    jinja2 = _require_jinja2()

    search: List[str] = [str(_TEMPLATE_DIR)]
    if template is not None:
        # A custom template is loaded from its own directory *and* the built-in
        # one, so it can `{% extends %}` or `{% include %}` the shipped report.
        search.insert(0, str(Path(template).resolve().parent))

    return jinja2.Environment(
        loader=jinja2.FileSystemLoader(search),
        autoescape=jinja2.select_autoescape(
            enabled_extensions=("html", "j2", "html.j2"), default=True
        ),
        trim_blocks=True,
        lstrip_blocks=True,
    )


def render_html_report(
    comments: Iterable[Comment],
    title: Optional[str] = None,
    generated_at: Optional[datetime] = None,
    template: Optional[PathLike] = None,
) -> str:
    """Render the interactive HTML report and return it as a string.

    Parameters
    ----------
    comments:
        Rows from ``parser.to_comments()``, or any filtered subset.
    title:
        Overrides the heading, which otherwise names the source document(s).
    generated_at:
        Timestamp shown in the header.  Defaults to now (UTC); pass a fixed
        value for byte-for-byte reproducible output.
    template:
        Path to a custom Jinja2 template.  It receives the same context as the
        built-in one: ``data``, ``payload``, ``css``, ``js``, ``daily_chart``,
        ``weekly_chart``, ``default_scale`` and ``version``.

    Requires the ``report`` extra: ``pip install docx-comment-parser[report]``.
    """
    from .. import __version__

    rows = list(comments)
    data: ReportData = build_report_data(rows, generated_at=generated_at)

    if title is not None:
        # Both aggregates are frozen dataclasses, so swap in a new one rather
        # than mutating.
        data = replace(data, overview=replace(data.overview, title=title))

    default_scale = "daily" if len(data.daily) <= _DAILY_DEFAULT_LIMIT else "weekly"

    environment = _environment(template)
    name = Path(template).name if template is not None else _TEMPLATE_NAME

    # Jinja2's stubs are skipped under mypy's follow_imports=skip, so render()
    # is typed as Any; it always returns str.
    html: str = environment.get_template(name).render(
        data=data,
        payload=_script_safe_json(build_payload(rows)),
        css=_asset("report.css"),
        js=_asset("report.js"),
        daily_chart=charts.column_chart(
            data.daily, "chart-daily", "Comments per day, split by resolved and open."
        ),
        weekly_chart=charts.column_chart(
            data.weekly, "chart-weekly", "Comments per week, split by resolved and open."
        ),
        default_scale=default_scale,
        version=__version__,
    )
    return html


def export_html_report(
    comments: Iterable[Comment],
    path: PathLike,
    title: Optional[str] = None,
    generated_at: Optional[datetime] = None,
    template: Optional[PathLike] = None,
) -> "Path":
    """Write the interactive HTML report and return the path written.

    Parent directories are created if missing.
    """
    target = Path(path)
    if target.parent and not target.parent.exists():
        target.parent.mkdir(parents=True, exist_ok=True)

    html = render_html_report(
        comments, title=title, generated_at=generated_at, template=template
    )
    target.write_text(html, encoding="utf-8")
    return target
