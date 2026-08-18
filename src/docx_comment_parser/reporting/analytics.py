"""Review analytics: the numbers every report format shares.

This module is deliberately free of any rendering concern and of every optional
dependency.  It turns a flat list of :class:`~docx_comment_parser.Comment` rows
into the aggregates a reviewer actually wants to see — who commented, how much
is resolved, when the activity happened, and how the threads hang together —
and hands that to whichever renderer asked for it.

Keeping it separate is what lets the HTML report (Jinja2) and the Markdown
report (standard library only) show *identical* figures without either one
reimplementing the arithmetic.  It also means the analytics can be tested
directly, without parsing HTML.

Everything here is a single linear pass over the comments, so a 10,000-comment
document costs the same per comment as a 10-comment one.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Callable, Dict, Iterable, List, Optional, Sequence, Tuple

from ..models import Comment

__all__ = [
    "Overview",
    "AuthorStat",
    "TimelineBucket",
    "Thread",
    "ReportData",
    "build_report_data",
]

#: Cap on how many columns a gap-filled series may contain, applied to the daily
#: and weekly series independently.  Below this a chart shows a real calendar,
#: empty periods included, which is what makes a gap in the review visible.
#: Above it the empty columns would dominate both the chart and the file size,
#: so only the periods that actually contain comments are emitted.
#:
#: 400 columns is roughly a year of days, or eight years of weeks — and at the
#: chart's 960-unit width still leaves each column about 2 units wide.
_MAX_FILLED_BUCKETS = 400


# ─────────────────────────────────────────────────────────────────────────────
#  Aggregate shapes
# ─────────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Overview:
    """Headline figures for the whole report."""

    title: str
    """Human-readable name for what was reported on — a file name, or a count
    of files for a batch."""

    documents: Tuple[str, ...]
    """Every distinct ``document_name`` present in the data, sorted."""

    total_comments: int
    resolved: int
    unresolved: int
    resolution_rate: float
    """Fraction in ``0.0``–``1.0``. ``0.0`` when there are no comments."""

    unique_authors: int
    root_comments: int
    replies: int
    threads: int
    """Conversations with more than one comment. Standalone notes are excluded,
    which is what makes this different from ``root_comments``."""

    earliest: Optional[datetime]
    latest: Optional[datetime]
    generated_at: datetime

    @property
    def resolution_percent(self) -> float:
        """Resolution rate as a 0–100 number, for display."""
        return self.resolution_rate * 100.0


@dataclass(frozen=True)
class AuthorStat:
    """One reviewer's contribution and resolution record."""

    author: str
    initials: str
    total: int
    resolved: int
    unresolved: int
    resolution_rate: float
    first: Optional[datetime]
    last: Optional[datetime]

    @property
    def resolution_percent(self) -> float:
        return self.resolution_rate * 100.0


@dataclass(frozen=True)
class TimelineBucket:
    """Comment activity in one calendar period."""

    label: str
    """Display label — ``2024-03-05`` for a day, ``2024-W10`` for a week."""

    start: date
    total: int
    resolved: int

    @property
    def unresolved(self) -> int:
        return self.total - self.resolved


@dataclass(frozen=True)
class Thread:
    """One conversation: a root comment plus every reply beneath it."""

    root_id: int
    comments: Tuple[Comment, ...]
    """Root first, then replies ordered by depth and then by date."""

    participants: Tuple[str, ...]
    resolved: bool
    """True only when *every* comment in the thread is resolved — an open reply
    keeps the whole conversation open, which is how a reviewer reads it."""

    started: Optional[datetime]
    last_activity: Optional[datetime]

    @property
    def size(self) -> int:
        return len(self.comments)

    @property
    def root(self) -> Comment:
        return self.comments[0]

    @property
    def reply_count(self) -> int:
        return len(self.comments) - 1


@dataclass(frozen=True)
class ReportData:
    """Everything a renderer needs, computed once."""

    overview: Overview
    authors: Tuple[AuthorStat, ...]
    """Sorted by comment count descending, then by name."""

    daily: Tuple[TimelineBucket, ...]
    weekly: Tuple[TimelineBucket, ...]
    threads: Tuple[Thread, ...]
    """Every conversation, including standalone comments (threads of size 1),
    ordered by first activity."""

    comments: Tuple[Comment, ...]
    """The rows the report was built from, in their original order."""

    @property
    def is_empty(self) -> bool:
        return not self.comments


# ─────────────────────────────────────────────────────────────────────────────
#  Computation
# ─────────────────────────────────────────────────────────────────────────────


def _title_for(documents: Sequence[str]) -> str:
    """Name the report after its source: one file, or a count of files."""
    if not documents:
        return "Comment review report"
    if len(documents) == 1:
        # Show the file name rather than the full path: the path is often long,
        # machine-specific, and uninteresting to whoever reads the report.
        name = documents[0].replace("\\", "/").rsplit("/", 1)[-1]
        return name or documents[0]
    return f"{len(documents)} documents"


def _build_overview(
    comments: Sequence[Comment],
    threads: Sequence[Thread],
    generated_at: datetime,
) -> Overview:
    documents = tuple(sorted({c.document_name for c in comments if c.document_name}))
    total = len(comments)
    resolved = sum(1 for c in comments if c.resolved)
    replies = sum(1 for c in comments if c.is_reply)
    dates = [c.date_parsed for c in comments if c.date_parsed is not None]

    return Overview(
        title=_title_for(documents),
        documents=documents,
        total_comments=total,
        resolved=resolved,
        unresolved=total - resolved,
        resolution_rate=(resolved / total) if total else 0.0,
        unique_authors=len({c.author for c in comments}),
        root_comments=total - replies,
        replies=replies,
        threads=sum(1 for t in threads if t.size > 1),
        earliest=min(dates) if dates else None,
        latest=max(dates) if dates else None,
        generated_at=generated_at,
    )


def _build_authors(comments: Sequence[Comment]) -> Tuple[AuthorStat, ...]:
    totals: Counter[str] = Counter()
    resolved: Counter[str] = Counter()
    initials: Dict[str, str] = {}
    first: Dict[str, datetime] = {}
    last: Dict[str, datetime] = {}

    for comment in comments:
        author = comment.author
        totals[author] += 1
        if comment.resolved:
            resolved[author] += 1
        # Word lets the same person appear with an empty initials attribute on
        # some comments; keep the first non-empty one we see.
        if comment.initials and author not in initials:
            initials[author] = comment.initials
        stamp = comment.date_parsed
        if stamp is not None:
            if author not in first or stamp < first[author]:
                first[author] = stamp
            if author not in last or stamp > last[author]:
                last[author] = stamp

    stats = [
        AuthorStat(
            author=author,
            initials=initials.get(author, ""),
            total=count,
            resolved=resolved[author],
            unresolved=count - resolved[author],
            resolution_rate=resolved[author] / count if count else 0.0,
            first=first.get(author),
            last=last.get(author),
        )
        for author, count in totals.items()
    ]
    # Busiest reviewer first; ties broken by name so the order is deterministic
    # rather than dependent on dict insertion.
    stats.sort(key=lambda s: (-s.total, s.author))
    return tuple(stats)


def _week_start(day: date) -> date:
    """Monday of the ISO week containing ``day``."""
    return day - timedelta(days=day.weekday())


def _bucket(
    comments: Sequence[Comment],
    key: Callable[[date], date],
) -> Tuple[Dict[date, int], Dict[date, int]]:
    """Count total and resolved comments per bucket start date.

    ``key`` maps a calendar date onto the start of the period it belongs to —
    identity for days, :func:`_week_start` for weeks.
    """
    totals: Dict[date, int] = defaultdict(int)
    resolved: Dict[date, int] = defaultdict(int)
    for comment in comments:
        stamp = comment.date_parsed
        if stamp is None:
            # A comment with an unparseable timestamp cannot be placed on a
            # timeline; it still appears everywhere else in the report.
            continue
        bucket = key(stamp.date())
        totals[bucket] += 1
        if comment.resolved:
            resolved[bucket] += 1
    return totals, resolved


def _fill(
    totals: Dict[date, int],
    resolved: Dict[date, int],
    step: timedelta,
    label: Callable[[date], str],
    max_buckets: int,
) -> Tuple[TimelineBucket, ...]:
    """Turn bucket counts into a dense, gap-filled series.

    Empty periods are included so the chart shows a real calendar rather than
    squashing a three-week gap into a single pixel — unless doing so would need
    more than ``max_buckets`` columns, in which case only the periods that
    actually contain comments are emitted.
    """
    if not totals:
        return ()

    ordered = sorted(totals)
    start, end = ordered[0], ordered[-1]
    span = int((end - start) / step) + 1

    if span > max_buckets:
        points = ordered
    else:
        points = [start + step * i for i in range(span)]

    return tuple(
        TimelineBucket(
            label=label(point),
            start=point,
            total=totals.get(point, 0),
            resolved=resolved.get(point, 0),
        )
        for point in points
    )


def _build_timelines(
    comments: Sequence[Comment],
) -> Tuple[Tuple[TimelineBucket, ...], Tuple[TimelineBucket, ...]]:
    day_totals, day_resolved = _bucket(comments, lambda d: d)
    week_totals, week_resolved = _bucket(comments, _week_start)

    daily = _fill(
        day_totals,
        day_resolved,
        timedelta(days=1),
        lambda d: d.isoformat(),
        _MAX_FILLED_BUCKETS,
    )
    weekly = _fill(
        week_totals,
        week_resolved,
        timedelta(weeks=1),
        lambda d: "{}-W{:02d}".format(*d.isocalendar()[:2]),
        _MAX_FILLED_BUCKETS,
    )
    return daily, weekly


def _build_threads(comments: Sequence[Comment]) -> Tuple[Thread, ...]:
    """Group comments into conversations using the ``root_id`` already computed.

    ``root_id`` comes from :func:`~docx_comment_parser.models.build_comments`,
    which has already resolved (and cycle-broken) the parent chain, so this is a
    grouping pass rather than a second tree walk.
    """
    grouped: Dict[int, List[Comment]] = defaultdict(list)
    order: Dict[int, int] = {}
    for index, comment in enumerate(comments):
        grouped[comment.root_id].append(comment)
        order.setdefault(comment.root_id, index)

    threads: List[Thread] = []
    for root_id, members in grouped.items():
        # Root first, then replies by depth and date. `date_parsed` may be None,
        # so sort on a sentinel rather than letting the comparison explode.
        members.sort(
            key=lambda c: (
                c.thread_depth,
                c.date_parsed or datetime.min.replace(tzinfo=timezone.utc),
                c.comment_id,
            )
        )
        stamps = [c.date_parsed for c in members if c.date_parsed is not None]
        seen: List[str] = []
        for member in members:
            if member.author not in seen:
                seen.append(member.author)

        threads.append(
            Thread(
                root_id=root_id,
                comments=tuple(members),
                participants=tuple(seen),
                resolved=all(c.resolved for c in members),
                started=min(stamps) if stamps else None,
                last_activity=max(stamps) if stamps else None,
            )
        )

    threads.sort(key=lambda t: order[t.root_id])
    return tuple(threads)


def build_report_data(
    comments: Iterable[Comment],
    generated_at: Optional[datetime] = None,
) -> ReportData:
    """Compute every aggregate a report needs from a list of comments.

    Parameters
    ----------
    comments:
        Rows from ``parser.to_comments()``, or any filtered subset of them.
    generated_at:
        Timestamp shown in the report footer.  Defaults to now (UTC); pass an
        explicit value to make generated output byte-for-byte reproducible.
    """
    rows = tuple(comments)
    stamp = generated_at or datetime.now(timezone.utc)

    threads = _build_threads(rows)
    daily, weekly = _build_timelines(rows)

    return ReportData(
        overview=_build_overview(rows, threads, stamp),
        authors=_build_authors(rows),
        daily=daily,
        weekly=weekly,
        threads=threads,
        comments=rows,
    )
