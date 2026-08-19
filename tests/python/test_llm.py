"""Tests for the v1.5 LLM layer: context, classification, records, chunking, prompts.

The context tests are the ones that matter most.  A window read from the wrong
offset produces a record that looks entirely plausible and quotes the wrong
paragraph, and no downstream test would catch it — so the document fixtures here
are built with known text at known positions and the offsets are asserted
directly, including the cases that break a naive reader: a comment spanning
several runs, a comment with no range at all, tracked deletions, field codes, and
table cells.

The chunking tests assert the one hard promise: a thread is never split.  The
prompt tests assert the two that make an answer checkable — every packed comment
id is declared, and a corpus that did not fit says so.
"""

from __future__ import annotations

import json
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest

import docx_comment_parser as dcp
from docx_comment_parser.llm import (
    Category,
    Intent,
    Priority,
    Prompt,
    chunk_records,
    chunk_texts,
    classify_comments,
    classify_text,
    create_action_items_prompt,
    create_diff_summary_prompt,
    create_resolution_prompt,
    create_review_summary_prompt,
    create_risk_prompt,
    create_triage_prompt,
    estimate_tokens,
    token_backend,
)
from docx_comment_parser.llm.chunking import _RECORD_OVERHEAD_TOKENS
from docx_comment_parser.llm.context import DocumentContext
from docx_comment_parser.llm.dataset import (
    LLMRecord,
    build_dataset,
    export_jsonl,
    to_embeddings_input,
    to_jsonl,
)
from docx_comment_parser.models import Comment

from conftest import CommentSpec, build_docx, make_specs

from test_privacy import make_comment


# ─────────────────────────────────────────────────────────────────────────────
#  Fixtures
# ─────────────────────────────────────────────────────────────────────────────

REVIEW_SPECS: List[CommentSpec] = [
    CommentSpec(
        id=0, author="Alice Tester", initials="AT",
        date="2024-03-01T10:00:00Z",
        text="This clause creates unlimited liability. Legal must sign off.",
        anchor="The supplier shall indemnify the customer without limit.",
    ),
    CommentSpec(
        id=1, author="Bob Reviewer", initials="BR", parent=0,
        date="2024-03-02T09:00:00Z",
        text="Agreed. I have asked counsel to look at the indemnity clause.",
    ),
    CommentSpec(
        id=2, author="Carol Editor", initials="CE", done=True,
        date="2024-03-03T11:00:00Z",
        text="Typo: 'recieve' should be 'receive'.",
        anchor="The customer will recieve notice",
    ),
    CommentSpec(
        id=3, author="Alice Tester", initials="AT",
        date="2024-03-04T15:00:00Z",
        text="Security: the API key is hardcoded here. This is a blocker.",
        anchor="Authorization: Bearer hardcoded-value",
    ),
    CommentSpec(
        id=4, author="Dave Architect", initials="DA",
        date="2024-03-05T08:00:00Z",
        text="Why seven years? GDPR needs a documented lawful basis for that.",
        anchor="Records are retained for seven years.",
    ),
]

BODY = (
    "Section 4 sets out the commercial terms agreed between the parties. "
    "Nothing in this section limits liability for fraud."
)


@pytest.fixture(scope="module")
def review_docx(tmp_path_factory: pytest.TempPathFactory) -> Path:
    directory = tmp_path_factory.mktemp("llm")
    return build_docx(directory / "review.docx", REVIEW_SPECS, body_text=BODY)


@pytest.fixture(scope="module")
def review_comments(review_docx: Path) -> List[Comment]:
    parser = dcp.DocxParser()
    parser.parse(str(review_docx))
    return parser.to_comments()


@pytest.fixture(scope="module")
def review_records(review_comments: List[Comment]) -> List[LLMRecord]:
    return list(build_dataset(review_comments, chars_before=200, chars_after=200))


# ─────────────────────────────────────────────────────────────────────────────
#  Document context
# ─────────────────────────────────────────────────────────────────────────────


def test_the_body_text_is_recovered(review_docx: Path) -> None:
    context = DocumentContext.from_docx(review_docx)
    assert "Section 4 sets out the commercial terms" in context.text
    assert len(context) == 4  # four anchored comments; the reply has no range


def test_each_anchor_maps_to_the_right_characters(review_docx: Path) -> None:
    """The offsets, asserted directly. Everything downstream depends on them."""
    context = DocumentContext.from_docx(review_docx)
    assert context.anchor_text(0) == "The supplier shall indemnify the customer without limit."
    assert context.anchor_text(2) == "The customer will recieve notice"
    assert context.anchor_text(4) == "Records are retained for seven years."


def test_a_window_reads_from_both_sides(review_docx: Path) -> None:
    context = DocumentContext.from_docx(review_docx)
    window = context.window(2, chars_before=200, chars_after=200)
    assert "indemnify" in window.before
    assert window.anchor == "The customer will recieve notice"
    assert "Bearer" in window.after


def test_the_first_anchor_has_the_document_before_it(review_docx: Path) -> None:
    context = DocumentContext.from_docx(review_docx)
    window = context.window(0, chars_before=300, chars_after=0)
    assert "commercial terms" in window.before
    assert window.after == ""


def test_a_window_at_the_end_of_the_document_is_short_not_broken(
    review_docx: Path,
) -> None:
    context = DocumentContext.from_docx(review_docx)
    window = context.window(4, chars_before=50, chars_after=5000)
    assert window.after == ""
    assert window.anchor


def test_zero_width_windows_are_empty(review_docx: Path) -> None:
    context = DocumentContext.from_docx(review_docx)
    window = context.window(0, chars_before=0, chars_after=0)
    assert window.before == "" and window.after == ""
    assert window.anchor  # the anchor itself is always available


def test_a_comment_with_no_anchor_yields_an_empty_window(review_docx: Path) -> None:
    context = DocumentContext.from_docx(review_docx)
    assert context.window(1).is_empty


def test_an_unknown_comment_id_yields_an_empty_window(review_docx: Path) -> None:
    assert DocumentContext.from_docx(review_docx).window(999).is_empty


def test_a_window_does_not_start_mid_word(review_docx: Path) -> None:
    context = DocumentContext.from_docx(review_docx)
    window = context.window(2, chars_before=25, chars_after=25)
    # The cut point falls inside a word; the trim moves it to the next space.
    assert not window.before or not window.before[0].isspace()
    assert window.before == "" or " " not in window.before[:1]


def test_a_missing_file_gives_an_empty_context(tmp_path: Path) -> None:
    """An enrichment must never take down an export that would have worked."""
    context = DocumentContext.from_docx(tmp_path / "nope.docx")
    assert not context
    assert context.window(0).is_empty


def test_a_corrupt_file_gives_an_empty_context(tmp_path: Path) -> None:
    broken = tmp_path / "broken.docx"
    broken.write_bytes(b"not a zip archive at all")
    assert not DocumentContext.from_docx(broken)


def test_a_document_with_no_body_part_gives_an_empty_context(tmp_path: Path) -> None:
    target = tmp_path / "nobody.docx"
    with zipfile.ZipFile(target, "w") as archive:
        archive.writestr("word/comments.xml", "<x/>")
    assert not DocumentContext.from_docx(target)


def _raw_docx(path: Path, document_xml: str) -> Path:
    """Write a minimal package around a hand-written document.xml."""
    from conftest import CONTENT_TYPES, ROOT_RELS, WORD_RELS

    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("[Content_Types].xml", CONTENT_TYPES)
        archive.writestr("_rels/.rels", ROOT_RELS)
        archive.writestr("word/_rels/document.xml.rels", WORD_RELS)
        archive.writestr("word/document.xml", document_xml)
        archive.writestr(
            "word/comments.xml",
            '<?xml version="1.0"?><w:comments xmlns:w="http://schemas.openxml'
            'formats.org/wordprocessingml/2006/main"/>',
        )
    return path


W_NS = 'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"'


def test_an_anchor_spanning_several_runs_is_joined(tmp_path: Path) -> None:
    """Word splits a selection across runs whenever formatting changes."""
    xml = (
        f'<?xml version="1.0"?><w:document {W_NS}><w:body><w:p>'
        '<w:commentRangeStart w:id="0"/>'
        '<w:r><w:t>The retention </w:t></w:r>'
        '<w:r><w:t>period </w:t></w:r>'
        '<w:r><w:t>is seven years</w:t></w:r>'
        '<w:commentRangeEnd w:id="0"/>'
        "</w:p></w:body></w:document>"
    )
    context = DocumentContext.from_docx(_raw_docx(tmp_path / "runs.docx", xml))
    assert context.anchor_text(0) == "The retention period is seven years"


def test_tracked_deletions_are_not_read_as_document_text(tmp_path: Path) -> None:
    """A model should see the document as it stands, not as it was."""
    xml = (
        f'<?xml version="1.0"?><w:document {W_NS}><w:body><w:p>'
        '<w:commentRangeStart w:id="0"/>'
        '<w:r><w:t>Keep this</w:t></w:r>'
        '<w:del><w:r><w:delText> and drop that</w:delText></w:r></w:del>'
        '<w:commentRangeEnd w:id="0"/>'
        "</w:p></w:body></w:document>"
    )
    context = DocumentContext.from_docx(_raw_docx(tmp_path / "del.docx", xml))
    assert context.anchor_text(0) == "Keep this"


def test_field_codes_are_not_read_as_document_text(tmp_path: Path) -> None:
    xml = (
        f'<?xml version="1.0"?><w:document {W_NS}><w:body><w:p>'
        '<w:commentRangeStart w:id="0"/>'
        '<w:r><w:instrText> PAGEREF _Toc12345 </w:instrText></w:r>'
        '<w:r><w:t>Chapter 3</w:t></w:r>'
        '<w:commentRangeEnd w:id="0"/>'
        "</w:p></w:body></w:document>"
    )
    context = DocumentContext.from_docx(_raw_docx(tmp_path / "field.docx", xml))
    assert context.anchor_text(0) == "Chapter 3"


def test_a_comment_inside_a_table_cell_is_found(tmp_path: Path) -> None:
    xml = (
        f'<?xml version="1.0"?><w:document {W_NS}><w:body>'
        "<w:tbl><w:tr><w:tc><w:p>"
        '<w:commentRangeStart w:id="7"/>'
        '<w:r><w:t>Cell contents</w:t></w:r>'
        '<w:commentRangeEnd w:id="7"/>'
        "</w:p></w:tc></w:tr></w:tbl>"
        "</w:body></w:document>"
    )
    context = DocumentContext.from_docx(_raw_docx(tmp_path / "table.docx", xml))
    assert context.anchor_text(7) == "Cell contents"


def test_a_reference_with_no_range_still_gets_a_position(tmp_path: Path) -> None:
    """Zero-width anchor: the paragraph around it is still usable context."""
    xml = (
        f'<?xml version="1.0"?><w:document {W_NS}><w:body><w:p>'
        '<w:r><w:t>Some preceding words here.</w:t></w:r>'
        '<w:r><w:commentReference w:id="3"/></w:r>'
        "</w:p></w:body></w:document>"
    )
    context = DocumentContext.from_docx(_raw_docx(tmp_path / "ref.docx", xml))
    assert context.anchor_range(3) is not None
    assert context.anchor_text(3) == ""
    assert "preceding words" in context.window(3, 100, 100).before


def test_an_unclosed_range_is_closed_at_the_end_of_the_body(tmp_path: Path) -> None:
    xml = (
        f'<?xml version="1.0"?><w:document {W_NS}><w:body><w:p>'
        '<w:commentRangeStart w:id="1"/>'
        '<w:r><w:t>Runs to the end.</w:t></w:r>'
        "</w:p></w:body></w:document>"
    )
    context = DocumentContext.from_docx(_raw_docx(tmp_path / "open.docx", xml))
    assert "Runs to the end." in context.anchor_text(1)


def test_a_paragraph_can_be_read_whole(review_docx: Path) -> None:
    context = DocumentContext.from_docx(review_docx)
    assert context.paragraph_text(4) == "Records are retained for seven years."


def test_breaks_and_tabs_become_whitespace(tmp_path: Path) -> None:
    xml = (
        f'<?xml version="1.0"?><w:document {W_NS}><w:body><w:p>'
        '<w:commentRangeStart w:id="0"/>'
        '<w:r><w:t>a</w:t><w:tab/><w:t>b</w:t><w:br/><w:t>c</w:t></w:r>'
        '<w:commentRangeEnd w:id="0"/>'
        "</w:p></w:body></w:document>"
    )
    context = DocumentContext.from_docx(_raw_docx(tmp_path / "ws.docx", xml))
    assert context.anchor_text(0) == "a\tb\nc"


# ─────────────────────────────────────────────────────────────────────────────
#  Classification
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "text,expected",
    [
        ("Typo: 'recieve' should be 'receive'.", Category.GRAMMAR),
        ("This paragraph is wordy — please reword it.", Category.EDITORIAL),
        ("The API endpoint returns a 500 here; this is a bug.", Category.TECHNICAL),
        ("The contrast fails WCAG AA on this diagram.", Category.DESIGN),
        ("GDPR requires a documented lawful basis for this retention period.",
         Category.COMPLIANCE),
        ("This indemnity clause creates unlimited liability — ask counsel.",
         Category.LEGAL),
        ("The API key is hardcoded; that is a credential leak.", Category.SECURITY),
        ("This acceptance criterion is not covered by any requirement.",
         Category.REQUIREMENTS),
    ],
)
def test_each_category_is_recognised(text: str, expected: Category) -> None:
    assert classify_text(text).category is expected


def test_an_unremarkable_comment_is_not_forced_into_a_category() -> None:
    """Reporting 'Other' honestly beats the least-bad of eight guesses."""
    result = classify_text("I had a look at this yesterday.")
    assert result.category is Category.OTHER
    assert result.confidence == 0.0
    assert not result.is_confident


def test_empty_text_classifies_without_raising() -> None:
    result = classify_text("")
    assert result.category is Category.OTHER
    assert result.intent is Intent.INFORMATION


@pytest.mark.parametrize(
    "text,expected",
    [
        ("Why does this say seven years?", Intent.QUESTION),
        ("Please remove this paragraph.", Intent.CHANGE_REQUEST),
        ("Consider moving this to the appendix.", Intent.SUGGESTION),
        ("I disagree — this is wrong.", Intent.OBJECTION),
        ("Looks good to me.", Intent.APPROVAL),
        ("For reference, we shipped this last year.", Intent.INFORMATION),
    ],
)
def test_each_intent_is_recognised(text: str, expected: Intent) -> None:
    assert classify_text(text).intent is expected


def test_a_compliment_attached_to_a_request_is_still_a_request() -> None:
    """Reading this as an approval would drop the work it asks for."""
    result = classify_text("Looks good, but please fix the citation.")
    assert result.intent is Intent.CHANGE_REQUEST
    assert result.action_required


@pytest.mark.parametrize(
    "text,expected",
    [
        ("This is a blocker — do not ship.", Priority.BLOCKER),
        ("Urgent: this is misleading.", Priority.HIGH),
        ("nit: extra whitespace here.", Priority.LOW),
        ("Please move this section down.", Priority.MEDIUM),
    ],
)
def test_priority_follows_the_words_used(text: str, expected: Priority) -> None:
    assert classify_text(text).priority is expected


def test_a_typo_is_low_priority_even_when_stated_forcefully() -> None:
    assert classify_text("This spelling is wrong: 'recieve'.").priority is Priority.LOW


def test_a_security_comment_is_high_without_saying_so() -> None:
    """The comments that become incidents when triaged as ordinary edits."""
    result = classify_text("The credentials are stored unencrypted in the config.")
    assert result.category is Category.SECURITY
    assert result.priority is Priority.HIGH


def test_a_blocker_always_requires_action() -> None:
    result = classify_text("This is a blocker.")
    assert result.priority is Priority.BLOCKER
    assert result.action_required


def test_the_anchored_text_informs_the_category() -> None:
    """'Add a citation' alone is editorial; on a liability clause it is legal."""
    plain = classify_text("Add a citation here.")
    anchored = classify_text(
        "Add a citation here.",
        "The supplier's liability under this indemnity clause is unlimited.",
    )
    assert anchored.scores.get(Category.LEGAL, 0) > plain.scores.get(Category.LEGAL, 0)


def test_a_classification_names_the_terms_that_drove_it() -> None:
    result = classify_text("GDPR compliance: the retention period needs a lawful basis.")
    assert result.matched
    assert any("GDPR" in term for term in result.matched)


def test_repetition_is_not_evidence() -> None:
    """A long comment must not out-score a short one by saying a word twice."""
    once = classify_text("This is a security problem.")
    many = classify_text("Security security security. This is a security problem.")
    assert once.scores[Category.SECURITY] == many.scores[Category.SECURITY]


def test_classification_is_deterministic() -> None:
    text = "Please clarify the retention period; GDPR compliance depends on it."
    assert classify_text(text) == classify_text(text)


def test_classify_comments_covers_a_whole_export(review_comments: List[Comment]) -> None:
    results = classify_comments(review_comments)
    assert len(results) == len(review_comments)
    assert {r.category for r in results} >= {Category.LEGAL, Category.SECURITY}


# ─────────────────────────────────────────────────────────────────────────────
#  Records
# ─────────────────────────────────────────────────────────────────────────────


def test_a_record_carries_the_roadmap_schema(review_records: List[LLMRecord]) -> None:
    payload = review_records[0].to_dict()
    for key in (
        "comment_id", "author", "context_before", "referenced_text",
        "context_after", "comment", "thread",
    ):
        assert key in payload


def test_a_record_carries_the_review_intelligence_tags(
    review_records: List[LLMRecord],
) -> None:
    payload = review_records[0].to_dict()
    assert payload["category"] == "Legal"
    assert payload["priority"] in {"blocker", "high", "medium", "low"}
    assert isinstance(payload["action_required"], bool)


def test_the_context_windows_are_real_document_text(
    review_records: List[LLMRecord],
) -> None:
    first = review_records[0]
    assert "commercial terms" in first.context_before
    assert first.referenced_text.startswith("The supplier shall indemnify")


def test_the_thread_travels_with_every_comment_in_it(
    review_records: List[LLMRecord],
) -> None:
    root = next(r for r in review_records if r.comment_id == 0)
    reply = next(r for r in review_records if r.comment_id == 1)
    assert len(root.thread) == 2
    assert [entry["comment_id"] for entry in root.thread] == [0, 1]
    assert root.thread == reply.thread


def test_a_thread_is_ordered_oldest_first(review_records: List[LLMRecord]) -> None:
    """Out of order is the fastest way to make a model misattribute a decision."""
    root = next(r for r in review_records if r.comment_id == 0)
    dates = [entry["date"] for entry in root.thread]
    assert dates == sorted(dates)


def test_threads_can_be_switched_off(review_comments: List[Comment]) -> None:
    dataset = build_dataset(review_comments, include_thread=False)
    assert all(record.thread == () for record in dataset)


def test_context_can_be_switched_off(review_comments: List[Comment]) -> None:
    dataset = build_dataset(review_comments, include_context=False)
    assert all(not record.context_before and not record.context_after
               for record in dataset)


def test_classification_can_be_switched_off(review_comments: List[Comment]) -> None:
    dataset = build_dataset(review_comments, classify=False)
    assert all(record.category == "Other" for record in dataset)
    assert all(record.category_confidence == 0.0 for record in dataset)


def test_a_record_is_json_serialisable(review_records: List[LLMRecord]) -> None:
    payload = json.dumps([record.to_dict() for record in review_records])
    assert json.loads(payload)[0]["comment_id"] == 0


def test_dates_are_iso_strings(review_records: List[LLMRecord]) -> None:
    assert review_records[0].date.startswith("2024-03-01T10:00:00")


def test_a_record_renders_as_readable_text(review_records: List[LLMRecord]) -> None:
    text = review_records[0].to_text()
    assert "[#0]" in text
    assert "Alice Tester" in text
    assert "Legal" in text
    assert "says:" in text


def test_text_rendering_is_cheaper_than_json(review_records: List[LLMRecord]) -> None:
    """The reason to_text() exists: it must actually cost fewer tokens."""
    as_text = sum(estimate_tokens(r.to_text()) for r in review_records)
    as_json = estimate_tokens(json.dumps([r.to_dict() for r in review_records]))
    assert as_text < as_json


def test_threads_from_different_documents_never_merge() -> None:
    """Comment ids are unique per file; a batch export holds several."""
    rows = [
        make_comment(0, "Alice Tester", "First doc.", document="a.docx"),
        make_comment(0, "Bob Reviewer", "Second doc.", document="b.docx"),
    ]
    dataset = build_dataset(rows, include_context=False)
    assert all(len(record.thread) == 1 for record in dataset)


def test_an_empty_export_produces_no_records() -> None:
    dataset = build_dataset([])
    assert len(dataset) == 0
    assert dataset.to_dicts() == []


# ─────────────────────────────────────────────────────────────────────────────
#  Anonymised records
# ─────────────────────────────────────────────────────────────────────────────


def test_anonymisation_happens_before_a_record_exists(
    review_comments: List[Comment],
) -> None:
    dataset = build_dataset(review_comments, anonymize="strict", include_context=False)
    joined = " ".join(f"{r.author} {r.comment}" for r in dataset)
    assert "Alice" not in joined
    assert "Bob Reviewer" not in joined
    assert dataset.is_anonymized
    assert dataset.anonymization is not None and dataset.anonymization.clean


def test_the_vault_comes_back_on_the_dataset(review_comments: List[Comment]) -> None:
    """Without it the pseudonyms would be permanent and the feature useless."""
    dataset = build_dataset(review_comments, anonymize="balanced", include_context=False)
    assert dataset.anonymizer is not None
    answer = f"{dataset[0].author} raised the first issue."
    assert "Alice Tester" in dataset.anonymizer.deanonymize(answer)


def test_the_thread_inside_a_record_is_anonymised_too(
    review_comments: List[Comment],
) -> None:
    """A record can leak through its own thread copy as easily as its own text."""
    dataset = build_dataset(review_comments, anonymize="strict", include_context=False)
    for record in dataset:
        for entry in record.thread:
            assert "Alice" not in str(entry["author"])
            assert "Alice" not in str(entry["text"])


def test_anonymisation_is_off_unless_asked_for(
    review_records: List[LLMRecord],
) -> None:
    assert any(record.author == "Alice Tester" for record in review_records)


# ─────────────────────────────────────────────────────────────────────────────
#  Embeddings input
# ─────────────────────────────────────────────────────────────────────────────


def test_embeddings_default_to_one_document_per_thread(
    review_comments: List[Comment],
) -> None:
    documents = to_embeddings_input(review_comments, include_context=False)
    assert len(documents) == 4  # five comments, one of them a reply
    assert all(doc["id"].count("#thread-") == 1 for doc in documents)


def test_an_embedding_document_has_the_shape_a_vector_store_wants(
    review_comments: List[Comment],
) -> None:
    document = to_embeddings_input(review_comments, include_context=False)[0]
    assert set(document) == {"id", "text", "metadata"}
    assert all(
        isinstance(value, (str, int, float, bool, type(None)))
        for value in document["metadata"].values()
    )


def test_a_thread_document_contains_its_replies(
    review_comments: List[Comment],
) -> None:
    documents = to_embeddings_input(review_comments, include_context=False)
    thread = next(doc for doc in documents if doc["id"].endswith("thread-0"))
    assert "asked counsel" in thread["text"]
    assert thread["metadata"]["thread_size"] == 2


def test_comment_granularity_gives_one_document_each(
    review_comments: List[Comment],
) -> None:
    documents = to_embeddings_input(
        review_comments, granularity="comment", include_context=False
    )
    assert len(documents) == len(review_comments)


def test_an_unknown_granularity_is_rejected(review_comments: List[Comment]) -> None:
    with pytest.raises(ValueError, match="granularity"):
        to_embeddings_input(review_comments, granularity="paragraph")


def test_embeddings_can_be_anonymised(review_comments: List[Comment]) -> None:
    documents = to_embeddings_input(
        review_comments, anonymize="strict", include_context=False
    )
    joined = json.dumps(documents)
    assert "Alice" not in joined


# ─────────────────────────────────────────────────────────────────────────────
#  JSONL
# ─────────────────────────────────────────────────────────────────────────────


def test_jsonl_is_one_object_per_line(review_records: List[LLMRecord]) -> None:
    lines = to_jsonl(review_records).splitlines()
    assert len(lines) == len(review_records)
    assert all(json.loads(line)["comment_id"] >= 0 for line in lines)


def test_jsonl_export_writes_a_readable_file(
    review_records: List[LLMRecord], tmp_path: Path
) -> None:
    target = export_jsonl(review_records, tmp_path / "out" / "records.jsonl")
    lines = target.read_text(encoding="utf-8").splitlines()
    assert len(lines) == len(review_records)
    assert json.loads(lines[0])["author"] == "Alice Tester"


def test_jsonl_accepts_plain_dicts(tmp_path: Path) -> None:
    target = export_jsonl([{"a": 1}, {"a": 2}], tmp_path / "plain.jsonl")
    assert target.read_text(encoding="utf-8").splitlines() == ['{"a": 1}', '{"a": 2}']


def test_jsonl_keeps_non_ascii_readable(tmp_path: Path) -> None:
    target = export_jsonl([{"author": "Νίκος"}], tmp_path / "unicode.jsonl")
    assert "Νίκος" in target.read_text(encoding="utf-8")


# ─────────────────────────────────────────────────────────────────────────────
#  Token counting and chunking
# ─────────────────────────────────────────────────────────────────────────────


def test_the_token_backend_names_itself() -> None:
    assert token_backend() in {"tiktoken", "estimate"}


def test_token_estimates_grow_with_text() -> None:
    assert estimate_tokens("") == 0
    assert estimate_tokens("one two three") < estimate_tokens("one two three " * 20)


def test_the_estimate_errs_high_rather_than_low() -> None:
    """Under-counting produces a request the provider rejects."""
    prose = "The quick brown fox jumps over the lazy dog. " * 20
    words = len(prose.split())
    assert estimate_tokens(prose) >= words


def test_a_tiktoken_encoder_is_used_when_present(monkeypatch: pytest.MonkeyPatch) -> None:
    """The optional back-end, exercised without requiring the package."""
    from docx_comment_parser.llm import chunking

    class FakeEncoder:
        def encode(self, text: str, disallowed_special: Any = ()) -> List[int]:
            return [0] * len(text.split())

    monkeypatch.setattr(chunking, "_encoder", FakeEncoder())
    monkeypatch.setattr(chunking, "_probed", True)
    assert chunking.token_backend() == "tiktoken"
    assert chunking.estimate_tokens("one two three") == 3


def test_chunking_keeps_a_thread_whole(review_records: List[LLMRecord]) -> None:
    """The one hard promise: a reply must never arrive without its comment."""
    chunks = chunk_records(review_records, max_tokens=120)
    seen: Dict[int, int] = {}
    for chunk in chunks:
        for record in chunk.records:
            assert seen.setdefault(record.thread_id, chunk.index) == chunk.index


def test_an_oversized_thread_is_emitted_whole_not_cut(
    review_records: List[LLMRecord],
) -> None:
    chunks = chunk_records(review_records, max_tokens=1)
    assert all(len(chunk.threads) == 1 for chunk in chunks)
    assert sum(len(chunk.records) for chunk in chunks) == len(review_records)


def test_chunks_know_their_position(review_records: List[LLMRecord]) -> None:
    chunks = chunk_records(review_records, max_tokens=120)
    assert chunks[0].is_first
    assert chunks[-1].is_last
    assert all(chunk.total == len(chunks) for chunk in chunks)


def test_a_generous_budget_produces_one_chunk(
    review_records: List[LLMRecord],
) -> None:
    chunks = chunk_records(review_records, max_tokens=1_000_000)
    assert len(chunks) == 1
    assert len(chunks[0].records) == len(review_records)


def test_every_record_appears_exactly_once(review_records: List[LLMRecord]) -> None:
    chunks = chunk_records(review_records, max_tokens=100)
    ids = [record.comment_id for chunk in chunks for record in chunk.records]
    assert sorted(ids) == sorted(record.comment_id for record in review_records)


def test_threads_can_be_allowed_to_split(review_records: List[LLMRecord]) -> None:
    chunks = chunk_records(review_records, max_tokens=100, keep_threads_together=False)
    assert len(chunks) >= len(chunk_records(review_records, max_tokens=100))


def test_chunk_token_counts_include_the_overhead(
    review_records: List[LLMRecord],
) -> None:
    chunk = chunk_records(review_records, max_tokens=1_000_000)[0]
    bare = sum(estimate_tokens(r.to_text()) for r in review_records)
    assert chunk.tokens == bare + _RECORD_OVERHEAD_TOKENS * len(review_records)


def test_no_records_produce_no_chunks() -> None:
    assert chunk_records([], max_tokens=1000) == []


def test_a_non_positive_budget_is_rejected(review_records: List[LLMRecord]) -> None:
    with pytest.raises(ValueError, match="max_tokens"):
        chunk_records(review_records, max_tokens=0)


# ── Multi-document datasets ──────────────────────────────────────────────────
#
# A comment id is only unique within one document — Word numbers each file from
# scratch — so two documents in one dataset routinely both have a thread rooted
# at id 1. Everything downstream that identifies a thread must therefore key on
# (document, thread_id). These tests use a dataset built from two real .docx
# files with deliberately colliding ids.


def _relabel(records: List[LLMRecord], document: str) -> List[LLMRecord]:
    """The same records, attributed to a different document."""
    import dataclasses

    return [dataclasses.replace(record, document=document) for record in records]


def test_threads_from_different_documents_are_never_merged(
    review_records: List[LLMRecord],
) -> None:
    """Two documents, colliding root ids. Merging them puts one document's
    reply under another document's comment — a record that reads perfectly and
    is entirely wrong."""
    mixed = _relabel(review_records, "a.docx") + _relabel(review_records, "b.docx")
    chunks = chunk_records(mixed, max_tokens=1_000_000)

    assert len(chunks) == 1
    keys = chunks[0].thread_keys
    assert len(keys) == len(set(keys))
    assert {document for document, _ in keys} == {"a.docx", "b.docx"}

    distinct_threads = {(r.document, r.thread_id) for r in mixed}
    assert set(keys) == distinct_threads
    assert len(chunks[0].threads) == len(distinct_threads)


def test_a_colliding_root_id_is_reported_once_per_document(
    review_records: List[LLMRecord],
) -> None:
    """chunk.threads exposes ids, and the same id appearing twice is the
    truthful answer when two documents each have a thread with that root."""
    root = review_records[0].thread_id
    mixed = _relabel([review_records[0]], "a.docx") + _relabel(
        [review_records[0]], "b.docx"
    )
    chunk = chunk_records(mixed, max_tokens=1_000_000)[0]

    assert chunk.threads == (root, root)
    assert chunk.thread_keys == (("a.docx", root), ("b.docx", root))


def test_a_thread_split_across_documents_is_packed_per_document(
    review_records: List[LLMRecord],
) -> None:
    """Grouping keys on the document, so a tight budget separates the two
    documents' same-id threads instead of packing them as one oversized group."""
    mixed = _relabel(review_records, "a.docx") + _relabel(review_records, "b.docx")
    chunks = chunk_records(mixed, max_tokens=150)

    seen: Dict[Any, int] = {}
    for chunk in chunks:
        for record in chunk.records:
            key = (record.document, record.thread_id)
            assert seen.setdefault(key, chunk.index) == chunk.index
    assert sum(len(chunk.records) for chunk in chunks) == len(mixed)


def test_thread_keys_and_threads_agree_on_count(
    review_records: List[LLMRecord],
) -> None:
    for chunk in chunk_records(review_records, max_tokens=120):
        assert len(chunk.threads) == len(chunk.thread_keys)


def test_a_custom_token_counter_is_used(review_records: List[LLMRecord]) -> None:
    chunks = chunk_records(
        review_records, max_tokens=100, token_counter=lambda text: 1
    )
    assert len(chunks) == 1


# ── chunk_texts ──────────────────────────────────────────────────────────────
#
# These pass an explicit ``token_counter`` wherever the assertion depends on a
# specific number of batches.  The default counter is tiktoken when the [llm]
# extra is installed and a character heuristic when it is not, and the two
# disagree by an order of magnitude on degenerate input — 400 repeated "a"s is
# ~112 tokens by the heuristic and ~25 under a real BPE vocabulary, which merges
# the run.  A packing test written against the heuristic's number therefore
# stops testing packing and starts testing which extras are installed.  Pinning
# the counter tests the thing the function actually promises: the budget is
# respected, nothing is dropped, and order is preserved, whatever the counter
# says.

#: One token per character — the arithmetic below is then readable by eye.
_PER_CHAR = len


def test_chunk_texts_splits_when_the_corpus_exceeds_the_budget() -> None:
    batches = chunk_texts(
        ["a" * 100, "b" * 100, "c" * 100], max_tokens=250, token_counter=_PER_CHAR
    )
    assert [len(batch) for batch in batches] == [2, 1]


def test_chunk_texts_keeps_every_text_exactly_once_and_in_order() -> None:
    texts = [f"text-{index} " + "word " * (index % 7) for index in range(50)]
    batches = chunk_texts(texts, max_tokens=60, token_counter=_PER_CHAR)
    assert [text for batch in batches for text in batch] == texts


def test_no_chunk_text_batch_exceeds_the_budget() -> None:
    """The whole point of a budget: nothing over it, except a single text that
    is over it on its own."""
    texts = ["word " * length for length in range(1, 40)]
    budget = 300
    batches = chunk_texts(texts, max_tokens=budget, token_counter=_PER_CHAR)
    for batch in batches:
        if len(batch) == 1:
            continue  # an unsplittable text is allowed to exceed, see below
        assert sum(_PER_CHAR(text) for text in batch) <= budget


def test_chunk_texts_packs_greedily_rather_than_one_per_batch() -> None:
    """A correct-but-useless implementation returns one batch per text. This is
    what separates packing from iteration."""
    texts = ["x" * 10] * 20
    batches = chunk_texts(texts, max_tokens=100, token_counter=_PER_CHAR)
    assert len(batches) == 2
    assert all(len(batch) == 10 for batch in batches)


def test_an_oversized_text_is_emitted_alone_not_cut() -> None:
    """Never silently truncate: the caller can measure and decide."""
    huge = "z" * 500
    batches = chunk_texts(
        ["a" * 10, huge, "b" * 10], max_tokens=100, token_counter=_PER_CHAR
    )
    assert [huge] in batches
    assert "".join(text for batch in batches for text in batch) == "a" * 10 + huge + "b" * 10


def test_chunk_texts_accepts_a_one_shot_iterable() -> None:
    """The signature says Iterable, so a generator must not be consumed twice
    into an empty result."""
    batches = chunk_texts(
        (f"item {index}" for index in range(10)),
        max_tokens=25,
        token_counter=_PER_CHAR,
    )
    assert sum(len(batch) for batch in batches) == 10


def test_chunk_texts_handles_empty_and_blank_input() -> None:
    assert chunk_texts([], max_tokens=100) == []
    assert chunk_texts(["", "", ""], max_tokens=100) == [["", "", ""]]


def test_chunk_texts_rejects_a_non_positive_budget() -> None:
    """The same guard chunk_records has. Without it, max_tokens=0 returns one
    batch per text — a plausible-looking result from a nonsense request."""
    for budget in (0, -1):
        with pytest.raises(ValueError, match="max_tokens"):
            chunk_texts(["a", "b"], max_tokens=budget)


def test_chunk_texts_uses_the_real_counter_by_default() -> None:
    """The default counter, asserted without hard-coding its numbers.

    The budget is *derived* from what the counter in use actually charges for
    the fixture, so this holds identically under tiktoken and under the
    heuristic. Room for exactly two paragraphs means exactly two per batch.
    """
    paragraph = (
        "The supplier shall indemnify the customer against all losses arising "
        "from any breach of the confidentiality obligations in clause 9. "
    ) * 12
    cost = estimate_tokens(paragraph)
    assert cost > 0

    batches = chunk_texts([paragraph] * 6, max_tokens=cost * 2)

    assert [len(batch) for batch in batches] == [2, 2, 2]
    for batch in batches:
        assert sum(estimate_tokens(text) for text in batch) <= cost * 2


def test_chunk_texts_gives_the_same_shape_under_either_token_backend(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The back-end changes the numbers, never the guarantees.

    This is the regression guard for the class of bug that made an earlier
    version of this test fail once the [llm] extra was installed: an assertion
    about *how many* batches a fixed budget produces is an assertion about the
    tokenizer, not about the packer.  Here the budget is scaled to each
    back-end's own count, and both must then pack identically.
    """
    from docx_comment_parser.llm import chunking

    texts = [f"Comment {index}: the clause needs review. " * 4 for index in range(9)]

    def shape_under(counter: Any) -> List[int]:
        budget = counter(texts[0]) * 3
        return [len(batch) for batch in chunk_texts(texts, max_tokens=budget,
                                                    token_counter=counter)]

    class WordEncoder:
        def encode(self, text: str, disallowed_special: Any = ()) -> List[int]:
            return [0] * len(text.split())

    heuristic_shape = shape_under(chunking.estimate_tokens)

    monkeypatch.setattr(chunking, "_encoder", WordEncoder())
    monkeypatch.setattr(chunking, "_probed", True)
    assert chunking.token_backend() == "tiktoken"
    encoder_shape = shape_under(chunking.estimate_tokens)

    assert heuristic_shape == encoder_shape == [3, 3, 3]


def test_a_chunk_serialises_both_ways(review_records: List[LLMRecord]) -> None:
    chunk = chunk_records(review_records, max_tokens=1_000_000)[0]
    assert len(json.loads(chunk.to_json())) == len(review_records)
    assert "[#0]" in chunk.to_text()


# ─────────────────────────────────────────────────────────────────────────────
#  Prompts
# ─────────────────────────────────────────────────────────────────────────────

BUILDERS = [
    create_review_summary_prompt,
    create_action_items_prompt,
    create_resolution_prompt,
    create_triage_prompt,
    create_risk_prompt,
]


@pytest.mark.parametrize("builder", BUILDERS, ids=lambda f: f.__name__)
def test_every_builder_produces_a_usable_prompt(
    builder: Any, review_records: List[LLMRecord]
) -> None:
    prompt = builder(review_records)
    assert isinstance(prompt, Prompt)
    assert prompt.system and prompt.user
    assert prompt.tokens > 0


@pytest.mark.parametrize("builder", BUILDERS, ids=lambda f: f.__name__)
def test_every_builder_asks_for_json(
    builder: Any, review_records: List[LLMRecord]
) -> None:
    prompt = builder(review_records)
    assert "JSON" in prompt.system
    assert "{" in prompt.user


@pytest.mark.parametrize("builder", BUILDERS, ids=lambda f: f.__name__)
def test_every_builder_survives_an_empty_corpus(builder: Any) -> None:
    prompt = builder([])
    assert prompt.system and prompt.user
    assert prompt.included == ()


def test_a_prompt_declares_which_comments_it_contains(
    review_records: List[LLMRecord],
) -> None:
    prompt = create_review_summary_prompt(review_records)
    assert set(prompt.included) == {r.comment_id for r in review_records}
    assert prompt.is_complete
    for comment_id in prompt.included:
        assert f"[#{comment_id}]" in prompt.user


def test_a_prompt_says_when_the_corpus_did_not_fit(
    review_records: List[LLMRecord],
) -> None:
    """Silently truncating a review and presenting it as complete is the failure
    this line exists to prevent."""
    prompt = create_review_summary_prompt(review_records, budget=40)
    assert prompt.omitted > 0
    assert not prompt.is_complete
    assert "did not fit" in prompt.user


def test_action_items_drops_resolved_threads_by_default(
    review_records: List[LLMRecord],
) -> None:
    prompt = create_action_items_prompt(review_records)
    resolved = [r.comment_id for r in review_records if r.resolved]
    assert resolved
    assert not set(prompt.included) & set(resolved)


def test_action_items_puts_the_blocker_first(review_records: List[LLMRecord]) -> None:
    prompt = create_action_items_prompt(review_records)
    blocker = next(r for r in review_records if r.priority == "blocker")
    assert prompt.included[0] == blocker.comment_id or prompt.included[1] == blocker.comment_id


def test_resolution_asks_for_replacement_text(
    review_records: List[LLMRecord],
) -> None:
    prompt = create_resolution_prompt(review_records)
    assert "proposed_edit" in prompt.user
    assert "not a description of what to" in prompt.system


def test_triage_only_sends_what_the_rules_were_unsure_about(
    review_records: List[LLMRecord],
) -> None:
    prompt = create_triage_prompt(review_records)
    confident = [
        r.comment_id
        for r in review_records
        if r.category != "Other" and r.category_confidence >= 0.5
    ]
    assert confident
    assert not set(prompt.included) & set(confident)


def test_risk_packs_the_risky_categories_first(
    review_records: List[LLMRecord],
) -> None:
    prompt = create_risk_prompt(review_records)
    first = next(r for r in review_records if r.comment_id == prompt.included[0])
    assert first.category in {"Security", "Legal", "Compliance", "Requirements"}


def test_a_prompt_converts_to_both_provider_shapes(
    review_records: List[LLMRecord],
) -> None:
    prompt = create_review_summary_prompt(review_records)

    openai = prompt.to_messages()
    assert [message["role"] for message in openai] == ["system", "user"]

    anthropic = prompt.to_anthropic()
    assert anthropic["system"] == prompt.system
    assert anthropic["messages"][0]["content"] == prompt.user


def test_a_prompt_serialises(review_records: List[LLMRecord]) -> None:
    payload = create_risk_prompt(review_records).to_dict()
    assert payload["kind"] == "risk"
    assert json.loads(json.dumps(payload))["tokens"] > 0


def test_a_focus_is_carried_into_the_prompt(
    review_records: List[LLMRecord],
) -> None:
    prompt = create_review_summary_prompt(review_records, focus="only the legal points")
    assert "only the legal points" in prompt.user


def test_a_prompt_built_from_anonymised_records_names_nobody(
    review_comments: List[Comment],
) -> None:
    dataset = build_dataset(review_comments, anonymize="strict", include_context=False)
    prompt = create_action_items_prompt(dataset.records)
    assert "Alice" not in prompt.user
    assert "Bob Reviewer" not in prompt.user


def test_the_diff_prompt_takes_a_diff_result(tmp_path: Path) -> None:
    """Where the v1.4 comparison engine and the v1.5 export layer meet."""
    before = build_docx(tmp_path / "v1.docx", REVIEW_SPECS)
    later = list(REVIEW_SPECS)
    later[0] = CommentSpec(
        id=0, author="Alice Tester", initials="AT", date="2024-03-01T10:00:00Z",
        text=REVIEW_SPECS[0].text, anchor=REVIEW_SPECS[0].anchor, done=True,
    )
    after = build_docx(tmp_path / "v2.docx", later)

    result = dcp.compare_comments(str(before), str(after))
    prompt = create_diff_summary_prompt(result)
    assert prompt.kind == "diff_summary"
    assert "converging" in prompt.user
    assert "Resolved" in prompt.user


# ─────────────────────────────────────────────────────────────────────────────
#  Parser and batch integration
# ─────────────────────────────────────────────────────────────────────────────


def test_parser_to_llm_dataset(review_docx: Path) -> None:
    parser = dcp.DocxParser()
    parser.parse(str(review_docx))
    records = parser.to_llm_dataset(chars_before=100, chars_after=100)
    assert len(records) == len(REVIEW_SPECS)
    assert records[0]["context_before"]


def test_parser_to_llm_records_returns_the_dataset(review_docx: Path) -> None:
    parser = dcp.DocxParser()
    parser.parse(str(review_docx))
    dataset = parser.to_llm_records(anonymize="names_only")
    assert dataset.anonymizer is not None
    assert len(dataset) == len(REVIEW_SPECS)


def test_parser_export_jsonl(review_docx: Path, tmp_path: Path) -> None:
    parser = dcp.DocxParser()
    parser.parse(str(review_docx))
    target = parser.export_jsonl(tmp_path / "records.jsonl", include_context=False)
    assert len(target.read_text("utf-8").splitlines()) == len(REVIEW_SPECS)


def test_parser_to_jsonl(review_docx: Path) -> None:
    parser = dcp.DocxParser()
    parser.parse(str(review_docx))
    assert len(parser.to_jsonl(include_context=False).splitlines()) == len(REVIEW_SPECS)


def test_parser_to_llm_chunks(review_docx: Path) -> None:
    parser = dcp.DocxParser()
    parser.parse(str(review_docx))
    chunks = parser.to_llm_chunks(max_tokens=120, include_context=False)
    assert sum(len(chunk.records) for chunk in chunks) == len(REVIEW_SPECS)


def test_parser_to_embeddings_input(review_docx: Path) -> None:
    parser = dcp.DocxParser()
    parser.parse(str(review_docx))
    assert len(parser.to_embeddings_input(granularity="comment")) == len(REVIEW_SPECS)


def test_batch_llm_export_spans_every_document(batch_dir: Path) -> None:
    parser = dcp.BatchParser()
    parser.parse_all([str(path) for path in sorted(batch_dir.glob("doc*.docx"))])

    records = parser.to_llm_dataset(include_context=False)
    documents = {record["document"] for record in records}
    assert len(documents) == 3


def test_batch_jsonl_export(batch_dir: Path, tmp_path: Path) -> None:
    parser = dcp.BatchParser()
    parser.parse_all([str(path) for path in sorted(batch_dir.glob("doc*.docx"))])
    target = parser.export_jsonl(tmp_path / "batch.jsonl", include_context=False)
    assert target.read_text("utf-8").count("\n") > 10


def test_an_empty_document_exports_nothing(empty_docx: Path) -> None:
    parser = dcp.DocxParser()
    parser.parse(str(empty_docx))
    assert parser.to_llm_dataset() == []


def test_a_unicode_document_survives_the_whole_pipeline(unicode_docx: Path) -> None:
    parser = dcp.DocxParser()
    parser.parse(str(unicode_docx))
    records = parser.to_llm_dataset()
    payload = json.dumps(records, ensure_ascii=False)
    assert "🚀" in payload
    assert "Νίκος" in payload


def test_a_legacy_document_without_threading_still_exports(legacy_docx: Path) -> None:
    parser = dcp.DocxParser()
    parser.parse(str(legacy_docx))
    records = parser.to_llm_dataset()
    assert len(records) == 2
    assert all(record["thread_id"] == record["comment_id"] for record in records)


# ─────────────────────────────────────────────────────────────────────────────
#  Scale
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.perf
def test_ten_thousand_comments_export_in_reasonable_time(large_docx: Path) -> None:
    import time

    parser = dcp.DocxParser()
    parser.parse(str(large_docx))

    start = time.perf_counter()
    records = parser.to_llm_dataset(chars_before=200, chars_after=200)
    elapsed = time.perf_counter() - start

    assert len(records) == 10_000
    assert elapsed < 60.0, f"10,000-comment LLM export took {elapsed:.1f}s"


@pytest.mark.perf
def test_the_document_is_read_once_however_many_comments(large_docx: Path) -> None:
    """The cache is what keeps a 10,000-comment export from re-parsing the body
    10,000 times — the difference between seconds and hours."""
    from docx_comment_parser.llm.context import ContextCache

    cache = ContextCache()
    reads: List[str] = []
    original = DocumentContext.from_docx

    def counted(path: Any) -> DocumentContext:
        reads.append(str(path))
        return original(path)

    DocumentContext.from_docx = staticmethod(counted)  # type: ignore[method-assign]
    try:
        parser = dcp.DocxParser()
        parser.parse(str(large_docx))
        parser.to_llm_dataset(chars_before=50, chars_after=50)
    finally:
        DocumentContext.from_docx = original  # type: ignore[method-assign]

    assert len(reads) == 1


@pytest.mark.perf
def test_chunking_ten_thousand_records_is_linear(large_docx: Path) -> None:
    import time

    parser = dcp.DocxParser()
    parser.parse(str(large_docx))
    records = list(build_dataset(parser.to_comments(), include_context=False))

    start = time.perf_counter()
    chunks = chunk_records(records, max_tokens=50_000)
    elapsed = time.perf_counter() - start

    assert sum(len(chunk.records) for chunk in chunks) == 10_000
    assert elapsed < 30.0, f"chunking 10,000 records took {elapsed:.1f}s"
