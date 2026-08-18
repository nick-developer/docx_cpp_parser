"""Everything a :class:`~.diff_engine.DiffResult` can be turned into.

Five formats, one set of numbers.  The flat row schema in
:data:`DIFF_FIELD_NAMES` is the substrate: CSV, JSON, pandas and Polars are
projections of it, and the two human-facing formats — a terminal summary and the
Markdown and HTML reports — are renderings of the same aggregates the engine
already computed.  Nothing here recomputes a figure, which is what stops two
formats from ever disagreeing about how many comments were resolved.

Dependency posture matches the rest of the package: the summary, the rows, CSV,
JSON and the Markdown report need nothing but the standard library.  Only
``to_dataframe`` / ``to_polars`` and the HTML report reach for an extra, and each
says exactly what to install if it is missing.
"""

from __future__ import annotations

import csv
import json
from datetime import datetime, timedelta
from pathlib import Path
from typing import (
    TYPE_CHECKING,
    Any,
    Dict,
    Iterable,
    List,
    Optional,
    Sequence,
    Tuple,
    Union,
)

from ..models import Comment
from .diff_engine import ChangeKind, CommentChange, DiffResult, Hotspot, ReviewVelocity

if TYPE_CHECKING:  # pragma: no cover - typing only
    import jinja2
    import pandas
    import polars

__all__ = [
    "DIFF_FIELD_NAMES",
    "render_summary",
    "to_rows",
    "to_dict",
    "to_json",
    "export_json",
    "export_csv",
    "to_dataframe",
    "to_polars",
    "render_markdown_diff",
    "export_markdown_diff",
    "render_html_diff",
    "export_html_diff",
    "build_diff_payload",
]

PathLike = Union[str, "Path"]

_HERE = Path(__file__).parent
_TEMPLATE_DIR = _HERE / "templates"
_ASSET_DIR = _HERE / "assets"
_REPORT_ASSET_DIR = _HERE.parent / "reporting" / "assets"
_TEMPLATE_NAME = "diff.html.j2"

_MAX_DETAIL = 200
"""Changes listed individually in the Markdown report, per section. Past this
the detail dwarfs the summary, so it is truncated with an explicit note."""

_TOP_HOTSPOTS = 10
"""Hotspots shown in a report. The tail is available on the result object."""


DIFF_FIELD_NAMES: Tuple[str, ...] = (
    "change",
    "kinds",
    "match_level",
    "score",
    "comment_id_before",
    "comment_id_after",
    "author",
    "date",
    "date_parsed",
    "text_before",
    "text_after",
    "referenced_text_before",
    "referenced_text_after",
    "resolved_before",
    "resolved_after",
    "thread_depth",
    "root_id",
    "document_before",
    "document_after",
)
"""Canonical column order for the tabular diff, shared by every exporter.

Before and after are separate columns rather than one column plus a marker: a
diff is read by comparing the two, and a shape that forces the reader to pair up
rows themselves is a worse table however compact it looks.
"""


# ─────────────────────────────────────────────────────────────────────────────
#  Flat rows
# ─────────────────────────────────────────────────────────────────────────────


def _row(change: CommentChange) -> Dict[str, Any]:
    before, after = change.before, change.after
    face = change.comment
    return {
        "change": change.kind.value,
        "kinds": ";".join(kind.value for kind in change.kinds),
        "match_level": change.level.value if change.level is not None else "",
        "score": round(change.score, 4),
        "comment_id_before": before.comment_id if before else None,
        "comment_id_after": after.comment_id if after else None,
        "author": face.author,
        "date": face.date,
        "date_parsed": face.date_parsed,
        "text_before": before.text if before else "",
        "text_after": after.text if after else "",
        "referenced_text_before": before.referenced_text if before else "",
        "referenced_text_after": after.referenced_text if after else "",
        "resolved_before": before.resolved if before else None,
        "resolved_after": after.resolved if after else None,
        "thread_depth": face.thread_depth,
        "root_id": face.root_id,
        "document_before": before.document_name if before else "",
        "document_after": after.document_name if after else "",
    }


def to_rows(result: DiffResult) -> List[Dict[str, Any]]:
    """One dict per change, in canonical column order.

    Values keep their native Python types: ``date_parsed`` is a datetime and the
    ``resolved_*`` columns are ``None`` on the side where the comment does not
    exist, rather than a misleading ``False``.
    """
    return [_row(change) for change in result.changes]


# ─────────────────────────────────────────────────────────────────────────────
#  Summary
# ─────────────────────────────────────────────────────────────────────────────


def _days(span: Optional[timedelta]) -> str:
    if span is None:
        return "—"
    days = span.total_seconds() / 86400.0
    if days < 1:
        return f"{span.total_seconds() / 3600.0:.1f} h"
    return f"{days:.1f} d"


def _signed(value: int) -> str:
    return f"+{value}" if value > 0 else str(value)


def render_summary(result: DiffResult) -> str:
    """A short plain-text account of the cycle, for a terminal or a log."""
    velocity = result.velocity
    lines = [
        f"Comment diff — {result.before_name} → {result.after_name}",
        "",
        f"  Comments      {velocity.comments_before:>6,} → {velocity.comments_after:<6,}"
        f"  ({_signed(velocity.net_change)})",
        f"  Still open    {velocity.open_before:>6,} → {velocity.open_after:<6,}"
        f"  ({_signed(velocity.open_after - velocity.open_before)})",
        f"  Resolution    {f'{velocity.resolution_rate_before * 100:.0f}%':>6} →"
        f" {f'{velocity.resolution_rate_after * 100:.0f}%':<6}"
        f"  ({velocity.resolution_rate_delta * 100:+.0f} pts)",
        "",
        f"  Added         {velocity.added:>6,}",
        f"  Removed       {velocity.removed:>6,}",
        f"  Resolved      {velocity.resolved:>6,}",
        f"  Re-opened     {velocity.reopened:>6,}",
        f"  Edited        {velocity.edited:>6,}",
        f"  Unchanged     {velocity.unchanged:>6,}",
        "",
        f"  Net progress  {_signed(velocity.net_resolved):>6} resolved"
        f"   ({'converging' if velocity.is_converging else 'not converging'})",
    ]

    if velocity.median_time_to_resolution is not None:
        lines.append(
            f"  Age when resolved (median) {_days(velocity.median_time_to_resolution)}"
        )
    if velocity.oldest_open_age is not None:
        lines.append(f"  Oldest open comment        {_days(velocity.oldest_open_age)}")

    levels = _level_counts(result)
    lines += [
        "",
        "  Matched by  "
        + ", ".join(f"{count} {name}" for name, count in levels if count)
        + f"  (threshold {result.threshold:.2f}, {result.similarity_backend})",
    ]

    if result.hotspots:
        lines += ["", f"  Risk        {len(result.hotspots)} thread(s) still open"]
        for spot in result.hotspots[:3]:
            reason = f" — {spot.reasons[0]}" if spot.reasons else ""
            lines.append(f"    #{spot.root_id} {spot.author}: {spot.text[:60]}{reason}")

    return "\n".join(lines)


def _level_counts(result: DiffResult) -> List[Tuple[str, int]]:
    """How many pairs each matching level accounted for."""
    counts: Dict[str, int] = {"id": 0, "anchor": 0, "fuzzy": 0}
    for change in result.changes:
        if change.level is not None:
            counts[change.level.value] += 1
    return [("id", counts["id"]), ("anchor", counts["anchor"]), ("fuzzy", counts["fuzzy"])]


# ─────────────────────────────────────────────────────────────────────────────
#  Structured export
# ─────────────────────────────────────────────────────────────────────────────


def _velocity_dict(velocity: ReviewVelocity) -> Dict[str, Any]:
    def seconds(span: Optional[timedelta]) -> Optional[float]:
        return span.total_seconds() if span is not None else None

    return {
        "comments_before": velocity.comments_before,
        "comments_after": velocity.comments_after,
        "net_change": velocity.net_change,
        "added": velocity.added,
        "removed": velocity.removed,
        "resolved": velocity.resolved,
        "reopened": velocity.reopened,
        "edited": velocity.edited,
        "unchanged": velocity.unchanged,
        "net_resolved": velocity.net_resolved,
        "open_before": velocity.open_before,
        "open_after": velocity.open_after,
        "resolution_rate_before": velocity.resolution_rate_before,
        "resolution_rate_after": velocity.resolution_rate_after,
        "resolution_rate_delta": velocity.resolution_rate_delta,
        "is_converging": velocity.is_converging,
        "mean_time_to_resolution_seconds": seconds(velocity.mean_time_to_resolution),
        "median_time_to_resolution_seconds": seconds(
            velocity.median_time_to_resolution
        ),
        "mean_open_age_seconds": seconds(velocity.mean_open_age),
        "oldest_open_age_seconds": seconds(velocity.oldest_open_age),
    }


def _hotspot_dict(spot: Hotspot) -> Dict[str, Any]:
    return {
        "root_id": spot.root_id,
        "document": spot.document,
        "author": spot.author,
        "text": spot.text,
        "referenced_text": spot.referenced_text,
        "size": spot.size,
        "open_comments": spot.open_comments,
        "persistent_open": spot.persistent_open,
        "reopened": spot.reopened,
        "added": spot.added,
        "age_days": spot.age_days,
        "reasons": list(spot.reasons),
        "score": round(spot.score, 3),
    }


def to_dict(result: DiffResult) -> Dict[str, Any]:
    """The whole comparison as nested plain data.

    Datetimes become ISO-8601 strings and timedeltas become seconds, so the
    result passes straight through ``json.dumps`` and comes back out of
    ``json.loads`` without a custom decoder.
    """
    return {
        "before": result.before_name,
        "after": result.after_name,
        "generated_at": result.generated_at.isoformat(),
        "threshold": result.threshold,
        "similarity_backend": result.similarity_backend,
        "velocity": _velocity_dict(result.velocity),
        "hotspots": [_hotspot_dict(spot) for spot in result.hotspots],
        "changes": [
            {
                name: value.isoformat() if isinstance(value, datetime) else value
                for name, value in row.items()
            }
            for row in to_rows(result)
        ],
    }


def to_json(result: DiffResult, indent: int = 2) -> str:
    """Serialise the comparison to a JSON string."""
    return json.dumps(
        to_dict(result),
        indent=indent if indent > 0 else None,
        ensure_ascii=False,
    )


def _prepare(path: PathLike) -> "Path":
    target = Path(path)
    if target.parent and not target.parent.exists():
        target.parent.mkdir(parents=True, exist_ok=True)
    return target


def export_json(result: DiffResult, path: PathLike, indent: int = 2) -> "Path":
    """Write the comparison to a UTF-8 JSON file. Returns the path written."""
    target = _prepare(path)
    target.write_text(to_json(result, indent=indent), encoding="utf-8")
    return target


def _cell(value: Any) -> Any:
    """Render one value for a CSV cell.

    Same rules as the comment exporter: an absent value is an empty cell rather
    than the literal text ``None``, datetimes are ISO-8601 so the column
    round-trips through Excel and ``read_csv`` alike, and booleans are spelled
    out so they read identically in every locale.
    """
    if value is None:
        return ""
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, bool):
        return "true" if value else "false"
    return value


def to_csv_rows(result: DiffResult) -> Iterable[Sequence[Any]]:
    """Yield CSV-ready rows, header first."""
    yield list(DIFF_FIELD_NAMES)
    for row in to_rows(result):
        yield [_cell(row[name]) for name in DIFF_FIELD_NAMES]


def export_csv(
    result: DiffResult,
    path: PathLike,
    encoding: str = "utf-8",
    delimiter: str = ",",
) -> "Path":
    """Write the tabular diff to a CSV file. Returns the path written."""
    target = _prepare(path)
    # newline="" is required by the csv module; without it Windows turns the
    # writer's \r\n into \r\r\n and every other row comes out blank.
    with open(target, "w", newline="", encoding=encoding) as handle:
        csv.writer(handle, delimiter=delimiter).writerows(to_csv_rows(result))
    return target


def _require(package: str, extra: str) -> Any:
    try:
        return __import__(package)
    except ImportError as exc:  # pragma: no cover - depends on environment
        raise ImportError(
            f"{package} is required for this export but is not installed.\n"
            f"Install it with:  pip install docx-comment-parser[{extra}]"
        ) from exc


def _columns(result: DiffResult) -> Dict[str, List[Any]]:
    rows = to_rows(result)
    return {name: [row[name] for row in rows] for name in DIFF_FIELD_NAMES}


_BOOL_COLUMNS = ("resolved_before", "resolved_after")
_INT_COLUMNS = ("thread_depth", "root_id")
_NULLABLE_INT_COLUMNS = ("comment_id_before", "comment_id_after")


def to_dataframe(result: DiffResult) -> "pandas.DataFrame":
    """Return the tabular diff as a pandas DataFrame.

    Dtypes are pinned rather than inferred, so an empty diff has the same schema
    as a busy one.  The ``resolved_*`` and ``comment_id_*`` columns are nullable
    (``boolean`` / ``Int64``) because "this comment does not exist on that side"
    is a real value that ``False`` and ``-1`` would both misrepresent.

    Requires the ``pandas`` extra.
    """
    pd = _require("pandas", "pandas")

    frame = pd.DataFrame(_columns(result), columns=list(DIFF_FIELD_NAMES))
    for column in _INT_COLUMNS:
        frame[column] = frame[column].astype("int64")
    for column in _NULLABLE_INT_COLUMNS:
        frame[column] = frame[column].astype("Int64")
    for column in _BOOL_COLUMNS:
        frame[column] = frame[column].astype("boolean")
    frame["score"] = frame["score"].astype("float64")
    frame["date_parsed"] = pd.to_datetime(frame["date_parsed"], utc=True)
    return frame


def to_polars(result: DiffResult) -> "polars.DataFrame":
    """Return the tabular diff as a Polars DataFrame.

    An explicit schema is supplied so an empty result keeps its columns and
    their types instead of collapsing to ``Null``.  Requires the ``polars``
    extra.
    """
    pl = _require("polars", "polars")

    schema: Dict[str, Any] = {}
    for name in DIFF_FIELD_NAMES:
        if name in _BOOL_COLUMNS:
            schema[name] = pl.Boolean
        elif name in _INT_COLUMNS or name in _NULLABLE_INT_COLUMNS:
            schema[name] = pl.Int64
        elif name == "score":
            schema[name] = pl.Float64
        elif name == "date_parsed":
            schema[name] = pl.Datetime(time_unit="us", time_zone="UTC")
        else:
            schema[name] = pl.Utf8

    return pl.DataFrame(_columns(result), schema=schema)


# ─────────────────────────────────────────────────────────────────────────────
#  Markdown report
# ─────────────────────────────────────────────────────────────────────────────


def _escape(text: str) -> str:
    """Neutralise the Markdown characters that would corrupt a table cell."""
    return " ".join(text.split()).replace("|", "\\|")


def _clip(text: str, width: int) -> str:
    flat = " ".join(text.split())
    return flat if len(flat) <= width else flat[: width - 1] + "…"


_SECTION_TITLES: Tuple[Tuple[ChangeKind, str], ...] = (
    (ChangeKind.REOPENED, "Re-opened"),
    (ChangeKind.ADDED, "New comments"),
    (ChangeKind.RESOLVED, "Newly resolved"),
    (ChangeKind.REMOVED, "Removed"),
    (ChangeKind.EDITED, "Edited"),
)
"""Order of the detail sections: what went backwards first, then what is new,
then what closed.  A reviewer opens this file to find work, not a chronicle."""


def render_markdown_diff(
    result: DiffResult,
    title: Optional[str] = None,
    include_details: bool = True,
) -> str:
    """Render the comparison as Markdown. Standard library only.

    Parameters
    ----------
    title:
        Overrides the heading, which otherwise names both documents.
    include_details:
        Include the per-comment sections after the summary tables.  Set False
        for a digest of just the figures.
    """
    from .. import __version__

    velocity = result.velocity
    heading = title if title is not None else f"{result.before_name} → {result.after_name}"

    out: List[str] = [f"# Comment diff — {heading}", ""]

    if not result.changes:
        out += ["Neither document contains any comments.", ""]
        return "\n".join(out + _footer(result, __version__))

    if not result.has_changes:
        out += ["**No changes.** Every comment is identical in both versions.", ""]

    out += [
        "## What changed",
        "",
        "| | Count |",
        "| --- | ---: |",
        f"| New comments | {velocity.added:,} |",
        f"| Removed | {velocity.removed:,} |",
        f"| Newly resolved | {velocity.resolved:,} |",
        f"| Re-opened | {velocity.reopened:,} |",
        f"| Edited | {velocity.edited:,} |",
        f"| Unchanged | {velocity.unchanged:,} |",
        "",
        "## Review velocity",
        "",
        "| Metric | Before | After | Change |",
        "| --- | ---: | ---: | ---: |",
        f"| Comments | {velocity.comments_before:,} | {velocity.comments_after:,} "
        f"| {_signed(velocity.net_change)} |",
        f"| Still open | {velocity.open_before:,} | {velocity.open_after:,} "
        f"| {_signed(velocity.open_after - velocity.open_before)} |",
        f"| Resolution rate | {velocity.resolution_rate_before * 100:.0f}% "
        f"| {velocity.resolution_rate_after * 100:.0f}% "
        f"| {velocity.resolution_rate_delta * 100:+.0f} pts |",
        "",
        f"Net progress: **{_signed(velocity.net_resolved)}** comments closed "
        f"({'converging' if velocity.is_converging else 'not converging'}).",
        "",
    ]

    if velocity.median_time_to_resolution is not None or velocity.oldest_open_age is not None:
        out += [
            "| Timing | Value |",
            "| --- | ---: |",
        ]
        if velocity.median_time_to_resolution is not None:
            out.append(
                f"| Age when resolved (median) | {_days(velocity.median_time_to_resolution)} |"
            )
        if velocity.mean_time_to_resolution is not None:
            out.append(
                f"| Age when resolved (mean) | {_days(velocity.mean_time_to_resolution)} |"
            )
        if velocity.oldest_open_age is not None:
            out.append(f"| Oldest still-open comment | {_days(velocity.oldest_open_age)} |")
        out += [
            "",
            "_Word records when a comment was written and whether it is now marked "
            "done, but never when it was marked done. These ages are measured "
            "against the newest comment in the later document and are therefore a "
            "lower bound._",
            "",
        ]

    # ── Risk hotspots ───────────────────────────────────────────────────────
    if result.hotspots:
        out += [
            f"## Risk hotspots ({len(result.hotspots):,})",
            "",
            "| Thread | Reviewer | Open | Size | Age | Why |",
            "| --- | --- | ---: | ---: | ---: | --- |",
        ]
        for spot in result.hotspots[:_TOP_HOTSPOTS]:
            age = f"{spot.age_days:.0f} d" if spot.age_days is not None else "—"
            out.append(
                f"| #{spot.root_id} {_escape(_clip(spot.text, 70))} "
                f"| {_escape(spot.author) or '—'} | {spot.open_comments} "
                f"| {spot.size} | {age} "
                f"| {_escape(', '.join(spot.reasons)) or 'still open'} |"
            )
        if len(result.hotspots) > _TOP_HOTSPOTS:
            out.append(
                f"| _…and {len(result.hotspots) - _TOP_HOTSPOTS:,} more_ | | | | | |"
            )
        out.append("")

    # ── Detail ──────────────────────────────────────────────────────────────
    if include_details:
        for kind, heading_text in _SECTION_TITLES:
            entries = result.of_kind(kind)
            if not entries:
                continue
            out += [f"## {heading_text} ({len(entries):,})", ""]
            for change in entries[:_MAX_DETAIL]:
                out.extend(_markdown_entry(change, kind))
            if len(entries) > _MAX_DETAIL:
                out.append(f"- _…and {len(entries) - _MAX_DETAIL:,} more._")
            out.append("")

    return "\n".join(out + _footer(result, __version__))


def _markdown_entry(change: CommentChange, kind: ChangeKind) -> List[str]:
    """One change as a Markdown bullet, showing both sides when both exist."""
    face = change.comment
    identity = change.after.comment_id if change.after else change.comment_id
    lines = [
        f"- **[#{identity}] {_escape(face.author)}**: "
        f"{_escape(_clip(face.text, 200))}"
    ]
    if kind is ChangeKind.EDITED and change.before is not None:
        lines.append(f"  - was: {_escape(_clip(change.before.text, 200))}")
    if face.referenced_text:
        lines.append(f"  - on: _{_escape(_clip(face.referenced_text, 160))}_")
    return lines


def _footer(result: DiffResult, version: str) -> List[str]:
    stamp = result.generated_at.strftime("%Y-%m-%d %H:%M UTC")
    return [
        "---",
        "",
        f"_Generated by docx-comment-parser {version} on {stamp} · "
        f"matching threshold {result.threshold:.2f} · "
        f"{result.similarity_backend}._",
    ]


def export_markdown_diff(
    result: DiffResult,
    path: PathLike,
    title: Optional[str] = None,
    include_details: bool = True,
) -> "Path":
    """Write the Markdown diff report. Returns the path written."""
    target = _prepare(path)
    target.write_text(
        render_markdown_diff(result, title=title, include_details=include_details),
        encoding="utf-8",
    )
    return target


# ─────────────────────────────────────────────────────────────────────────────
#  HTML report
# ─────────────────────────────────────────────────────────────────────────────


def _require_jinja2() -> Any:
    """Import Jinja2, or raise an error that says exactly how to install it."""
    try:
        import jinja2
    except ImportError as exc:  # pragma: no cover - depends on environment
        raise ImportError(
            "jinja2 is required for HTML reports but is not installed.\n"
            "Install it with:  pip install docx-comment-parser[report]"
        ) from exc
    return jinja2


def _asset(directory: Path, name: str) -> str:
    return (directory / name).read_text(encoding="utf-8")


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
    if comment.date_parsed is not None:
        return comment.date_parsed.strftime("%Y-%m-%d %H:%M")
    return comment.date


def build_diff_payload(result: DiffResult) -> Dict[str, Any]:
    """Pack the changes into the compact structure the page's JavaScript reads.

    Authors, documents and change kinds are interned into lookup tables and each
    change becomes a positional array.  The report embeds its data once and
    filters it in the browser, so this is what keeps a diff of two
    5,000-comment documents a file that can be emailed.
    """
    authors: Dict[str, int] = {}
    documents: Dict[str, int] = {}
    kinds: Dict[str, int] = {}
    rows: List[List[Any]] = []

    for change in result.changes:
        face = change.comment
        rows.append([
            kinds.setdefault(change.kind.value, len(kinds)),
            [kinds.setdefault(k.value, len(kinds)) for k in change.kinds],
            change.level.value if change.level is not None else "",
            round(change.score, 3),
            change.before.comment_id if change.before else -1,
            change.after.comment_id if change.after else -1,
            authors.setdefault(face.author, len(authors)),
            _display_date(face),
            face.date_parsed.isoformat() if face.date_parsed else "",
            change.before.text if change.before else "",
            change.after.text if change.after else "",
            change.before.referenced_text if change.before else "",
            change.after.referenced_text if change.after else "",
            -1 if change.before is None else (1 if change.before.resolved else 0),
            -1 if change.after is None else (1 if change.after.resolved else 0),
            documents.setdefault(face.document_name, len(documents)),
        ])

    return {
        "kinds": list(kinds),
        "authors": list(authors),
        "documents": list(documents),
        "rows": rows,
    }


def _environment(template: Optional[PathLike]) -> "jinja2.Environment":
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


def _mix(result: DiffResult) -> List[Dict[str, Any]]:
    """The change mix as proportional segments, for the stacked summary bar."""
    velocity = result.velocity
    parts = [
        ("added", "New", velocity.added),
        ("removed", "Removed", velocity.removed),
        ("resolved", "Resolved", velocity.resolved),
        ("reopened", "Re-opened", velocity.reopened),
        ("edited", "Edited", velocity.edited),
        ("unchanged", "Unchanged", velocity.unchanged),
    ]
    total = sum(count for _slug, _label, count in parts)
    if not total:
        return []
    return [
        {
            "slug": slug,
            "label": label,
            "count": count,
            "percent": count / total * 100.0,
        }
        for slug, label, count in parts
        if count
    ]


def render_html_diff(
    result: DiffResult,
    title: Optional[str] = None,
    template: Optional[PathLike] = None,
) -> str:
    """Render the self-contained HTML diff report and return it as a string.

    The output has no external references of any kind — the stylesheet, the
    script and the data are inlined — so it opens from a file:// URL, from an
    email attachment, or from a machine with no network.

    Parameters
    ----------
    title:
        Overrides the heading, which otherwise names both documents.
    template:
        Path to a custom Jinja2 template.  It receives the same context as the
        built-in one: ``result``, ``velocity``, ``hotspots``, ``mix``,
        ``payload``, ``css``, ``js``, ``levels``, ``title`` and ``version``.

    Requires the ``report`` extra: ``pip install docx-comment-parser[report]``.
    """
    from .. import __version__

    environment = _environment(template)
    name = Path(template).name if template is not None else _TEMPLATE_NAME

    # The diff page inherits the review report's design tokens rather than
    # redefining them, so the two reports read as one product; diff.css adds
    # only what is new here.
    css = _asset(_REPORT_ASSET_DIR, "report.css") + "\n" + _asset(_ASSET_DIR, "diff.css")

    html: str = environment.get_template(name).render(
        result=result,
        velocity=result.velocity,
        hotspots=result.hotspots[:_TOP_HOTSPOTS],
        hotspot_total=len(result.hotspots),
        mix=_mix(result),
        levels=[(name, count) for name, count in _level_counts(result) if count],
        payload=_script_safe_json(build_diff_payload(result)),
        css=css,
        js=_asset(_ASSET_DIR, "diff.js"),
        title=title if title is not None else f"{result.before_name} → {result.after_name}",
        version=__version__,
    )
    return html


def export_html_diff(
    result: DiffResult,
    path: PathLike,
    title: Optional[str] = None,
    template: Optional[PathLike] = None,
) -> "Path":
    """Write the self-contained HTML diff report. Returns the path written."""
    target = _prepare(path)
    target.write_text(
        render_html_diff(result, title=title, template=template), encoding="utf-8"
    )
    return target
