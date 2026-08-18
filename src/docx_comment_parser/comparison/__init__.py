"""Comparing two review cycles.

A document goes out for review, comes back with comments, gets revised, and goes
out again.  The question at that point is never "what comments are in this file"
— it is "what happened since last time".  This package answers it::

    from docx_comment_parser import compare_comments

    result = compare_comments("review_v1.docx", "review_v2.docx")

    print(result.summary())
    result.added        # comments that are new in v2
    result.removed      # comments that are gone
    result.resolved     # comments that were open and are now closed
    result.reopened     # comments that were closed and are open again

    result.export_html("what_changed.html")

Three modules, in the order the work happens:

:mod:`.similarity`
    How alike two pieces of text are.  Uses ``rapidfuzz`` when the ``diff``
    extra is installed and the standard library's ``difflib`` otherwise, so
    comparing works on a bare install.

:mod:`.matcher`
    Which comment in the newer document *is* which comment in the older one —
    by id, then by anchor, then by text similarity.

:mod:`.diff_engine`
    What changed, how fast the review is converging, and which threads still
    carry risk.

:mod:`.diff_report` renders the result: a terminal summary, CSV, JSON, a
DataFrame, a Markdown digest, or a single self-contained HTML page.

Importing ``docx_comment_parser`` does not import any of this — the package is
resolved on first use, so a program that only parses documents pays nothing for
its existence.
"""

from __future__ import annotations

from .diff_engine import (
    ChangeKind,
    CommentChange,
    DiffResult,
    Hotspot,
    ReviewVelocity,
    compare_comments,
    diff_comments,
)
from .matcher import Match, MatchLevel, MatchResult, match_comments

# Deliberately *not* re-exporting the scoring function itself.  It is called
# ``similarity`` and so is its module, and binding the function here would
# shadow the module on this package — making ``comparison.similarity.backend()``
# an AttributeError for anyone who imported the package first.  The module keeps
# the name; the function is one import away::
#
#     from docx_comment_parser.comparison.similarity import similarity
from .similarity import DEFAULT_THRESHOLD, backend, normalise

__all__ = [
    # the one function most callers need
    "compare_comments",
    "diff_comments",
    # results
    "DiffResult",
    "CommentChange",
    "ChangeKind",
    "ReviewVelocity",
    "Hotspot",
    # matching
    "match_comments",
    "MatchResult",
    "Match",
    "MatchLevel",
    # similarity
    "normalise",
    "backend",
    "DEFAULT_THRESHOLD",
]
