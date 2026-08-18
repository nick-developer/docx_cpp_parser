"""Tests for the v1.4 comparison layer: similarity, matching, diffing, reports.

The matching tests are the important ones.  A diff engine that quietly pairs the
wrong two comments produces output that looks entirely reasonable and is wrong
in every column, so each level of the matching strategy is exercised in
isolation, with documents built to defeat the level above it: renumbered ids to
force anchor matching, reworded text plus renumbered ids to force fuzzy
matching, and — the case that matters most — a document where the ids line up
but the comments behind them are strangers.

Both similarity back-ends are exercised.  rapidfuzz and difflib are different
algorithms and their scores differ slightly, so nothing here asserts a score
another back-end produced; what is asserted is that both reach the same
*decisions*, which is the actual promise.
"""

from __future__ import annotations

import importlib.util
import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterator, List

import pytest

import docx_comment_parser as dcp
from docx_comment_parser.comparison import (
    ChangeKind,
    DiffResult,
    MatchLevel,
    compare_comments,
    diff_comments,
    match_comments,
)
from docx_comment_parser.comparison import diff_report, similarity as sim
from docx_comment_parser.models import Comment

from conftest import CommentSpec, build_docx, make_specs

FIXED_TIME = datetime(2026, 5, 1, 12, 0, tzinfo=timezone.utc)


# ─────────────────────────────────────────────────────────────────────────────
#  Fixtures
# ─────────────────────────────────────────────────────────────────────────────


def comments_of(path: Path) -> List[Comment]:
    parser = dcp.DocxParser()
    parser.parse(str(path))
    return parser.to_comments()


def docx_of(directory: Path, name: str, specs: List[CommentSpec]) -> List[Comment]:
    """Build a document from ``specs`` and return its comment rows."""
    return comments_of(build_docx(directory / name, specs))


#: A first review round: four comments, one thread, one already resolved.
V1_SPECS = [
    CommentSpec(
        id=0, author="Alice Tester", initials="AT", date="2026-03-01T09:00:00Z",
        text="This paragraph needs a clearer opening sentence.",
        anchor="The methodology employed in this study.",
    ),
    CommentSpec(
        id=1, author="Bob Reviewer", initials="BR", date="2026-03-02T10:00:00Z",
        text="Agreed.", parent=0,
    ),
    CommentSpec(
        id=2, author="Carol Editor", initials="CE", date="2026-03-03T11:00:00Z",
        text="Check the statistics quoted in section three.", done=True,
        anchor="Results show p < 0.05.",
    ),
    CommentSpec(
        id=3, author="Alice Tester", initials="AT", date="2026-03-04T12:00:00Z",
        text="Clause 4.2 still needs legal sign-off.",
        anchor="Clause 4.2 of the agreement.",
    ),
]

#: The second round.  Comment 0 was resolved, 1 was reworded *and* resolved,
#: 2 was re-opened, 3 was deleted, and 4 is new.
V2_SPECS = [
    CommentSpec(
        id=0, author="Alice Tester", initials="AT", date="2026-03-01T09:00:00Z",
        text="This paragraph needs a clearer opening sentence.", done=True,
        anchor="The methodology employed in this study.",
    ),
    CommentSpec(
        id=1, author="Bob Reviewer", initials="BR", date="2026-03-02T10:00:00Z",
        text="Agreed, rewritten in the latest draft.", parent=0, done=True,
    ),
    CommentSpec(
        id=2, author="Carol Editor", initials="CE", date="2026-03-03T11:00:00Z",
        text="Check the statistics quoted in section three.",
        anchor="Results show p < 0.05.",
    ),
    CommentSpec(
        id=4, author="Dan Legal", initials="DL", date="2026-03-20T14:00:00Z",
        text="New concern about the security annexe.",
        anchor="Annexe B covers security.",
    ),
]


@pytest.fixture(scope="module")
def cycle(tmp_path_factory: pytest.TempPathFactory) -> DiffResult:
    """The canonical two-round comparison every classification test uses."""
    directory = tmp_path_factory.mktemp("cycle")
    return compare_comments(
        str(build_docx(directory / "v1.docx", V1_SPECS)),
        str(build_docx(directory / "v2.docx", V2_SPECS)),
        generated_at=FIXED_TIME,
    )


@pytest.fixture
def both_backends(request: pytest.FixtureRequest) -> Iterator[str]:
    """Run a test once per available similarity back-end.

    The module memoises its choice, so forcing difflib means clearing that cache
    and restoring it afterwards — otherwise the override would leak into every
    test that ran later in the session.
    """
    wanted = request.param
    if wanted == "rapidfuzz":
        pytest.importorskip("rapidfuzz")

    previous_fuzz, previous_probed = sim._fuzz, sim._probed
    if wanted == "difflib":
        sim._fuzz, sim._probed = None, True
    else:
        sim._fuzz, sim._probed = None, False
    try:
        assert sim.backend() == wanted
        yield wanted
    finally:
        sim._fuzz, sim._probed = previous_fuzz, previous_probed


BACKENDS = pytest.mark.parametrize(
    "both_backends", ["difflib", "rapidfuzz"], indirect=True
)


# ═════════════════════════════════════════════════════════════════════════════
#  Similarity
# ═════════════════════════════════════════════════════════════════════════════


@BACKENDS
def test_identical_text_scores_one(both_backends: str) -> None:
    assert sim.similarity("needs a rewrite", "needs a rewrite") == 1.0


@BACKENDS
def test_unrelated_text_scores_low(both_backends: str) -> None:
    assert sim.similarity("needs a rewrite", "the budget is approved") < 0.5


@BACKENDS
def test_a_typo_fix_stays_above_the_threshold(both_backends: str) -> None:
    """The case the default threshold exists for: 0.85 must not call this new."""
    score = sim.similarity(
        "this sentance needs rephrasing for clarity",
        "this sentence needs rephrasing for clarity",
    )
    assert score >= sim.DEFAULT_THRESHOLD


@BACKENDS
def test_empty_input_is_handled(both_backends: str) -> None:
    assert sim.similarity("", "") == 1.0
    assert sim.similarity("", "anything") == 0.0
    assert sim.similarity("anything", "") == 0.0


def test_normalise_folds_whitespace_and_case() -> None:
    assert sim.normalise("  Needs\n a   REWRITE ") == "needs a rewrite"


def test_normalise_keeps_punctuation() -> None:
    """"delete?" is not "delete" — punctuation carries meaning in a review."""
    assert sim.normalise("Delete?") != sim.normalise("Delete")


@BACKENDS
def test_best_match_finds_the_closest_choice(both_backends: str) -> None:
    hit = sim.best_match("needs revision", ["unrelated text", "needs revisions"])
    assert hit is not None
    index, score = hit
    assert index == 1
    assert score >= sim.DEFAULT_THRESHOLD


@BACKENDS
def test_best_match_returns_none_below_the_threshold(both_backends: str) -> None:
    assert sim.best_match("needs revision", ["completely unrelated"]) is None


@BACKENDS
def test_best_match_on_no_choices(both_backends: str) -> None:
    assert sim.best_match("anything", []) is None


@BACKENDS
def test_similarity_matrix_keeps_only_passing_pairs(both_backends: str) -> None:
    pairs = sim.similarity_matrix(
        ["needs revision", "budget approved"],
        ["needs revisions", "totally different"],
    )
    assert [(left, right) for left, right, _score in pairs] == [(0, 0)]


@BACKENDS
def test_similarity_matrix_on_empty_input(both_backends: str) -> None:
    assert sim.similarity_matrix([], ["anything"]) == []
    assert sim.similarity_matrix(["anything"], []) == []


def test_backend_names_itself() -> None:
    assert sim.backend() in {"rapidfuzz", "difflib"}


# ═════════════════════════════════════════════════════════════════════════════
#  Matching — level 1, identity
# ═════════════════════════════════════════════════════════════════════════════


def test_untouched_document_matches_entirely_by_id(tmp_path: Path) -> None:
    rows = docx_of(tmp_path, "a.docx", V1_SPECS)
    result = match_comments(rows, rows)

    assert len(result.matches) == len(rows)
    assert not result.unmatched_before
    assert not result.unmatched_after
    assert {match.level for match in result.matches} == {MatchLevel.ID}


def test_an_edited_comment_still_matches_by_id(tmp_path: Path) -> None:
    before = docx_of(tmp_path, "a.docx", V1_SPECS)
    after = docx_of(tmp_path, "b.docx", V2_SPECS)
    result = match_comments(before, after)

    pair = next(m for m in result.matches if m.before.comment_id == 1)
    assert pair.level is MatchLevel.ID
    assert pair.after.text == "Agreed, rewritten in the latest draft."


def test_an_id_reused_by_a_different_comment_is_not_a_match(tmp_path: Path) -> None:
    """The failure mode that would poison every column of the output.

    A document where id 0 belongs to a completely different comment by a
    different person must be reported as one removal and one addition, never as
    a heavily edited comment.
    """
    before = docx_of(tmp_path, "a.docx", [
        CommentSpec(id=0, author="Alice", date="2026-01-01T09:00:00Z",
                    text="Please rewrite the opening paragraph."),
    ])
    after = docx_of(tmp_path, "b.docx", [
        CommentSpec(id=0, author="Zoe", date="2026-06-01T09:00:00Z",
                    text="The budget figures in the appendix are approved."),
    ])
    result = match_comments(before, after)

    assert result.matches == ()
    assert len(result.unmatched_before) == 1
    assert len(result.unmatched_after) == 1


def test_a_rewritten_comment_by_the_same_author_keeps_its_id_match(
    tmp_path: Path,
) -> None:
    """An unchanged timestamp is enough on its own — Word never rewrites one."""
    before = docx_of(tmp_path, "a.docx", [
        CommentSpec(id=0, author="Alice", date="2026-01-01T09:00:00Z",
                    text="Please rewrite the opening paragraph."),
    ])
    after = docx_of(tmp_path, "b.docx", [
        CommentSpec(id=0, author="Alice", date="2026-01-01T09:00:00Z",
                    text="Completely different wording now, nothing in common."),
    ])
    result = match_comments(before, after)

    assert len(result.matches) == 1
    assert result.matches[0].level is MatchLevel.ID


# ═════════════════════════════════════════════════════════════════════════════
#  Matching — level 2, anchor
# ═════════════════════════════════════════════════════════════════════════════


def test_renumbered_ids_still_match_on_text_and_author(tmp_path: Path) -> None:
    """Word renumbers comments when earlier ones are deleted."""
    before = docx_of(tmp_path, "a.docx", V1_SPECS)
    renumbered = [
        CommentSpec(**{**spec.__dict__, "id": spec.id + 500,
                       "parent": None if spec.parent is None else spec.parent + 500})
        for spec in V1_SPECS
    ]
    after = docx_of(tmp_path, "b.docx", renumbered)

    result = match_comments(before, after)
    assert len(result.matches) == len(before)
    assert {match.level for match in result.matches} == {MatchLevel.ANCHOR}


def test_a_reworded_comment_matches_on_its_anchor(tmp_path: Path) -> None:
    """Same person, same passage, different words — level 2's real job."""
    before = docx_of(tmp_path, "a.docx", [
        CommentSpec(id=0, author="Alice", date="2026-01-01T09:00:00Z",
                    text="Please rewrite this.", anchor="The methodology section."),
    ])
    after = docx_of(tmp_path, "b.docx", [
        CommentSpec(id=7, author="Alice", date="2026-02-02T09:00:00Z",
                    text="Needs a total overhaul, not a tweak.",
                    anchor="The methodology section."),
    ])
    result = match_comments(before, after)

    assert len(result.matches) == 1
    assert result.matches[0].level is MatchLevel.ANCHOR


def test_an_unchanged_anchor_corroborates_an_id_match(tmp_path: Path) -> None:
    """When the timestamp moved, the anchored text is the next signal tried."""
    before = docx_of(tmp_path, "a.docx", [
        CommentSpec(id=0, author="Alice", date="2026-01-01T09:00:00Z",
                    text="Rewrite this paragraph.", anchor="The methodology section."),
    ])
    after = docx_of(tmp_path, "b.docx", [
        CommentSpec(id=0, author="Alice", date="2026-09-09T09:00:00Z",
                    text="Nothing whatsoever in common with the previous wording.",
                    anchor="The methodology section."),
    ])
    result = match_comments(before, after)
    assert len(result.matches) == 1
    assert result.matches[0].level is MatchLevel.ID


def test_two_comments_competing_for_one_anchor(tmp_path: Path) -> None:
    """A bucket can run out of partners; the loser must fall through, not pair up."""
    before = docx_of(tmp_path, "a.docx", [
        CommentSpec(id=0, author="Alice", date="2026-01-01T09:00:00Z",
                    text="First note here.", anchor="Shared passage."),
        CommentSpec(id=1, author="Alice", date="2026-01-02T09:00:00Z",
                    text="Second note here.", anchor="Shared passage."),
    ])
    after = docx_of(tmp_path, "b.docx", [
        CommentSpec(id=90, author="Alice", date="2026-02-01T09:00:00Z",
                    text="Second note here, revised.", anchor="Shared passage."),
    ])
    result = match_comments(before, after, fuzzy=False)

    assert len(result.matches) == 1
    # The closer of the two competitors wins the single available partner.
    assert result.matches[0].before.comment_id == 1
    assert [c.comment_id for c in result.unmatched_before] == [0]


def test_a_comment_with_no_usable_text_is_still_accounted_for(tmp_path: Path) -> None:
    """Empty and punctuation-only comments have no key and no blocking tokens."""
    before = docx_of(tmp_path, "a.docx", [
        CommentSpec(id=0, author="Alice", date="2026-01-01T09:00:00Z", text="?!"),
    ])
    after = docx_of(tmp_path, "b.docx", [
        CommentSpec(id=77, author="Bob", date="2026-02-02T09:00:00Z", text="   "),
    ])
    result = match_comments(before, after)
    assert len(result.unmatched_before) + len(result.matches) == 1
    assert len(result.unmatched_after) + len(result.matches) == 1


def test_a_comment_with_no_anchor_falls_past_level_two(tmp_path: Path) -> None:
    before = docx_of(tmp_path, "a.docx", [
        CommentSpec(id=0, author="Alice", date="2026-01-01T09:00:00Z", text="One."),
    ])
    after = docx_of(tmp_path, "b.docx", [
        CommentSpec(id=9, author="Bob", date="2026-02-02T09:00:00Z", text="Two."),
    ])
    result = match_comments(before, after)
    assert result.matches == ()


# ═════════════════════════════════════════════════════════════════════════════
#  Matching — level 3, fuzzy
# ═════════════════════════════════════════════════════════════════════════════


@BACKENDS
def test_fuzzy_matches_a_typo_fix_across_a_renumbering(
    tmp_path: Path, both_backends: str
) -> None:
    before = docx_of(tmp_path, f"a-{both_backends}.docx", [
        CommentSpec(id=0, author="Alice", date="2026-01-01T09:00:00Z",
                    text="This sentance needs rephrasing for clarity.",
                    anchor="Original passage."),
    ])
    after = docx_of(tmp_path, f"b-{both_backends}.docx", [
        CommentSpec(id=41, author="Alice", date="2026-02-02T09:00:00Z",
                    text="This sentence needs rephrasing for clarity.",
                    anchor="Rewritten passage."),
    ])
    result = match_comments(before, after)

    assert len(result.matches) == 1
    assert result.matches[0].level is MatchLevel.FUZZY
    assert result.matches[0].score >= sim.DEFAULT_THRESHOLD


@BACKENDS
def test_fuzzy_refuses_a_pair_below_the_threshold(
    tmp_path: Path, both_backends: str
) -> None:
    before = docx_of(tmp_path, f"c-{both_backends}.docx", [
        CommentSpec(id=0, author="Alice", date="2026-01-01T09:00:00Z",
                    text="Rewrite the opening paragraph please.", anchor="One."),
    ])
    after = docx_of(tmp_path, f"d-{both_backends}.docx", [
        CommentSpec(id=41, author="Alice", date="2026-02-02T09:00:00Z",
                    text="The appendix budget figures are approved.", anchor="Two."),
    ])
    assert match_comments(before, after).matches == ()


@BACKENDS
def test_a_rewritten_anchor_does_not_block_a_fuzzy_match(
    tmp_path: Path, both_backends: str
) -> None:
    """The anchor may only raise a score, never lower one.

    Revising a document rewrites the passages its comments point at — that is
    what a revision is.  Weighting a changed anchor *against* the pair would
    reject exactly the comments a second-round diff exists to find.
    """
    before = docx_of(tmp_path, f"g-{both_backends}.docx", [
        CommentSpec(id=0, author="Alice", date="2026-01-01T09:00:00Z",
                    text="This sentance needs rephrasing for clarity.",
                    anchor="The methodology employed in this study is flawed."),
    ])
    after = docx_of(tmp_path, f"h-{both_backends}.docx", [
        CommentSpec(id=77, author="Alice", date="2026-02-02T09:00:00Z",
                    text="This sentence needs rephrasing for clarity.",
                    anchor="Nothing whatsoever in common with the old passage."),
    ])
    result = match_comments(before, after)

    assert len(result.matches) == 1
    assert result.matches[0].level is MatchLevel.FUZZY


def test_fuzzy_can_be_turned_off(tmp_path: Path) -> None:
    before = docx_of(tmp_path, "a.docx", [
        CommentSpec(id=0, author="Alice", date="2026-01-01T09:00:00Z",
                    text="This sentance needs rephrasing.", anchor="One."),
    ])
    after = docx_of(tmp_path, "b.docx", [
        CommentSpec(id=41, author="Alice", date="2026-02-02T09:00:00Z",
                    text="This sentence needs rephrasing.", anchor="Two."),
    ])
    assert len(match_comments(before, after, fuzzy=True).matches) == 1
    assert match_comments(before, after, fuzzy=False).matches == ()


def test_the_threshold_is_respected(tmp_path: Path) -> None:
    before = docx_of(tmp_path, "a.docx", [
        CommentSpec(id=0, author="Alice", date="2026-01-01T09:00:00Z",
                    text="Rewrite this opening paragraph for clarity.", anchor="One."),
    ])
    after = docx_of(tmp_path, "b.docx", [
        CommentSpec(id=41, author="Alice", date="2026-02-02T09:00:00Z",
                    text="Rewrite the whole opening section for brevity.", anchor="Two."),
    ])
    assert match_comments(before, after, threshold=0.99).matches == ()
    assert len(match_comments(before, after, threshold=0.5).matches) == 1


@BACKENDS
def test_greedy_assignment_picks_the_best_pair_first(
    tmp_path: Path, both_backends: str
) -> None:
    """Two candidates competing for one partner: the closer one must win.

    Assignment is over globally sorted scores, so an earlier mediocre pair
    cannot consume the partner a later, better pair needed.
    """
    before = docx_of(tmp_path, f"e-{both_backends}.docx", [
        CommentSpec(id=0, author="Alice", date="2026-01-01T09:00:00Z",
                    text="Check the numbers in table four carefully.", anchor="A"),
        CommentSpec(id=1, author="Alice", date="2026-01-01T10:00:00Z",
                    text="Check the numbers in table five carefully.", anchor="B"),
    ])
    after = docx_of(tmp_path, f"f-{both_backends}.docx", [
        CommentSpec(id=90, author="Alice", date="2026-02-01T10:00:00Z",
                    text="Check the numbers in table five carefully!", anchor="D"),
        CommentSpec(id=91, author="Alice", date="2026-02-01T09:00:00Z",
                    text="Check the numbers in table four carefully!", anchor="C"),
    ])
    result = match_comments(before, after)

    pairs = {m.before.comment_id: m.after.comment_id for m in result.matches}
    assert pairs == {0: 91, 1: 90}


def test_every_comment_is_accounted_for_exactly_once(tmp_path: Path) -> None:
    """The invariant the whole engine rests on."""
    before = docx_of(tmp_path, "a.docx", make_specs(60, seed=4))
    after = docx_of(tmp_path, "b.docx", make_specs(70, seed=5))
    result = match_comments(before, after)

    seen_before = [m.before for m in result.matches] + list(result.unmatched_before)
    seen_after = [m.after for m in result.matches] + list(result.unmatched_after)
    assert len(seen_before) == len(before)
    assert len(seen_after) == len(after)
    assert {id(c) for c in seen_before} == {id(c) for c in before}
    assert {id(c) for c in seen_after} == {id(c) for c in after}


def test_matching_is_deterministic(tmp_path: Path) -> None:
    before = docx_of(tmp_path, "a.docx", make_specs(40, seed=11))
    after = docx_of(tmp_path, "b.docx", make_specs(45, seed=12))

    first = match_comments(before, after)
    second = match_comments(before, after)
    assert [(m.before.comment_id, m.after.comment_id, m.level) for m in first.matches] == [
        (m.before.comment_id, m.after.comment_id, m.level) for m in second.matches
    ]


def test_by_level_filters_matches(tmp_path: Path) -> None:
    rows = docx_of(tmp_path, "a.docx", V1_SPECS)
    result = match_comments(rows, rows)
    assert len(result.by_level(MatchLevel.ID)) == len(rows)
    assert result.by_level(MatchLevel.FUZZY) == ()


def test_matching_empty_documents(tmp_path: Path) -> None:
    rows = docx_of(tmp_path, "a.docx", V1_SPECS)
    assert match_comments([], []).matches == ()
    assert len(match_comments(rows, []).unmatched_before) == len(rows)
    assert len(match_comments([], rows).unmatched_after) == len(rows)


def test_blocking_engages_on_a_large_residue(tmp_path: Path) -> None:
    """Past the exhaustive limit, level 3 blocks candidates by shared words.

    Built so that no id and no anchor lines up, which forces every comment
    through level 3, and large enough that the blocking path is the one taken.
    """
    before_specs = [
        CommentSpec(id=i, author="Alice", date="2026-01-01T09:00:00Z",
                    text=f"Recommendation {i}: revise the vocabulary paragraph thoroughly.",
                    anchor=f"passage {i}")
        for i in range(300)
    ]
    after_specs = [
        CommentSpec(id=i + 10_000, author="Alice", date="2026-02-01T09:00:00Z",
                    text=f"Recommendation {i}: revise the vocabulary paragraph thoroughly!",
                    anchor=f"passage {i} rewritten")
        for i in range(300)
    ]
    before = docx_of(tmp_path, "a.docx", before_specs)
    after = docx_of(tmp_path, "b.docx", after_specs)

    result = match_comments(before, after)
    assert {m.level for m in result.matches} == {MatchLevel.FUZZY}
    # Blocking must not cost accuracy: every comment finds its own counterpart.
    assert len(result.matches) == 300
    for match in result.matches:
        assert match.after.comment_id == match.before.comment_id + 10_000


# ═════════════════════════════════════════════════════════════════════════════
#  Diff classification
# ═════════════════════════════════════════════════════════════════════════════


def test_added_comments(cycle: DiffResult) -> None:
    assert [c.comment.author for c in cycle.added] == ["Dan Legal"]


def test_removed_comments(cycle: DiffResult) -> None:
    assert [c.comment.comment_id for c in cycle.removed] == [3]


def test_resolved_comments(cycle: DiffResult) -> None:
    assert sorted(c.comment.comment_id for c in cycle.resolved) == [0, 1]


def test_reopened_comments(cycle: DiffResult) -> None:
    assert [c.comment.comment_id for c in cycle.reopened] == [2]


def test_edited_comments(cycle: DiffResult) -> None:
    edited = cycle.edited
    assert [c.comment.comment_id for c in edited] == [1]
    assert edited[0].before is not None and edited[0].before.text == "Agreed."


def test_a_comment_can_be_both_resolved_and_edited(cycle: DiffResult) -> None:
    """Facets overlap on purpose; a single label would misreport one of them."""
    change = next(c for c in cycle.changes if c.comment_id == 1)
    assert change.has(ChangeKind.RESOLVED)
    assert change.has(ChangeKind.EDITED)
    assert change.kind is ChangeKind.RESOLVED  # status leads the headline


def test_every_comment_appears_exactly_once_in_changes(cycle: DiffResult) -> None:
    assert len(cycle.changes) == len(V1_SPECS) + 1  # four matched-or-removed, one new
    assert len(cycle.matched) == 3


def test_identical_documents_report_no_changes(tmp_path: Path) -> None:
    rows = docx_of(tmp_path, "a.docx", V1_SPECS)
    result = diff_comments(rows, rows)

    assert not result.has_changes
    assert len(result.unchanged) == len(rows)
    assert result.added == () and result.removed == ()


def test_reanchoring_is_reported(tmp_path: Path) -> None:
    before = docx_of(tmp_path, "a.docx", [
        CommentSpec(id=0, author="Alice", date="2026-01-01T09:00:00Z",
                    text="Fix this.", anchor="The original passage."),
    ])
    after = docx_of(tmp_path, "b.docx", [
        CommentSpec(id=0, author="Alice", date="2026-01-01T09:00:00Z",
                    text="Fix this.", anchor="The rewritten passage."),
    ])
    result = diff_comments(before, after)
    assert result.reanchored and not result.edited


def test_whitespace_only_rewrapping_is_not_an_edit(tmp_path: Path) -> None:
    """Word rewrites runs when a comment is re-wrapped; that is not a change."""
    before = docx_of(tmp_path, "a.docx", [
        CommentSpec(id=0, author="Alice", date="2026-01-01T09:00:00Z",
                    text="Needs a rewrite here."),
    ])
    after = docx_of(tmp_path, "b.docx", [
        CommentSpec(id=0, author="Alice", date="2026-01-01T09:00:00Z",
                    text="Needs   a\n rewrite here."),
    ])
    assert diff_comments(before, after).edited == ()


def test_diff_of_two_empty_documents(tmp_path: Path) -> None:
    empty = docx_of(tmp_path, "e.docx", [])
    result = diff_comments(empty, empty)
    assert result.changes == ()
    assert result.velocity.comments_before == 0
    assert result.velocity.resolution_rate_before == 0.0
    assert result.hotspots == ()


def test_change_exposes_the_newer_comment(cycle: DiffResult) -> None:
    change = next(c for c in cycle.changes if c.comment_id == 1)
    assert change.comment.text == "Agreed, rewritten in the latest draft."
    assert change.resolved is True
    assert change.author == "Bob Reviewer"


def test_a_removal_falls_back_to_the_older_comment(cycle: DiffResult) -> None:
    removal = cycle.removed[0]
    assert removal.after is None
    assert removal.comment.comment_id == 3
    assert removal.level is None and removal.score == 0.0


# ═════════════════════════════════════════════════════════════════════════════
#  Review velocity
# ═════════════════════════════════════════════════════════════════════════════


def test_velocity_counts_match_the_facets(cycle: DiffResult) -> None:
    velocity = cycle.velocity
    assert velocity.added == len(cycle.added)
    assert velocity.removed == len(cycle.removed)
    assert velocity.resolved == len(cycle.resolved)
    assert velocity.reopened == len(cycle.reopened)
    assert velocity.edited == len(cycle.edited)


def test_velocity_totals(cycle: DiffResult) -> None:
    velocity = cycle.velocity
    assert velocity.comments_before == 4
    assert velocity.comments_after == 4
    assert velocity.net_change == 0
    assert velocity.open_before == 3
    assert velocity.open_after == 2


def test_net_resolved_subtracts_reopenings(cycle: DiffResult) -> None:
    assert cycle.velocity.net_resolved == 2 - 1


def test_resolution_rate_delta(cycle: DiffResult) -> None:
    velocity = cycle.velocity
    assert velocity.resolution_rate_before == pytest.approx(0.25)
    assert velocity.resolution_rate_after == pytest.approx(0.5)
    assert velocity.resolution_rate_delta == pytest.approx(0.25)


def test_converging_needs_progress_and_a_smaller_backlog(tmp_path: Path) -> None:
    before = docx_of(tmp_path, "a.docx", V1_SPECS)
    after = docx_of(tmp_path, "b.docx", V2_SPECS)
    assert diff_comments(before, after).velocity.is_converging

    # Reverse the cycle: comments re-open and the backlog grows.
    assert not diff_comments(after, before).velocity.is_converging


def test_time_to_resolution_is_measured_against_the_newest_comment(
    tmp_path: Path,
) -> None:
    """No resolution timestamp exists in OOXML, so the newest comment stands in."""
    before = docx_of(tmp_path, "a.docx", [
        CommentSpec(id=0, author="Alice", date="2026-01-01T00:00:00Z", text="Old issue."),
        CommentSpec(id=1, author="Bob", date="2026-01-11T00:00:00Z", text="Marker."),
    ])
    after = docx_of(tmp_path, "b.docx", [
        CommentSpec(id=0, author="Alice", date="2026-01-01T00:00:00Z",
                    text="Old issue.", done=True),
        CommentSpec(id=1, author="Bob", date="2026-01-11T00:00:00Z",
                    text="Marker.", done=True),
    ])
    velocity = diff_comments(before, after).velocity
    assert velocity.median_time_to_resolution == timedelta(days=5)
    assert velocity.mean_time_to_resolution == timedelta(days=5)


def test_open_age_tracks_the_oldest_unresolved_comment(tmp_path: Path) -> None:
    before = docx_of(tmp_path, "a.docx", [
        CommentSpec(id=0, author="Alice", date="2026-01-01T00:00:00Z", text="Old issue."),
    ])
    after = docx_of(tmp_path, "b.docx", [
        CommentSpec(id=0, author="Alice", date="2026-01-01T00:00:00Z", text="Old issue."),
        CommentSpec(id=1, author="Bob", date="2026-02-01T00:00:00Z", text="Newer note."),
    ])
    velocity = diff_comments(before, after).velocity
    assert velocity.oldest_open_age == timedelta(days=31)


def test_timing_is_absent_when_no_date_can_be_parsed(tmp_path: Path) -> None:
    before = docx_of(tmp_path, "a.docx", [
        CommentSpec(id=0, author="Alice", date="not a date", text="One."),
    ])
    after = docx_of(tmp_path, "b.docx", [
        CommentSpec(id=0, author="Alice", date="not a date", text="One.", done=True),
    ])
    velocity = diff_comments(before, after).velocity
    assert velocity.median_time_to_resolution is None
    assert velocity.oldest_open_age is None


# ═════════════════════════════════════════════════════════════════════════════
#  Risk hotspots
# ═════════════════════════════════════════════════════════════════════════════


def test_resolved_threads_are_not_hotspots(tmp_path: Path) -> None:
    rows = docx_of(tmp_path, "a.docx", [
        CommentSpec(id=0, author="Alice", date="2026-01-01T09:00:00Z",
                    text="Done and dusted.", done=True),
    ])
    assert diff_comments(rows, rows).hotspots == ()


def test_a_reopened_thread_outranks_a_merely_open_one(cycle: DiffResult) -> None:
    assert cycle.hotspots
    top = cycle.hotspots[0]
    assert top.root_id == 2
    assert top.reopened == 1
    assert "re-opened after being resolved" in top.reasons


def test_a_thread_open_across_both_versions_is_flagged_persistent(
    tmp_path: Path,
) -> None:
    rows = docx_of(tmp_path, "a.docx", [
        CommentSpec(id=0, author="Alice", date="2026-01-01T09:00:00Z",
                    text="Nobody has closed this."),
    ])
    spot = diff_comments(rows, rows).hotspots[0]
    assert spot.persistent_open == 1
    assert "open across both versions" in spot.reasons


def test_a_long_running_thread_is_flagged(tmp_path: Path) -> None:
    before = docx_of(tmp_path, "a.docx", [
        CommentSpec(id=0, author="Alice", date="2026-01-01T00:00:00Z", text="Ancient."),
    ])
    after = docx_of(tmp_path, "b.docx", [
        CommentSpec(id=0, author="Alice", date="2026-01-01T00:00:00Z", text="Ancient."),
        CommentSpec(id=1, author="Bob", date="2026-06-01T00:00:00Z", text="Recent."),
    ])
    spot = next(s for s in diff_comments(before, after).hotspots if s.root_id == 0)
    assert spot.age_days is not None and spot.age_days > 100
    assert "long-running" in spot.reasons


def test_hotspots_are_ranked_by_score(cycle: DiffResult) -> None:
    scores = [spot.score for spot in cycle.hotspots]
    assert scores == sorted(scores, reverse=True)


def test_a_growing_thread_is_flagged(tmp_path: Path) -> None:
    before = docx_of(tmp_path, "a.docx", [
        CommentSpec(id=0, author="Alice", date="2026-01-01T09:00:00Z", text="Root issue."),
    ])
    after = docx_of(tmp_path, "b.docx", [
        CommentSpec(id=0, author="Alice", date="2026-01-01T09:00:00Z", text="Root issue."),
        CommentSpec(id=1, author="Bob", date="2026-01-02T09:00:00Z",
                    text="Still a problem.", parent=0),
        CommentSpec(id=2, author="Carol", date="2026-01-03T09:00:00Z",
                    text="Agreed, still broken.", parent=0),
    ])
    spot = diff_comments(before, after).hotspots[0]
    assert spot.added == 2
    assert "still attracting new comments" in spot.reasons


# ═════════════════════════════════════════════════════════════════════════════
#  Entry points
# ═════════════════════════════════════════════════════════════════════════════


def test_compare_accepts_paths(tmp_path: Path) -> None:
    a = build_docx(tmp_path / "a.docx", V1_SPECS)
    b = build_docx(tmp_path / "b.docx", V2_SPECS)
    result = compare_comments(str(a), str(b))
    assert result.before_name == "a.docx"
    assert result.after_name == "b.docx"


def test_compare_accepts_path_objects(tmp_path: Path) -> None:
    a = build_docx(tmp_path / "a.docx", V1_SPECS)
    b = build_docx(tmp_path / "b.docx", V2_SPECS)
    assert compare_comments(a, b).velocity.comments_before == 4


def test_compare_accepts_comment_lists(tmp_path: Path) -> None:
    before = docx_of(tmp_path, "a.docx", V1_SPECS)
    after = docx_of(tmp_path, "b.docx", V2_SPECS)
    result = compare_comments(before, after)
    assert result.before_name == "a.docx"
    assert len(result.added) == 1


def test_compare_accepts_a_filtered_subset(tmp_path: Path) -> None:
    """Anything that can be filtered can be compared."""
    from docx_comment_parser.filters import filter_comments

    before = docx_of(tmp_path, "a.docx", V1_SPECS)
    after = docx_of(tmp_path, "b.docx", V2_SPECS)
    result = compare_comments(
        filter_comments(before, author="alice"),
        filter_comments(after, author="alice"),
    )
    assert result.velocity.comments_before == 2


def test_generated_at_is_honoured(cycle: DiffResult) -> None:
    assert cycle.generated_at == FIXED_TIME


def test_the_result_records_how_it_was_made(cycle: DiffResult) -> None:
    assert cycle.threshold == sim.DEFAULT_THRESHOLD
    assert cycle.similarity_backend in {"rapidfuzz", "difflib"}


def test_compare_comments_is_importable_from_the_top_level() -> None:
    assert dcp.compare_comments is compare_comments


def test_the_comparison_layer_is_reachable_as_an_attribute() -> None:
    assert dcp.comparison.compare_comments is compare_comments


def test_unknown_attributes_still_raise(tmp_path: Path) -> None:
    with pytest.raises(AttributeError):
        dcp.no_such_thing  # type: ignore[attr-defined]


# ═════════════════════════════════════════════════════════════════════════════
#  Structured export
# ═════════════════════════════════════════════════════════════════════════════


def test_rows_have_the_canonical_columns(cycle: DiffResult) -> None:
    rows = cycle.to_rows()
    assert len(rows) == len(cycle.changes)
    for row in rows:
        assert tuple(row) == diff_report.DIFF_FIELD_NAMES


def test_rows_keep_both_sides(cycle: DiffResult) -> None:
    row = next(r for r in cycle.to_rows() if r["comment_id_after"] == 1)
    assert row["text_before"] == "Agreed."
    assert row["text_after"] == "Agreed, rewritten in the latest draft."
    assert row["resolved_before"] is False
    assert row["resolved_after"] is True


def test_an_absent_side_is_none_not_false(cycle: DiffResult) -> None:
    """``False`` would claim the comment existed and was open. It did not exist."""
    row = next(r for r in cycle.to_rows() if r["change"] == "added")
    assert row["resolved_before"] is None
    assert row["comment_id_before"] is None


def test_json_round_trips(cycle: DiffResult) -> None:
    decoded = json.loads(cycle.to_json())
    assert decoded["before"] == "v1.docx"
    assert decoded["velocity"]["net_resolved"] == 1
    assert len(decoded["changes"]) == len(cycle.changes)
    assert decoded["hotspots"][0]["root_id"] == 2


def test_json_is_directly_serialisable(cycle: DiffResult) -> None:
    """No datetimes or timedeltas survive into the payload."""
    json.dumps(cycle.to_dict())


def test_export_json_writes_a_file(cycle: DiffResult, tmp_path: Path) -> None:
    target = cycle.export_json(tmp_path / "nested" / "diff.json")
    assert target.exists()
    assert json.loads(target.read_text(encoding="utf-8"))["after"] == "v2.docx"


def test_export_csv_writes_every_change(cycle: DiffResult, tmp_path: Path) -> None:
    import csv as _csv

    target = cycle.export_csv(tmp_path / "diff.csv")
    with open(target, newline="", encoding="utf-8") as handle:
        rows = list(_csv.reader(handle))
    assert rows[0] == list(diff_report.DIFF_FIELD_NAMES)
    assert len(rows) == len(cycle.changes) + 1


def test_csv_renders_absent_values_as_empty_cells(cycle: DiffResult, tmp_path: Path) -> None:
    import csv as _csv

    target = cycle.export_csv(tmp_path / "diff.csv")
    with open(target, newline="", encoding="utf-8") as handle:
        rows = list(_csv.DictReader(handle))
    added = next(r for r in rows if r["change"] == "added")
    assert added["comment_id_before"] == ""
    assert added["resolved_before"] == ""
    assert added["resolved_after"] == "false"


def test_dataframe_export(cycle: DiffResult) -> None:
    pytest.importorskip("pandas")
    frame = cycle.to_dataframe()
    assert list(frame.columns) == list(diff_report.DIFF_FIELD_NAMES)
    assert len(frame) == len(cycle.changes)
    assert str(frame["comment_id_before"].dtype) == "Int64"
    assert str(frame["resolved_after"].dtype) == "boolean"
    assert str(frame["date_parsed"].dtype).startswith("datetime64")


def test_dataframe_of_an_empty_diff_keeps_its_schema(tmp_path: Path) -> None:
    pytest.importorskip("pandas")
    empty = docx_of(tmp_path, "e.docx", [])
    frame = diff_comments(empty, empty).to_dataframe()
    assert list(frame.columns) == list(diff_report.DIFF_FIELD_NAMES)
    assert len(frame) == 0


def test_polars_export(cycle: DiffResult) -> None:
    pytest.importorskip("polars")
    frame = cycle.to_polars()
    assert frame.columns == list(diff_report.DIFF_FIELD_NAMES)
    assert frame.height == len(cycle.changes)


# ═════════════════════════════════════════════════════════════════════════════
#  Summary and Markdown
# ═════════════════════════════════════════════════════════════════════════════


def test_summary_names_both_documents(cycle: DiffResult) -> None:
    text = cycle.summary()
    assert "v1.docx" in text and "v2.docx" in text


def test_summary_reports_every_bucket(cycle: DiffResult) -> None:
    text = cycle.summary()
    for label in ("Added", "Removed", "Resolved", "Re-opened", "Edited", "Unchanged"):
        assert label in text


def test_summary_names_the_backend_and_threshold(cycle: DiffResult) -> None:
    assert cycle.similarity_backend in cycle.summary()
    assert "0.85" in cycle.summary()


def test_markdown_report_contains_the_figures(cycle: DiffResult) -> None:
    text = cycle.to_markdown()
    assert text.startswith("# Comment diff — v1.docx → v2.docx")
    assert "## Review velocity" in text
    assert "## Risk hotspots" in text
    assert "Dan Legal" in text  # the added comment
    assert "was: Agreed." in text  # the edited comment's old text


def test_markdown_can_omit_the_detail(cycle: DiffResult) -> None:
    digest = cycle.to_markdown(include_details=False)
    assert "## Review velocity" in digest
    assert "## New comments" not in digest


def test_markdown_title_override(cycle: DiffResult) -> None:
    assert cycle.to_markdown(title="Round two").startswith("# Comment diff — Round two")


def test_markdown_of_an_unchanged_pair_says_so(tmp_path: Path) -> None:
    rows = docx_of(tmp_path, "a.docx", V1_SPECS)
    assert "**No changes.**" in diff_comments(rows, rows).to_markdown()


def test_markdown_of_empty_documents(tmp_path: Path) -> None:
    empty = docx_of(tmp_path, "e.docx", [])
    text = diff_comments(empty, empty).to_markdown()
    assert "Neither document contains any comments." in text


def test_markdown_escapes_table_breaking_characters(tmp_path: Path) -> None:
    """A pipe in a comment would otherwise split a cell and shift every column."""
    before = docx_of(tmp_path, "a.docx", [
        CommentSpec(id=0, author="A|B", date="2026-01-01T09:00:00Z",
                    text="Contains | a pipe\nand a newline."),
    ])
    text = diff_comments(before, before).to_markdown()
    for line in text.splitlines():
        if line.startswith("|"):
            assert "\\|" in line or "|" not in line[1:-1].replace(" | ", "")


def test_export_markdown_writes_a_file(cycle: DiffResult, tmp_path: Path) -> None:
    target = cycle.export_markdown(tmp_path / "nested" / "diff.md")
    assert target.exists()
    assert "# Comment diff" in target.read_text(encoding="utf-8")


def test_markdown_report_is_reproducible(tmp_path: Path) -> None:
    before = docx_of(tmp_path, "a.docx", V1_SPECS)
    after = docx_of(tmp_path, "b.docx", V2_SPECS)
    first = diff_comments(before, after, generated_at=FIXED_TIME).to_markdown()
    second = diff_comments(before, after, generated_at=FIXED_TIME).to_markdown()
    assert first == second


# ═════════════════════════════════════════════════════════════════════════════
#  HTML report
# ═════════════════════════════════════════════════════════════════════════════

# A marker, not a module-level importorskip: `pytest.importorskip` raises Skipped
# at import time, which takes the *whole module* with it — every matcher and
# diff test above included. That matters because the release workflow tests each
# built wheel with pytest and nothing else installed, so jinja2 is absent exactly
# where the comparison engine most needs covering.
needs_jinja2 = pytest.mark.skipif(
    importlib.util.find_spec("jinja2") is None,
    reason="HTML diff reports need the report extra",
)


@needs_jinja2
def test_html_report_is_written(cycle: DiffResult, tmp_path: Path) -> None:
    target = cycle.export_html(tmp_path / "nested" / "diff.html")
    assert target.exists()
    body = target.read_text(encoding="utf-8")
    assert body.startswith("<!doctype html>")
    assert body.rstrip().endswith("</html>")


@needs_jinja2
def test_html_report_has_no_external_references(cycle: DiffResult) -> None:
    """The offline promise: nothing is fetched when the file is opened."""
    body = cycle.to_html()
    assert re.findall(r'(?:src|href)\s*=\s*["\'](?!#)([^"\']+)', body) == []


@needs_jinja2
def test_html_report_carries_the_headline_numbers(cycle: DiffResult) -> None:
    body = cycle.to_html()
    assert "v1.docx" in body and "v2.docx" in body
    assert "Review velocity" in body
    assert "Risk hotspots" in body


@needs_jinja2
def test_html_report_embeds_the_changes_as_json(cycle: DiffResult) -> None:
    body = cycle.to_html()
    payload = re.search(
        r'<script type="application/json" id="diff-data">(.*?)</script>', body, re.S
    )
    assert payload is not None
    data = json.loads(payload.group(1))
    assert len(data["rows"]) == len(cycle.changes)
    assert "Dan Legal" in data["authors"]


@needs_jinja2
def test_document_content_cannot_break_the_page(tmp_path: Path) -> None:
    """A comment containing markup must be inert data, not markup."""
    hostile = "</script><script>alert('x')</script>"
    before = docx_of(tmp_path, "a.docx", [
        CommentSpec(id=0, author="Alice", date="2026-01-01T09:00:00Z", text=hostile),
    ])
    after = docx_of(tmp_path, "b.docx", [
        CommentSpec(id=0, author="Alice", date="2026-01-01T09:00:00Z",
                    text=hostile, done=True),
    ])
    body = diff_comments(before, after).to_html()

    # Exactly the two script tags the template itself opens.
    assert body.count("<script") == 2
    payload = re.search(
        r'<script type="application/json" id="diff-data">(.*?)</script>', body, re.S
    )
    assert payload is not None
    # …and the comment's own text survives intact once decoded.
    assert hostile in json.dumps(json.loads(payload.group(1)))


@needs_jinja2
def test_html_title_override(cycle: DiffResult) -> None:
    assert "<h1>Round two</h1>" in cycle.to_html(title="Round two")


@needs_jinja2
def test_html_report_is_reproducible(tmp_path: Path) -> None:
    before = docx_of(tmp_path, "a.docx", V1_SPECS)
    after = docx_of(tmp_path, "b.docx", V2_SPECS)
    first = diff_comments(before, after, generated_at=FIXED_TIME).to_html()
    second = diff_comments(before, after, generated_at=FIXED_TIME).to_html()
    assert first == second


@needs_jinja2
def test_html_report_of_an_empty_diff(tmp_path: Path) -> None:
    empty = docx_of(tmp_path, "e.docx", [])
    body = diff_comments(empty, empty).to_html()
    assert "Nothing is left open" in body


@needs_jinja2
def test_a_custom_template_receives_the_context(cycle: DiffResult, tmp_path: Path) -> None:
    template = tmp_path / "mine.html.j2"
    template.write_text(
        "<!doctype html><title>{{ title }}</title>"
        "<p>{{ velocity.net_resolved }} closed, {{ hotspots | length }} hotspots</p>",
        encoding="utf-8",
    )
    body = cycle.to_html(template=template)
    assert "1 closed, 2 hotspots" in body


@needs_jinja2
def test_unicode_survives_every_format(tmp_path: Path) -> None:
    specs = [
        CommentSpec(id=0, author="Νίκος Ηλιόπουλος", initials="ΝΗ",
                    date="2026-01-01T09:00:00Z",
                    text="Χρειάζεται διόρθωση — 日本語, emoji 🚀.",
                    anchor="Κείμενο αναφοράς"),
    ]
    resolved = [CommentSpec(**{**specs[0].__dict__, "done": True})]
    result = diff_comments(
        docx_of(tmp_path, "a.docx", specs), docx_of(tmp_path, "b.docx", resolved)
    )

    assert "🚀" in result.to_markdown()
    assert "🚀" in result.to_html()
    assert "🚀" in result.to_json()
    csv_path = result.export_csv(tmp_path / "u.csv")
    assert "🚀" in csv_path.read_text(encoding="utf-8")
