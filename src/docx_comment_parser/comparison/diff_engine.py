"""Turning matched comments into an answer about a review cycle.

:mod:`.matcher` says *which comment is which*.  This module says *what
happened*: what was added, what went away, what got resolved, what came back
open, and — the two questions a review lead actually asks — how fast the review
is converging and which threads are the ones to worry about.

A change is described as a set of facets rather than a single label, because the
real ones overlap: a comment can be reworded *and* resolved in the same cycle,
and a diff that had to pick one of those would be lying about the other.
:attr:`DiffResult.resolved` and :attr:`DiffResult.edited` therefore both contain
that comment, and :attr:`DiffResult.unchanged` contains only comments where
nothing at all moved.

Every aggregate here is a linear pass over the comments, so the cost of a diff
is dominated by the matching, not by the reporting on it.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
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

from ..models import Comment, build_comments
from .matcher import DEFAULT_THRESHOLD, Match, MatchLevel, MatchResult, match_comments
from .similarity import backend, normalise

if TYPE_CHECKING:  # pragma: no cover - typing only
    import pandas
    import polars

__all__ = [
    "ChangeKind",
    "CommentChange",
    "ReviewVelocity",
    "Hotspot",
    "DiffResult",
    "compare_comments",
    "diff_comments",
]

PathLike = Union[str, "Path"]
CommentSource = Union[str, "Path", Iterable[Comment]]

_HOTSPOT_SNIPPET = 240
"""Characters of a root comment carried into a hotspot. Enough to recognise the
issue, short enough that a hotspot table stays scannable."""


class ChangeKind(str, Enum):
    """One facet of what happened to a comment between two versions."""

    ADDED = "added"
    """Present in the newer document only."""

    REMOVED = "removed"
    """Present in the older document only — deleted, or its anchor was cut."""

    RESOLVED = "resolved"
    """Open before, resolved after."""

    REOPENED = "reopened"
    """Resolved before, open after."""

    EDITED = "edited"
    """The comment's own text changed."""

    REANCHORED = "reanchored"
    """The document text the comment points at changed."""

    UNCHANGED = "unchanged"
    """Matched, with no change to text, anchor or status."""


@dataclass(frozen=True)
class CommentChange:
    """What happened to one comment across the two versions.

    Exactly one of ``before`` / ``after`` is ``None`` for an addition or a
    removal; both are present for everything else.
    """

    before: Optional[Comment]
    after: Optional[Comment]
    kinds: Tuple[ChangeKind, ...]
    """Every facet that applies, in a stable order. Never empty."""

    level: Optional[MatchLevel]
    """How the pair was matched; ``None`` for additions and removals."""

    score: float
    """Similarity of the two comment bodies, 0.0–1.0. ``0.0`` when unmatched."""

    @property
    def comment(self) -> Comment:
        """The best single representative: the newer version where there is one.

        Reports show one row per change, and for everything except a removal the
        newer text is the one a reader wants to see.
        """
        return self.after if self.after is not None else _require(self.before)

    @property
    def kind(self) -> ChangeKind:
        """The headline facet, for a display that has room for only one."""
        return self.kinds[0]

    @property
    def author(self) -> str:
        return self.comment.author

    @property
    def comment_id(self) -> int:
        return self.comment.comment_id

    @property
    def resolved(self) -> bool:
        """Resolution state in the newer document (or the older, if removed)."""
        return self.comment.resolved

    def has(self, kind: ChangeKind) -> bool:
        return kind in self.kinds


def _require(comment: Optional[Comment]) -> Comment:
    """Assert a comment is present. Only reachable if a change had neither side."""
    if comment is None:  # pragma: no cover - defensive
        raise ValueError("a change must have a before or an after comment")
    return comment


# ─────────────────────────────────────────────────────────────────────────────
#  Enterprise aggregates
# ─────────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class ReviewVelocity:
    """How much the review moved between the two versions.

    The timing figures need a caveat that matters: OOXML records when a comment
    was *written* and whether it is now marked done, but never when it was
    marked done.  There is no resolution timestamp to read.  So "time to
    resolution" here is the age of a newly resolved comment measured against the
    newest activity in the later document — the closest thing to "how long this
    sat open" that the file format can support.  It is a lower bound on the real
    figure and is stated as such wherever it is displayed.
    """

    comments_before: int
    comments_after: int
    added: int
    removed: int
    resolved: int
    reopened: int
    edited: int
    unchanged: int

    open_before: int
    open_after: int
    resolution_rate_before: float
    resolution_rate_after: float

    mean_time_to_resolution: Optional[timedelta]
    median_time_to_resolution: Optional[timedelta]
    mean_open_age: Optional[timedelta]
    oldest_open_age: Optional[timedelta]

    @property
    def net_resolved(self) -> int:
        """Comments closed minus comments re-opened — the cycle's real progress."""
        return self.resolved - self.reopened

    @property
    def net_change(self) -> int:
        """Growth in the number of comments."""
        return self.comments_after - self.comments_before

    @property
    def resolution_rate_delta(self) -> float:
        """Change in the share of comments that are resolved, -1.0 to 1.0."""
        return self.resolution_rate_after - self.resolution_rate_before

    @property
    def is_converging(self) -> bool:
        """True when the review is closing faster than it is opening.

        The question behind "are we nearly done?": more comments closed than
        re-opened, and the open pile no larger than it was.
        """
        return self.net_resolved > 0 and self.open_after <= self.open_before


@dataclass(frozen=True)
class Hotspot:
    """A conversation that carries risk into the next review cycle."""

    root_id: int
    document: str
    author: str
    text: str
    """The root comment, clipped."""

    referenced_text: str
    size: int
    """Comments in the thread, in the newer document."""

    open_comments: int
    persistent_open: int
    """Comments that were open in the older document and are still open."""

    reopened: int
    added: int
    """Comments that joined this thread in the newer document."""

    age_days: Optional[float]
    """Days from the thread's first comment to the newest activity in the
    document, or ``None`` when no comment in it has a usable date."""

    reasons: Tuple[str, ...]
    score: float
    """Ranking weight; comparable within one diff, not across diffs."""


# ─────────────────────────────────────────────────────────────────────────────
#  The result
# ─────────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class DiffResult:
    """Everything the comparison found, ready to summarise, export or render."""

    before_name: str
    after_name: str
    before_comments: Tuple[Comment, ...]
    after_comments: Tuple[Comment, ...]
    changes: Tuple[CommentChange, ...]
    """Every comment on either side, exactly once, in reading order:
    matched comments in the older document's order, then additions."""

    velocity: ReviewVelocity
    hotspots: Tuple[Hotspot, ...]
    """Threads that still need attention, most pressing first."""

    threshold: float
    similarity_backend: str
    """``rapidfuzz`` or ``difflib`` — which engine scored the fuzzy matches."""

    generated_at: datetime

    # ── Facet views ─────────────────────────────────────────────────────────

    def of_kind(self, kind: ChangeKind) -> Tuple[CommentChange, ...]:
        """Every change carrying ``kind``, in reading order."""
        return tuple(change for change in self.changes if kind in change.kinds)

    @property
    def added(self) -> Tuple[CommentChange, ...]:
        """Comments that appear only in the newer document."""
        return self.of_kind(ChangeKind.ADDED)

    @property
    def removed(self) -> Tuple[CommentChange, ...]:
        """Comments that appear only in the older document."""
        return self.of_kind(ChangeKind.REMOVED)

    @property
    def resolved(self) -> Tuple[CommentChange, ...]:
        """Comments that were open and are now marked resolved."""
        return self.of_kind(ChangeKind.RESOLVED)

    @property
    def reopened(self) -> Tuple[CommentChange, ...]:
        """Comments that were resolved and are open again."""
        return self.of_kind(ChangeKind.REOPENED)

    @property
    def edited(self) -> Tuple[CommentChange, ...]:
        """Matched comments whose own text changed."""
        return self.of_kind(ChangeKind.EDITED)

    @property
    def reanchored(self) -> Tuple[CommentChange, ...]:
        """Matched comments where the document text they point at changed."""
        return self.of_kind(ChangeKind.REANCHORED)

    @property
    def unchanged(self) -> Tuple[CommentChange, ...]:
        """Matched comments where nothing moved at all."""
        return self.of_kind(ChangeKind.UNCHANGED)

    @property
    def matched(self) -> Tuple[CommentChange, ...]:
        """Every change with a comment on both sides."""
        return tuple(
            change
            for change in self.changes
            if change.before is not None and change.after is not None
        )

    @property
    def has_changes(self) -> bool:
        """False when the two documents' comments are identical."""
        return any(not change.has(ChangeKind.UNCHANGED) for change in self.changes)

    # ── Outputs ─────────────────────────────────────────────────────────────

    def summary(self) -> str:
        """A short human-readable account of the cycle."""
        from .diff_report import render_summary

        return render_summary(self)

    def to_rows(self) -> List[Dict[str, Any]]:
        """One flat dict per change, in canonical column order."""
        from .diff_report import to_rows

        return to_rows(self)

    def to_dict(self) -> Dict[str, Any]:
        """The whole comparison as nested plain data, ready for ``json.dumps``."""
        from .diff_report import to_dict

        return to_dict(self)

    def to_json(self, indent: int = 2) -> str:
        """The whole comparison as a JSON string."""
        from .diff_report import to_json

        return to_json(self, indent=indent)

    def export_json(self, path: PathLike, indent: int = 2) -> "Path":
        """Write :meth:`to_dict` to a JSON file. Returns the path written."""
        from .diff_report import export_json

        return export_json(self, path, indent=indent)

    def export_csv(
        self, path: PathLike, encoding: str = "utf-8", delimiter: str = ","
    ) -> "Path":
        """Write :meth:`to_rows` to a CSV file. Returns the path written."""
        from .diff_report import export_csv

        return export_csv(self, path, encoding=encoding, delimiter=delimiter)

    def to_dataframe(self) -> "pandas.DataFrame":
        """Return :meth:`to_rows` as a pandas DataFrame. Requires the ``pandas`` extra."""
        from .diff_report import to_dataframe

        return to_dataframe(self)

    def to_polars(self) -> "polars.DataFrame":
        """Return :meth:`to_rows` as a Polars DataFrame. Requires the ``polars`` extra."""
        from .diff_report import to_polars

        return to_polars(self)

    def to_html(self, title: Optional[str] = None, template: Optional[PathLike] = None) -> str:
        """Render the self-contained HTML diff report. Requires the ``report`` extra."""
        from .diff_report import render_html_diff

        return render_html_diff(self, title=title, template=template)

    def export_html(
        self,
        path: PathLike,
        title: Optional[str] = None,
        template: Optional[PathLike] = None,
    ) -> "Path":
        """Write the self-contained HTML diff report. Requires the ``report`` extra."""
        from .diff_report import export_html_diff

        return export_html_diff(self, path, title=title, template=template)

    def to_markdown(self, title: Optional[str] = None, include_details: bool = True) -> str:
        """Render a Markdown diff report. Needs no extras."""
        from .diff_report import render_markdown_diff

        return render_markdown_diff(self, title=title, include_details=include_details)

    def export_markdown(
        self,
        path: PathLike,
        title: Optional[str] = None,
        include_details: bool = True,
    ) -> "Path":
        """Write a Markdown diff report. Needs no extras."""
        from .diff_report import export_markdown_diff

        return export_markdown_diff(
            self, path, title=title, include_details=include_details
        )


# ─────────────────────────────────────────────────────────────────────────────
#  Classification
# ─────────────────────────────────────────────────────────────────────────────


def _facets(match: Match) -> Tuple[ChangeKind, ...]:
    """Every way in which a matched pair differs.

    Ordered so that the first facet is the one worth putting in a narrow column:
    a status change outranks an edit, because "this got resolved" is the news
    and "the wording changed" is the detail.
    """
    before, after = match.before, match.after
    kinds: List[ChangeKind] = []

    if before.resolved != after.resolved:
        kinds.append(ChangeKind.RESOLVED if after.resolved else ChangeKind.REOPENED)
    if normalise(before.text) != normalise(after.text):
        kinds.append(ChangeKind.EDITED)
    if normalise(before.referenced_text) != normalise(after.referenced_text):
        kinds.append(ChangeKind.REANCHORED)

    return tuple(kinds) if kinds else (ChangeKind.UNCHANGED,)


def _build_changes(result: MatchResult) -> Tuple[CommentChange, ...]:
    """Project matched pairs and orphans onto one ordered list of changes."""
    changes: List[CommentChange] = [
        CommentChange(
            before=match.before,
            after=match.after,
            kinds=_facets(match),
            level=match.level,
            score=match.score,
        )
        for match in result.matches
    ]

    changes.extend(
        CommentChange(
            before=comment,
            after=None,
            kinds=(ChangeKind.REMOVED,),
            level=None,
            score=0.0,
        )
        for comment in result.unmatched_before
    )
    # Removals belong beside the comments they sat next to in the old document;
    # additions have no such place, so they go last, in the new document's order.
    changes.sort(
        key=lambda change: (
            change.before.document_name if change.before else "",
            change.before.comment_id if change.before else 0,
        )
    )

    changes.extend(
        CommentChange(
            before=None,
            after=comment,
            kinds=(ChangeKind.ADDED,),
            level=None,
            score=0.0,
        )
        for comment in sorted(
            result.unmatched_after,
            key=lambda comment: (comment.document_name, comment.comment_id),
        )
    )
    return tuple(changes)


# ─────────────────────────────────────────────────────────────────────────────
#  Velocity
# ─────────────────────────────────────────────────────────────────────────────


def _latest(comments: Sequence[Comment]) -> Optional[datetime]:
    stamps = [c.date_parsed for c in comments if c.date_parsed is not None]
    return max(stamps) if stamps else None


def _mean(spans: Sequence[timedelta]) -> Optional[timedelta]:
    if not spans:
        return None
    return sum(spans, timedelta()) / len(spans)


def _median(spans: Sequence[timedelta]) -> Optional[timedelta]:
    if not spans:
        return None
    ordered = sorted(spans)
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[middle]
    return (ordered[middle - 1] + ordered[middle]) / 2


def _build_velocity(
    before: Sequence[Comment],
    after: Sequence[Comment],
    changes: Sequence[CommentChange],
) -> ReviewVelocity:
    counts: Dict[ChangeKind, int] = defaultdict(int)
    for change in changes:
        for kind in change.kinds:
            counts[kind] += 1

    resolved_before = sum(1 for c in before if c.resolved)
    resolved_after = sum(1 for c in after if c.resolved)

    # The newest comment in the later document stands in for "now": it is the
    # only point in time the file itself can vouch for.
    horizon = _latest(after) or _latest(before)

    closed_spans: List[timedelta] = []
    open_spans: List[timedelta] = []
    if horizon is not None:
        for change in changes:
            if change.after is None:
                continue
            born = change.after.date_parsed
            if born is None or born > horizon:
                continue
            if ChangeKind.RESOLVED in change.kinds:
                closed_spans.append(horizon - born)
            elif not change.after.resolved:
                open_spans.append(horizon - born)

    return ReviewVelocity(
        comments_before=len(before),
        comments_after=len(after),
        added=counts[ChangeKind.ADDED],
        removed=counts[ChangeKind.REMOVED],
        resolved=counts[ChangeKind.RESOLVED],
        reopened=counts[ChangeKind.REOPENED],
        edited=counts[ChangeKind.EDITED],
        unchanged=counts[ChangeKind.UNCHANGED],
        open_before=len(before) - resolved_before,
        open_after=len(after) - resolved_after,
        resolution_rate_before=resolved_before / len(before) if before else 0.0,
        resolution_rate_after=resolved_after / len(after) if after else 0.0,
        mean_time_to_resolution=_mean(closed_spans),
        median_time_to_resolution=_median(closed_spans),
        mean_open_age=_mean(open_spans),
        oldest_open_age=max(open_spans) if open_spans else None,
    )


# ─────────────────────────────────────────────────────────────────────────────
#  Hotspots
# ─────────────────────────────────────────────────────────────────────────────

# Weights for ranking risk.  They encode a judgement, so they are named: a
# thread that came back open is the loudest signal a review can produce, a
# thread nobody has closed across two cycles is the next, and sheer size or age
# only breaks ties between threads that already qualify.
_W_REOPENED = 4.0
_W_PERSISTENT = 3.0
_W_OPEN = 1.5
_W_ADDED = 0.5
_W_AGE_PER_MONTH = 1.0

_PERSISTENT_REASON = "open across both versions"
_REOPENED_REASON = "re-opened after being resolved"
_GROWING_REASON = "still attracting new comments"
_LONG_REASON = "long-running"


def _clip(text: str, width: int = _HOTSPOT_SNIPPET) -> str:
    flat = " ".join(text.split())
    return flat if len(flat) <= width else flat[: width - 1] + "…"


def _build_hotspots(
    after: Sequence[Comment], changes: Sequence[CommentChange]
) -> Tuple[Hotspot, ...]:
    """Rank the threads of the newer document by how much risk they carry.

    Only threads with something still open are considered: a fully resolved
    conversation, however long or contentious it was, is finished business.
    """
    horizon = _latest(after)

    members: Dict[Tuple[str, int], List[Comment]] = defaultdict(list)
    for comment in after:
        members[(comment.document_name, comment.root_id)].append(comment)

    persistent: Dict[Tuple[str, int], int] = defaultdict(int)
    reopened: Dict[Tuple[str, int], int] = defaultdict(int)
    added: Dict[Tuple[str, int], int] = defaultdict(int)
    for change in changes:
        after_comment = change.after
        if after_comment is None:
            continue
        key = (after_comment.document_name, after_comment.root_id)
        if ChangeKind.REOPENED in change.kinds:
            reopened[key] += 1
        if ChangeKind.ADDED in change.kinds:
            added[key] += 1
        if (
            change.before is not None
            and not change.before.resolved
            and not after_comment.resolved
        ):
            persistent[key] += 1

    hotspots: List[Hotspot] = []
    for key, thread in members.items():
        open_comments = sum(1 for c in thread if not c.resolved)
        if not open_comments:
            continue

        thread = sorted(thread, key=lambda c: (c.thread_depth, c.comment_id))
        root = thread[0]

        stamps = [c.date_parsed for c in thread if c.date_parsed is not None]
        age_days: Optional[float] = None
        if stamps and horizon is not None:
            age_days = max((horizon - min(stamps)).total_seconds() / 86400.0, 0.0)

        reasons: List[str] = []
        if reopened[key]:
            reasons.append(_REOPENED_REASON)
        if persistent[key]:
            reasons.append(_PERSISTENT_REASON)
        if added[key] >= 2:
            reasons.append(_GROWING_REASON)
        if age_days is not None and age_days >= 30:
            reasons.append(_LONG_REASON)

        score = (
            _W_REOPENED * reopened[key]
            + _W_PERSISTENT * persistent[key]
            + _W_OPEN * open_comments
            + _W_ADDED * added[key]
            + (_W_AGE_PER_MONTH * age_days / 30.0 if age_days else 0.0)
        )

        hotspots.append(
            Hotspot(
                root_id=root.root_id,
                document=root.document_name,
                author=root.author,
                text=_clip(root.text),
                referenced_text=_clip(root.referenced_text),
                size=len(thread),
                open_comments=open_comments,
                persistent_open=persistent[key],
                reopened=reopened[key],
                added=added[key],
                age_days=age_days,
                reasons=tuple(reasons),
                score=score,
            )
        )

    hotspots.sort(key=lambda spot: (-spot.score, spot.document, spot.root_id))
    return tuple(hotspots)


# ─────────────────────────────────────────────────────────────────────────────
#  Entry points
# ─────────────────────────────────────────────────────────────────────────────


def _name_of(comments: Sequence[Comment], fallback: str) -> str:
    """Name a side of the comparison after the document(s) it came from."""
    documents = sorted({c.document_name for c in comments if c.document_name})
    if not documents:
        return fallback
    if len(documents) == 1:
        return documents[0].replace("\\", "/").rsplit("/", 1)[-1] or documents[0]
    return f"{len(documents)} documents"


def _as_comments(source: CommentSource) -> Tuple[List[Comment], Optional[str]]:
    """Accept either a .docx path or an already-parsed list of comments.

    Returning the path alongside the rows lets a comparison of two files label
    itself without the caller repeating the names.
    """
    if isinstance(source, (str, Path)):
        from .._core import DocxParser

        parser = DocxParser()
        parser.parse(str(source))
        rows = build_comments(parser.comments(), parser.stats().file_path)
        return rows, Path(source).name
    return list(source), None


def diff_comments(
    before: Sequence[Comment],
    after: Sequence[Comment],
    threshold: float = DEFAULT_THRESHOLD,
    fuzzy: bool = True,
    before_name: str = "before",
    after_name: str = "after",
    generated_at: Optional[datetime] = None,
) -> DiffResult:
    """Compare two already-parsed lists of comments.

    :func:`compare_comments` is the friendlier entry point; this one is here for
    callers that have already filtered or assembled the rows themselves.
    """
    matched = match_comments(before, after, threshold=threshold, fuzzy=fuzzy)
    changes = _build_changes(matched)

    return DiffResult(
        before_name=before_name,
        after_name=after_name,
        before_comments=tuple(before),
        after_comments=tuple(after),
        changes=changes,
        velocity=_build_velocity(before, after, changes),
        hotspots=_build_hotspots(after, changes),
        threshold=threshold,
        similarity_backend=backend(),
        generated_at=generated_at or datetime.now(timezone.utc),
    )


def compare_comments(
    before: CommentSource,
    after: CommentSource,
    threshold: float = DEFAULT_THRESHOLD,
    fuzzy: bool = True,
    generated_at: Optional[datetime] = None,
) -> DiffResult:
    """Compare the comments of two review cycles.

    ::

        from docx_comment_parser import compare_comments

        result = compare_comments("review_v1.docx", "review_v2.docx")
        print(result.summary())
        result.export_html("what_changed.html")

    Parameters
    ----------
    before, after:
        A path to a ``.docx``, or a list of
        :class:`~docx_comment_parser.Comment` rows you already have — from
        ``parser.to_comments()``, or from ``filter_comments(...)`` if only part
        of the document should take part in the comparison.
    threshold:
        Similarity a level 3 (fuzzy) match must reach, 0.0–1.0.  Defaults to
        0.85.  Raise it to demand near-identical text; lower it when the second
        cycle rewrote comments heavily.
    fuzzy:
        Set False to match on ids and anchors only.  Faster, and appropriate
        when a text-similarity guess would be worse than reporting an add and a
        removal honestly.
    generated_at:
        Timestamp recorded in the result and shown in reports.  Defaults to now
        (UTC); pass a fixed value for byte-for-byte reproducible output.

    Returns
    -------
    DiffResult
        Which carries ``added``, ``removed``, ``resolved`` and ``reopened``,
        along with ``velocity``, ``hotspots``, and every export and report
        format.

    Notes
    -----
    Fuzzy matching uses ``rapidfuzz`` when it is installed
    (``pip install docx-comment-parser[diff]``) and the standard library's
    ``difflib`` otherwise.  Both produce the same kind of answer; rapidfuzz
    produces it faster.  :attr:`DiffResult.similarity_backend` records which one
    ran.
    """
    before_rows, before_path = _as_comments(before)
    after_rows, after_path = _as_comments(after)

    return diff_comments(
        before_rows,
        after_rows,
        threshold=threshold,
        fuzzy=fuzzy,
        before_name=before_path or _name_of(before_rows, "before"),
        after_name=after_path or _name_of(after_rows, "after"),
        generated_at=generated_at,
    )
