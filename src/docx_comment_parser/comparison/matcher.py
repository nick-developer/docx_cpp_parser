"""Deciding which comment in version 2 *is* which comment in version 1.

Everything a diff can say depends on this question, and nothing in the file
format answers it reliably.  Word reuses ``w:id`` values, renumbers them when
comments are deleted, and rewrites the paragraph ids of anything it touches, so
"same id" is a strong hint and not a fact.  The three levels below are tried in
descending order of certainty; each one only ever looks at the comments the
previous level could not account for.

Level 1 — identity
    Same ``comment_id``, corroborated by a second signal that survives editing:
    an unchanged timestamp, the same author with recognisably similar text, or
    the same anchored document text.  An id match with no corroboration is
    *rejected* and falls through, because a renumbered document would otherwise
    pair a comment with a stranger and report both a spurious edit and no
    add/remove at all — the single worst failure this engine can have.

Level 2 — anchor
    The comment moved, or the id changed, but something about where it sits did
    not: the same author saying the same words, or the same author commenting on
    the same passage of the document.

Level 3 — fuzzy
    Neither the id nor the anchor survived, so the text itself has to carry the
    match.  Scored on the comment body with the anchored text as a secondary
    signal, and accepted only at or above the threshold.

Cost
----
Levels 1 and 2 are dictionary lookups, so a document compared against itself —
the common case — never reaches level 3 at all.  Level 3 is quadratic in what is
left over, which is normally a handful of comments.  When it is not, candidates
are blocked by shared word tokens so the cross-product is never materialised:
see :func:`_fuzzy_pass`.
"""

from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass
from enum import Enum
from typing import Callable, Dict, Iterable, List, Optional, Sequence, Set, Tuple

from ..models import Comment
from .similarity import (
    DEFAULT_THRESHOLD,
    normalise,
    similarity,
    similarity_matrix,
)

__all__ = [
    "MatchLevel",
    "Match",
    "MatchResult",
    "match_comments",
    "DEFAULT_THRESHOLD",
]


class MatchLevel(str, Enum):
    """How a pair of comments was matched, in descending order of certainty."""

    ID = "id"
    ANCHOR = "anchor"
    FUZZY = "fuzzy"


@dataclass(frozen=True)
class Match:
    """One comment from the *before* document paired with its *after* self."""

    before: Comment
    after: Comment
    level: MatchLevel
    score: float
    """Similarity of the two comment bodies, 0.0–1.0.

    This is a property of the pair, not of the confidence in the match: a level
    1 pair whose text was rewritten scores low and is still certainly the same
    comment.  ``level`` carries the confidence.
    """


@dataclass(frozen=True)
class MatchResult:
    """The outcome of matching two documents' comments."""

    matches: Tuple[Match, ...]
    unmatched_before: Tuple[Comment, ...]
    """Comments present only in the *before* document — candidates for removal."""

    unmatched_after: Tuple[Comment, ...]
    """Comments present only in the *after* document — candidates for addition."""

    def by_level(self, level: MatchLevel) -> Tuple[Match, ...]:
        """The matches made at one particular level."""
        return tuple(match for match in self.matches if match.level is level)


# ─────────────────────────────────────────────────────────────────────────────
#  Tunables
# ─────────────────────────────────────────────────────────────────────────────

_ID_CORROBORATION = 0.40
"""How alike two same-id comments' bodies must be to accept the pair on text
alone.  Deliberately generous: a comment can be rewritten wholesale and still be
the same comment, so this is a sanity floor against pairing unrelated text, not
a similarity judgement.  The other corroborating signals (an identical
timestamp, an identical anchor) accept the pair without consulting it at all."""

_BUCKET_LIMIT = 10_000
"""Pairs a single level 2 bucket will rank before falling back to plain
in-order pairing.  A bucket is one author on one exact passage, so real ones
hold a handful of comments; this only ever fires on documents that repeat the
same anchored text hundreds of times."""

_EXHAUSTIVE_LIMIT = 250
"""Residue size below which level 3 compares every remaining pair.

250 × 250 pairs of short strings is a few tens of milliseconds even on the
standard-library back-end, and an exhaustive pass cannot miss a match that
blocking would."""

_TOKEN_PATTERN = re.compile(r"\d+|[^\W_]{3,}", re.UNICODE)
"""Blocking keys: any run of digits, and any word of three or more characters.

Numbers are kept at any length and words are not, which looks backwards until
you read review comments.  "Clause 4.2", "table 12", "line 340" — the number is
almost always the discriminating part, while the words around it are the same
handful every reviewer uses.  Words shorter than three characters carry no
information at all and are dropped."""

_MAX_BLOCK_TOKENS = 8
"""Tokens indexed per comment.  Chosen by the same deterministic rule on both
sides, so two near-identical comments index almost the same set and are certain
to meet in at least one block."""

_MAX_CANDIDATES = 60
"""Candidates level 3 will score for one comment once blocking is in force.

This is the bound that makes the worst case survivable.  Candidates are
collected rarest-token-first, so the ones kept are the most promising ones
available; a comment whose every word is common has no discriminating evidence
to offer anyway, and scoring the whole residue for it would buy an unbounded
amount of work for almost no chance of a match."""

_ANCHOR_WEIGHT = 0.3
"""How much the anchored document text contributes to a level 3 score.

The anchor can only ever *raise* a pair's score (see :func:`_fuzzy_pass`), so
this weight decides how much a shared passage helps, never how much a changed
one hurts.  Revising a document rewrites the passages comments point at — that
is what a revision is — so an anchor that no longer matches is the expected
case, not evidence against the pair."""

_TEXT_WEIGHT = 1.0 - _ANCHOR_WEIGHT
"""The comment's own words carry the rest, and carry a match on their own."""


def _fuzzy_floor(threshold: float) -> float:
    """Lowest text score that could still reach ``threshold`` once the anchor helps.

    Anything below this cannot pass however similar the anchors are, so the
    expensive scoring never has to see it.
    """
    return max(0.0, (threshold - _ANCHOR_WEIGHT) / _TEXT_WEIGHT)


# ─────────────────────────────────────────────────────────────────────────────
#  Level 1 — identity
# ─────────────────────────────────────────────────────────────────────────────


def _same_author(before: Comment, after: Comment) -> bool:
    """Author equality, tolerating the empty author Word sometimes writes."""
    left, right = before.author.casefold(), after.author.casefold()
    return left == right or not left or not right


def _id_pair_is_plausible(before: Comment, after: Comment) -> bool:
    """Decide whether two same-id comments are really the same comment.

    The id alone is not enough (see the module docstring), so a second signal
    has to agree.  In order of cost: an identical raw timestamp — which Word
    never rewrites on an existing comment and which is therefore both the
    strongest and the cheapest signal — then an identical anchor, then text
    similarity.
    """
    if not _same_author(before, after):
        return False
    if before.date and before.date == after.date:
        return True
    if before.referenced_text and before.referenced_text == after.referenced_text:
        return True
    return (
        similarity(normalise(before.text), normalise(after.text))
        >= _ID_CORROBORATION
    )


def _id_pass(
    before: Sequence[Comment], after: Sequence[Comment]
) -> Tuple[List[Match], List[Comment], List[Comment]]:
    """Pair comments sharing a ``comment_id`` where a second signal agrees."""
    pool: Dict[int, List[Comment]] = defaultdict(list)
    for comment in after:
        pool[comment.comment_id].append(comment)

    matches: List[Match] = []
    left_over: List[Comment] = []
    taken: Set[int] = set()

    for candidate in before:
        chosen: Optional[Comment] = None
        for other in pool.get(candidate.comment_id, ()):
            if id(other) in taken:
                continue
            if _id_pair_is_plausible(candidate, other):
                chosen = other
                break
        if chosen is None:
            left_over.append(candidate)
        else:
            taken.add(id(chosen))
            matches.append(
                Match(
                    before=candidate,
                    after=chosen,
                    level=MatchLevel.ID,
                    score=similarity(
                        normalise(candidate.text), normalise(chosen.text)
                    ),
                )
            )

    remaining_after = [comment for comment in after if id(comment) not in taken]
    return matches, left_over, remaining_after


# ─────────────────────────────────────────────────────────────────────────────
#  Level 2 — anchor
# ─────────────────────────────────────────────────────────────────────────────


def _keyed_pass(
    before: Sequence[Comment],
    after: Sequence[Comment],
    key: Callable[[Comment], Optional[Tuple[str, ...]]],
    score_is_exact: bool,
) -> Tuple[List[Match], List[Comment], List[Comment]]:
    """Pair comments that produce the same key, best pair in the bucket first.

    ``key`` returns ``None`` for a comment the pass cannot judge — one with no
    anchored text, for instance — which leaves it untouched for later levels.

    A bucket routinely holds more than one comment on each side: three people
    commenting on the same paragraph is an ordinary review, and so is one person
    leaving three notes on it.  Assignment inside a bucket is therefore ranked
    globally, exactly as level 3 does it — walking the comments in order and
    letting each take its own favourite would let the first one claim a partner
    that fits a later one far better, and neither would ever be reconsidered.
    """
    after_pool: Dict[Tuple[str, ...], List[Comment]] = defaultdict(list)
    for comment in after:
        bucket_key = key(comment)
        if bucket_key is not None:
            after_pool[bucket_key].append(comment)

    before_pool: Dict[Tuple[str, ...], List[Comment]] = defaultdict(list)
    for comment in before:
        bucket_key = key(comment)
        if bucket_key is not None and bucket_key in after_pool:
            before_pool[bucket_key].append(comment)

    matches: List[Match] = []
    taken: Set[int] = set()

    for bucket_key, group_before in before_pool.items():
        group_after = after_pool[bucket_key]

        if score_is_exact or len(group_before) * len(group_after) > _BUCKET_LIMIT:
            # Every pair in an exact-text bucket scores 1.0, so pairing them in
            # order is as good as any assignment and costs nothing.  The size
            # guard falls back to the same cheap pairing rather than let one
            # absurd bucket — a boilerplate passage carrying hundreds of notes —
            # turn a dictionary pass into a quadratic one.
            for candidate, other in zip(group_before, group_after):
                matches.append(
                    Match(
                        before=candidate,
                        after=other,
                        level=MatchLevel.ANCHOR,
                        score=(
                            1.0 if score_is_exact
                            else similarity(
                                normalise(candidate.text), normalise(other.text)
                            )
                        ),
                    )
                )
                taken.add(id(candidate))
                taken.add(id(other))
            continue

        ranked = [
            (
                similarity(normalise(candidate.text), normalise(other.text)),
                left,
                right,
            )
            for left, candidate in enumerate(group_before)
            for right, other in enumerate(group_after)
        ]
        # Highest score first; positions break ties so the outcome does not
        # depend on iteration order.
        ranked.sort(key=lambda item: (-item[0], item[1], item[2]))

        used_before: Set[int] = set()
        used_after: Set[int] = set()
        for score, left, right in ranked:
            if left in used_before or right in used_after:
                continue
            used_before.add(left)
            used_after.add(right)
            matches.append(
                Match(
                    before=group_before[left],
                    after=group_after[right],
                    level=MatchLevel.ANCHOR,
                    score=score,
                )
            )
            taken.add(id(group_before[left]))
            taken.add(id(group_after[right]))

    left_over = [comment for comment in before if id(comment) not in taken]
    remaining_after = [comment for comment in after if id(comment) not in taken]
    return matches, left_over, remaining_after


def _text_key(comment: Comment) -> Optional[Tuple[str, ...]]:
    """Same person, same words — the comment survived a renumbering intact."""
    text = normalise(comment.text)
    if not text:
        return None
    return (comment.author.casefold(), text)


def _anchor_key(comment: Comment) -> Optional[Tuple[str, ...]]:
    """Same person, same passage of the document — the comment was reworded."""
    anchor = normalise(comment.referenced_text)
    if not anchor:
        return None
    return (comment.author.casefold(), anchor)


# ─────────────────────────────────────────────────────────────────────────────
#  Level 3 — fuzzy
# ─────────────────────────────────────────────────────────────────────────────


def _tokens(comment: Comment) -> List[str]:
    """Blocking tokens for one comment: its longest distinct words.

    Numbers first, then longest word first: a number is nearly always the
    distinctive part of a review comment and must never be the token the cap
    discards, and past that a long word is more often distinctive than a short
    one.  The *same* rule runs on both sides, so two near-identical comments
    produce near-identical token sets and are certain to meet in a block.
    """
    found = _TOKEN_PATTERN.findall(comment.text.casefold())
    if not found:
        return []
    unique = sorted(
        set(found),
        key=lambda word: (0 if word[0].isdigit() else 1, -len(word), word),
    )
    return unique[:_MAX_BLOCK_TOKENS]


def _candidate_pairs(
    before: Sequence[Comment], after: Sequence[Comment]
) -> Optional[Dict[int, List[int]]]:
    """Restrict level 3 to promising pairs, or ``None`` to compare them all.

    Returns ``{before_index: [after_index, ...]}``.  Blocking only engages once
    the residue is large enough for the cross-product to matter; below that an
    exhaustive comparison is both faster to set up and strictly more accurate.

    Above it, candidates come from an inverted word index consulted
    *rarest token first*.  That ordering is what makes the bound safe: a word
    shared by five comments narrows the search to five, while one shared by all
    of them narrows nothing, so taking the rare postings first fills the
    per-comment budget with the candidates most likely to contain the real
    partner.  Reviewers reuse vocabulary heavily — "please", "section",
    "clarify" — and an index that treated every word as equally informative
    would put the entire residue in one block and quietly restore the
    cross-product it was built to avoid.
    """
    if len(before) * len(after) <= _EXHAUSTIVE_LIMIT * _EXHAUSTIVE_LIMIT:
        return None

    index: Dict[str, List[int]] = defaultdict(list)
    for position, comment in enumerate(after):
        for token in _tokens(comment):
            index[token].append(position)

    blocks: Dict[int, List[int]] = {}
    for position, comment in enumerate(before):
        tokens = sorted(
            _tokens(comment),
            # Rarest first; the token itself breaks ties so the order — and
            # therefore the result — does not depend on set iteration.
            key=lambda word: (len(index.get(word, ())), word),
        )
        ordered: List[int] = []
        seen: Set[int] = set()
        for token in tokens:
            for candidate in index.get(token, ()):
                if candidate not in seen:
                    seen.add(candidate)
                    ordered.append(candidate)
            if len(ordered) >= _MAX_CANDIDATES:
                break
        if ordered:
            blocks[position] = sorted(ordered[:_MAX_CANDIDATES])
    return blocks


def _fuzzy_pass(
    before: Sequence[Comment],
    after: Sequence[Comment],
    threshold: float,
) -> Tuple[List[Match], List[Comment], List[Comment]]:
    """Pair what is left on text similarity, best pair first.

    The comment body carries the match.  A shared anchor can lift a borderline
    pair over the line — two comments on the same passage really are more likely
    to be the same comment — but a *differing* anchor never pushes one under it,
    which is why the blend is taken as a maximum against the plain text score
    rather than replacing it.  Getting that backwards would reject exactly the
    pairs a diff exists to find: a comment whose wording survived a revision
    that rewrote the passage underneath it.

    Assignment is greedy over globally sorted scores rather than per comment:
    taking the best pair in the whole residue first stops an early mediocre
    match from consuming the partner a later, better one needed.
    """
    if not before or not after:
        return [], list(before), list(after)

    before_text = [normalise(comment.text) for comment in before]
    after_text = [normalise(comment.text) for comment in after]

    floor = _fuzzy_floor(threshold)
    blocks = _candidate_pairs(before, after)
    scored: List[Tuple[float, int, int]] = []

    if blocks is None:
        # Small residue: hand the whole cross-product to the similarity layer,
        # which prunes it with length bounds and, on rapidfuzz, never leaves C.
        for left, right, score in similarity_matrix(
            before_text, after_text, threshold=floor
        ):
            scored.append((score, left, right))
    else:
        for left, candidates in blocks.items():
            # The candidates are the queries and the single comment being placed
            # is the choice, not the other way round: that orientation lets the
            # similarity layer index the one string once and stream the many
            # past it, instead of rebuilding an index per comparison.
            for position, _only, score in similarity_matrix(
                [after_text[right] for right in candidates],
                [before_text[left]],
                threshold=floor,
            ):
                scored.append((score, left, candidates[position]))

    ranked: List[Tuple[float, float, int, int]] = []
    for score, left, right in scored:
        anchor_left = before[left].referenced_text
        anchor_right = after[right].referenced_text
        combined = score
        if anchor_left and anchor_right:
            anchor_score = similarity(normalise(anchor_left), normalise(anchor_right))
            combined = max(score, _TEXT_WEIGHT * score + _ANCHOR_WEIGHT * anchor_score)
        if combined >= threshold:
            ranked.append((combined, score, left, right))

    # Sort by confidence, then by position, so an equal-scoring tie resolves the
    # same way on every run and on every platform.
    ranked.sort(key=lambda item: (-item[0], item[2], item[3]))

    used_before: Set[int] = set()
    used_after: Set[int] = set()
    matches: List[Match] = []
    for _combined, score, left, right in ranked:
        if left in used_before or right in used_after:
            continue
        used_before.add(left)
        used_after.add(right)
        matches.append(
            Match(
                before=before[left],
                after=after[right],
                level=MatchLevel.FUZZY,
                score=score,
            )
        )

    left_over_before = [c for i, c in enumerate(before) if i not in used_before]
    left_over_after = [c for i, c in enumerate(after) if i not in used_after]
    return matches, left_over_before, left_over_after


# ─────────────────────────────────────────────────────────────────────────────
#  Entry point
# ─────────────────────────────────────────────────────────────────────────────


def match_comments(
    before: Iterable[Comment],
    after: Iterable[Comment],
    threshold: float = DEFAULT_THRESHOLD,
    fuzzy: bool = True,
) -> MatchResult:
    """Pair up two versions' comments.

    Parameters
    ----------
    before, after:
        Comment rows from the earlier and later document, typically
        ``parser.to_comments()``.
    threshold:
        Minimum similarity for a level 3 match, 0.0–1.0.  Defaults to
        :data:`~docx_comment_parser.comparison.similarity.DEFAULT_THRESHOLD`
        (0.85).  Levels 1 and 2 are exact and ignore it.
    fuzzy:
        Set False to stop after level 2.  Useful when the two documents are
        known to be renumbered copies of each other and any text-similarity
        match would be a guess.

    Returns
    -------
    MatchResult
        The pairs, plus whatever each side could not account for.  Every input
        comment appears exactly once across the three fields.
    """
    left = list(before)
    right = list(after)

    matches: List[Match] = []

    id_matches, left, right = _id_pass(left, right)
    matches.extend(id_matches)

    text_matches, left, right = _keyed_pass(left, right, _text_key, score_is_exact=True)
    matches.extend(text_matches)

    anchor_matches, left, right = _keyed_pass(
        left, right, _anchor_key, score_is_exact=False
    )
    matches.extend(anchor_matches)

    if fuzzy:
        fuzzy_matches, left, right = _fuzzy_pass(left, right, threshold)
        matches.extend(fuzzy_matches)

    # Report in the before-document's own order, which is the order whoever
    # read the first review already has in their head.  The passes above run in
    # confidence order, not document order, so this is where that is undone.
    matches.sort(
        key=lambda match: (match.before.document_name, match.before.comment_id)
    )

    return MatchResult(
        matches=tuple(matches),
        unmatched_before=tuple(left),
        unmatched_after=tuple(right),
    )
