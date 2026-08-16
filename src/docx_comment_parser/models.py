"""Typed, export-friendly representation of a single .docx comment.

The compiled core (``docx_comment_parser._core``) hands back
:class:`~docx_comment_parser.CommentMetadata` objects that mirror the OOXML
structures one-for-one.  That is the right shape for a parser but the wrong
shape for a DataFrame: parent links use a ``-1`` sentinel, "resolved" is called
``done``, dates are raw XML strings, and nothing records which document a
comment came from or how deep in a reply thread it sits.

:class:`Comment` is the flat, tabular projection of that data.  One instance per
comment, one row per instance, stable field order.  Everything the parser knows
is preserved — no field is dropped in translation.
"""

from __future__ import annotations

from dataclasses import dataclass, fields
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Dict, Iterable, List, Optional, Sequence, Tuple

if TYPE_CHECKING:  # pragma: no cover - typing only
    from docx_comment_parser._core import CommentMetadata, DocxParser

__all__ = ["Comment", "FIELD_NAMES", "parse_ooxml_date", "build_comments"]


@dataclass
class Comment:
    """One .docx comment, flattened for tabular export.

    The first thirteen fields are the canonical export schema.  The remaining
    fields carry the lower-level OOXML anchoring detail through unchanged, so
    nothing is lost by exporting rather than reading ``parser.comments()``.
    """

    # Slots roughly halve per-instance memory and speed up attribute access,
    # which matters when materialising 10,000+ comments.  They are declared
    # explicitly rather than via `@dataclass(slots=True)` because that argument
    # only exists on Python 3.10+ and this package supports 3.9.  Declaring
    # __slots__ by hand is safe here precisely because no field has a default —
    # there are no class-level attribute values to collide with the slots.
    #
    # ``test_slots_match_fields`` fails if this tuple drifts from the fields.
    __slots__ = (
        "comment_id", "parent_id", "author", "initials", "date", "date_parsed",
        "text", "referenced_text", "paragraph_style", "resolved", "is_reply",
        "thread_depth", "document_name", "root_id", "reply_count", "para_id",
        "para_id_parent", "range_start_para_id", "range_end_para_id",
        "paragraph_index", "run_index",
    )

    # ── canonical export schema ──────────────────────────────────────────────
    comment_id: int
    """``w:id`` of the comment. Unique within a document."""

    parent_id: Optional[int]
    """``comment_id`` of the comment this one replies to; ``None`` for roots."""

    author: str
    initials: str

    date: str
    """Raw ISO-8601 timestamp exactly as stored in the XML (never lossy)."""

    date_parsed: Optional[datetime]
    """``date`` parsed into a timezone-aware datetime, or ``None`` if unparseable."""

    text: str
    """Full plain text of the comment body."""

    referenced_text: str
    """The document text the comment is anchored to."""

    paragraph_style: str
    resolved: bool
    """OOXML ``w16cex:done`` — True when the comment has been marked resolved."""

    is_reply: bool
    thread_depth: int
    """0 for a root comment, 1 for a direct reply, 2 for a reply to a reply, ..."""

    document_name: str
    """Path of the .docx the comment came from (as passed to ``parse``)."""

    # ── additional detail carried through from the parser ────────────────────
    root_id: int
    """``comment_id`` of the root of this comment's thread (itself, for roots)."""

    reply_count: int
    """Number of direct replies to this comment."""

    para_id: str
    para_id_parent: str
    range_start_para_id: str
    range_end_para_id: str
    paragraph_index: int
    """0-based index of the anchoring paragraph in the document body; -1 if unknown."""

    run_index: int
    """0-based index of the anchoring run within its paragraph; -1 if unknown."""

    def to_dict(self) -> Dict[str, Any]:
        """Return this comment as a plain dict, in canonical field order."""
        return {name: getattr(self, name) for name in FIELD_NAMES}


FIELD_NAMES: Tuple[str, ...] = tuple(f.name for f in fields(Comment))
"""Canonical column order, shared by every exporter."""


# ─────────────────────────────────────────────────────────────────────────────
#  Date handling
# ─────────────────────────────────────────────────────────────────────────────

def parse_ooxml_date(raw: str) -> Optional[datetime]:
    """Parse an OOXML ``w:date`` string into a timezone-aware datetime.

    Word writes ``2024-03-01T10:22:00Z`` and, in some locales, values with a
    numeric offset or fractional seconds.  Python 3.9's
    :meth:`datetime.fromisoformat` accepts neither a trailing ``Z`` nor more
    than 6 fractional digits, so both are normalised first.

    Returns ``None`` for empty or unparseable input rather than raising — a
    single malformed timestamp must never abort a 10,000-comment export.
    """
    if not raw:
        return None

    text = raw.strip()
    if not text:
        return None

    # Normalise the UTC designator, which fromisoformat rejects before 3.11.
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"

    # Clamp fractional seconds to microsecond precision.
    dot = text.find(".")
    if dot != -1:
        end = dot + 1
        while end < len(text) and text[end].isdigit():
            end += 1
        digits = text[dot + 1:end]
        if len(digits) > 6:
            text = text[:dot + 1] + digits[:6] + text[end:]

    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None

    # Word occasionally omits the offset; treat naive timestamps as UTC so the
    # column has a single consistent dtype instead of a mix that pandas rejects.
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


# ─────────────────────────────────────────────────────────────────────────────
#  CommentMetadata → Comment
# ─────────────────────────────────────────────────────────────────────────────

def _resolve_thread_positions(
    metas: Sequence["CommentMetadata"],
) -> Tuple[Dict[int, int], Dict[int, int]]:
    """Compute ``{comment_id: depth}`` and ``{comment_id: root_id}`` in one pass.

    Depth is the length of the parent chain.  The walk is memoised, so the whole
    document costs O(n) rather than O(n·depth), and a ``seen`` set breaks any
    cycle a malformed document might contain (a self-referencing parent would
    otherwise loop forever).
    """
    parent_of: Dict[int, int] = {}
    for meta in metas:
        declared_parent = meta.parent_id
        # The C++ layer uses -1 as "no parent"; only keep real links, and only
        # to comments that actually exist.
        if declared_parent is not None and declared_parent >= 0:
            parent_of[meta.id] = declared_parent

    known = {meta.id for meta in metas}
    depths: Dict[int, int] = {}
    roots: Dict[int, int] = {}

    for meta in metas:
        # Walk up to the root (or to something already resolved), recording the
        # path so every node on it can be filled in without re-walking.
        path: List[int] = []
        node = meta.id
        seen = {node}
        while node not in depths:
            parent = parent_of.get(node)
            if parent is None or parent not in known or parent in seen:
                # Root reached — or a dangling/cyclic link, which we treat as a
                # root so the comment still exports rather than disappearing.
                depths[node] = 0
                roots[node] = node
                break
            path.append(node)
            seen.add(parent)
            node = parent

        base_depth = depths[node]
        base_root = roots[node]
        for offset, ancestor in enumerate(reversed(path), start=1):
            depths[ancestor] = base_depth + offset
            roots[ancestor] = base_root

    return depths, roots


def _to_comment(
    meta: "CommentMetadata",
    document_name: str,
    depth: int,
    root_id: int,
) -> Comment:
    """Project one :class:`CommentMetadata` onto a :class:`Comment`."""
    parent = meta.parent_id
    return Comment(
        comment_id=meta.id,
        parent_id=parent if parent is not None and parent >= 0 else None,
        author=meta.author,
        initials=meta.initials,
        date=meta.date,
        date_parsed=parse_ooxml_date(meta.date),
        text=meta.text,
        referenced_text=meta.referenced_text,
        paragraph_style=meta.paragraph_style,
        resolved=meta.done,
        is_reply=meta.is_reply,
        thread_depth=depth,
        document_name=document_name,
        root_id=root_id,
        reply_count=len(meta.replies),
        para_id=meta.para_id,
        para_id_parent=meta.para_id_parent,
        range_start_para_id=meta.range_start_para_id,
        range_end_para_id=meta.range_end_para_id,
        paragraph_index=meta.paragraph_index,
        run_index=meta.run_index,
    )


def build_comments(
    metas: Iterable["CommentMetadata"],
    document_name: str = "",
) -> List[Comment]:
    """Convert raw parser output into a list of :class:`Comment` rows.

    Parameters
    ----------
    metas:
        ``CommentMetadata`` objects, typically ``parser.comments()``.
    document_name:
        Value for the ``document_name`` column — normally the .docx path.
    """
    materialised: List["CommentMetadata"] = list(metas)
    depths, roots = _resolve_thread_positions(materialised)
    return [
        _to_comment(meta, document_name, depths[meta.id], roots[meta.id])
        for meta in materialised
    ]


def comments_from_parser(parser: "DocxParser") -> List[Comment]:
    """Build :class:`Comment` rows from a parsed :class:`DocxParser`.

    The document name is taken from the parser's own stats, so callers never
    have to repeat the path they already passed to ``parse()``.
    """
    return build_comments(parser.comments(), parser.stats().file_path)
