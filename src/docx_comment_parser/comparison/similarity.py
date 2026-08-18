"""Text similarity, with an optional accelerated back-end.

Matching two review cycles means asking "is this the same comment, slightly
edited?" thousands of times, so the scoring function is the hot path of the whole
diff engine.  Two implementations answer it:

``rapidfuzz``
    A C++ extension.  Roughly an order of magnitude faster, and what the roadmap
    names.  Installed by the ``diff`` extra.

``difflib``
    The standard library.  Always present, so ``compare_comments`` works on a
    bare ``pip install docx-comment-parser`` exactly like CSV, JSON and the
    Markdown report do.

The back-end is chosen once, on first use, and never changes the *shape* of the
answer: both return a similarity in ``0.0``–``1.0`` where 1.0 is identical text.
Absolute scores can differ by a percent or two between them — they are different
algorithms — so nothing in this package compares a score against a value another
back-end produced, and the default threshold sits far from any boundary that a
percent could flip.

Two properties make the difflib path viable rather than merely present:

* an exact upper bound on the ratio, ``2·min(len)/(len(a)+len(b))``, rejects
  most candidate pairs with one integer comparison and no allocation;
* ``SequenceMatcher`` caches the sequence given to ``set_seq2``, so scoring one
  query against many candidates re-uses a single b-side index instead of
  rebuilding it per pair.
"""

from __future__ import annotations

from difflib import SequenceMatcher
from typing import Any, List, Optional, Sequence, Tuple

__all__ = [
    "DEFAULT_THRESHOLD",
    "backend",
    "normalise",
    "similarity",
    "best_match",
    "similarity_matrix",
]

DEFAULT_THRESHOLD = 0.85
"""The roadmap's fuzzy-matching threshold.

High enough that a reworded comment is a new comment rather than an edit of an
old one, low enough to absorb the typo fixes and re-punctuation that dominate
real second-pass reviews.
"""

# The back-end is resolved lazily and memoised.  ``_probed`` is separate from
# ``_fuzz`` because "not yet looked" and "looked, absent" are different states
# and the second must not re-attempt the import on every call.
_fuzz: Optional[Any] = None
_probed = False


def _rapidfuzz() -> Optional[Any]:
    """Return ``rapidfuzz.fuzz`` if it is installed, else ``None``."""
    global _fuzz, _probed
    if not _probed:
        try:
            from rapidfuzz import fuzz
        except ImportError:
            _fuzz = None
        else:
            _fuzz = fuzz
        _probed = True
    return _fuzz


def backend() -> str:
    """Name the similarity implementation in use: ``rapidfuzz`` or ``difflib``.

    Reported in every diff result so a surprising match can be traced back to
    the engine that produced it.
    """
    return "rapidfuzz" if _rapidfuzz() is not None else "difflib"


def normalise(text: str) -> str:
    """Fold a comment down to what a reader would call "the same text".

    Whitespace runs collapse to one space and case is folded, so re-wrapping a
    comment in Word — which rewrites the runs and therefore the extracted
    whitespace — does not read as an edit.  Nothing else is stripped:
    punctuation carries meaning in a review comment ("delete?" is not "delete").
    """
    return " ".join(text.split()).casefold()


def _bound(a: str, b: str) -> float:
    """Exact upper bound on the similarity of two strings, from lengths alone.

    Every matching-block algorithm here scores ``2·M/T`` for ``M`` matched
    characters out of ``T`` total, and ``M`` cannot exceed the shorter string.
    A pair failing this bound cannot pass any threshold, so it is discarded
    before a single character is compared.
    """
    la, lb = len(a), len(b)
    total = la + lb
    if total == 0:
        return 1.0
    return 2.0 * min(la, lb) / total


def similarity(a: str, b: str) -> float:
    """Return how alike two strings are, from 0.0 to 1.0.

    Inputs are compared as given; call :func:`normalise` first if whitespace and
    case should not count as a difference.
    """
    if a == b:
        return 1.0
    if not a or not b:
        return 0.0

    fuzz = _rapidfuzz()
    if fuzz is not None:
        score: float = fuzz.ratio(a, b) / 100.0
        return score

    # autojunk heuristically ignores characters appearing in more than 1% of a
    # sequence longer than 200 elements.  On prose that is the common letters,
    # which silently deflates the score of exactly the long comments this engine
    # most needs to judge accurately.
    return SequenceMatcher(None, a, b, autojunk=False).ratio()


def best_match(
    query: str,
    choices: Sequence[str],
    threshold: float = DEFAULT_THRESHOLD,
) -> Optional[Tuple[int, float]]:
    """Find the choice most similar to ``query``.

    Returns ``(index, score)`` for the best choice scoring at or above
    ``threshold``, or ``None`` when nothing reaches it.  Ties go to the earliest
    index, so the result does not depend on iteration order.
    """
    if not choices:
        return None

    fuzz = _rapidfuzz()
    if fuzz is not None:
        from rapidfuzz import process

        hit = process.extractOne(
            query,
            choices,
            scorer=fuzz.ratio,
            score_cutoff=threshold * 100.0,
        )
        if hit is None:
            return None
        return int(hit[2]), float(hit[1]) / 100.0

    # difflib: index the query once as the b-side, then stream the candidates
    # past it.  quick_ratio and real_quick_ratio are documented upper bounds on
    # ratio, so raising the floor as better candidates appear turns most of the
    # remaining work into two integer comparisons.
    matcher = SequenceMatcher(None, "", query, autojunk=False)
    best_index = -1
    floor = threshold
    for index, choice in enumerate(choices):
        if _bound(query, choice) < floor:
            continue
        matcher.set_seq1(choice)
        if matcher.real_quick_ratio() < floor or matcher.quick_ratio() < floor:
            continue
        score = matcher.ratio()
        if score >= floor:
            # Strictly greater keeps the earliest index on a tie.
            if best_index == -1 or score > floor:
                best_index = index
                floor = score
    if best_index == -1:
        return None
    return best_index, floor


def similarity_matrix(
    queries: Sequence[str],
    choices: Sequence[str],
    threshold: float = DEFAULT_THRESHOLD,
) -> List[Tuple[int, int, float]]:
    """Score every query against every choice, keeping pairs at or above ``threshold``.

    Returned as ``(query_index, choice_index, score)`` triples.  Pairs below the
    threshold are dropped rather than returned as zeros: the caller only ever
    wants the candidates, and a dense matrix of a residue times a residue is the
    one allocation this engine cannot afford.
    """
    pairs: List[Tuple[int, int, float]] = []
    if not queries or not choices:
        return pairs

    fuzz = _rapidfuzz()
    if fuzz is not None:
        from rapidfuzz import process

        # cdist runs the whole cross-product in C, with the cutoff applied
        # inside the loop so sub-threshold pairs never cross into Python.
        scores = process.cdist(
            queries,
            choices,
            scorer=fuzz.ratio,
            score_cutoff=threshold * 100.0,
            workers=1,
        )
        for query_index, row in enumerate(scores):
            for choice_index, value in enumerate(row):
                if value:
                    pairs.append((query_index, choice_index, float(value) / 100.0))
        return pairs

    matcher = SequenceMatcher(None, "", "", autojunk=False)
    for choice_index, choice in enumerate(choices):
        matcher.set_seq2(choice)
        for query_index, query in enumerate(queries):
            if _bound(query, choice) < threshold:
                continue
            matcher.set_seq1(query)
            if matcher.real_quick_ratio() < threshold:
                continue
            if matcher.quick_ratio() < threshold:
                continue
            score = matcher.ratio()
            if score >= threshold:
                pairs.append((query_index, choice_index, score))
    return pairs
