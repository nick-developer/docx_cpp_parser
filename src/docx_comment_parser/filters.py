"""Comment filtering shared by the CLI and available to library users.

Kept separate from the CLI so the same predicates can be applied to comments
before any export, and so they can be tested without going through a terminal.
"""

from __future__ import annotations

from collections import Counter
from typing import List, Optional, Sequence

from .models import Comment

__all__ = ["filter_comments"]


def filter_comments(
    comments: Sequence[Comment],
    author: Optional[str] = None,
    contains: Optional[str] = None,
    resolved: Optional[bool] = None,
    threads_only: bool = False,
) -> List[Comment]:
    """Return the subset of ``comments`` matching every supplied criterion.

    Criteria are combined with AND.  Passing nothing returns all comments.

    Parameters
    ----------
    author:
        Case-insensitive substring match against the author name, so
        ``"alice"`` matches ``"Alice Smith"``.
    contains:
        Case-insensitive substring match against the comment text *or* the
        document text it is anchored to.
    resolved:
        ``True`` keeps only resolved comments, ``False`` only unresolved ones,
        ``None`` keeps both.
    threads_only:
        Keep only comments belonging to a thread of more than one comment —
        that is, a root with at least one reply, plus those replies.  Standalone
        comments are dropped.
    """
    result = list(comments)

    if author:
        needle = author.casefold()
        result = [c for c in result if needle in c.author.casefold()]

    if contains:
        needle = contains.casefold()
        result = [
            c
            for c in result
            if needle in c.text.casefold() or needle in c.referenced_text.casefold()
        ]

    if resolved is not None:
        result = [c for c in result if c.resolved is resolved]

    if threads_only:
        # Thread size is computed over the *original* set, not the already
        # filtered one: filtering by author must not make a real thread look
        # like a standalone comment and disappear entirely.
        sizes = Counter(c.root_id for c in comments)
        result = [c for c in result if sizes[c.root_id] > 1]

    return result
