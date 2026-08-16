"""Tests for the Comment dataclass, date parsing and thread-depth resolution."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

import docx_comment_parser as dcp
from docx_comment_parser.models import (
    FIELD_NAMES,
    Comment,
    build_comments,
    parse_ooxml_date,
)
from conftest import CommentSpec, build_docx


# ─────────────────────────────────────────────────────────────────────────────
#  Schema
# ─────────────────────────────────────────────────────────────────────────────

ROADMAP_SCHEMA = [
    "comment_id", "parent_id", "author", "initials", "date", "text",
    "referenced_text", "paragraph_style", "resolved", "is_reply",
    "thread_depth", "document_name",
]


@pytest.mark.parametrize("field", ROADMAP_SCHEMA)
def test_roadmap_schema_fields_present(field: str) -> None:
    assert field in FIELD_NAMES


def test_field_order_is_stable() -> None:
    """Column order is part of the CSV contract; pin the first thirteen."""
    assert FIELD_NAMES[:13] == (
        "comment_id", "parent_id", "author", "initials", "date", "date_parsed",
        "text", "referenced_text", "paragraph_style", "resolved", "is_reply",
        "thread_depth", "document_name",
    )


def test_slots_match_fields() -> None:
    """__slots__ is hand-written for 3.9 compatibility, so guard against drift."""
    assert Comment.__slots__ == FIELD_NAMES


def test_comment_instances_have_no_dict() -> None:
    """Slots must actually be in effect — this is the memory win."""
    comment = Comment(*[None] * len(FIELD_NAMES))  # type: ignore[arg-type]
    assert not hasattr(comment, "__dict__")
    with pytest.raises(AttributeError):
        comment.unexpected_attribute = 1  # type: ignore[attr-defined]


def test_comment_to_dict_matches_field_names(basic_docx: Path) -> None:
    parser = dcp.DocxParser()
    parser.parse(str(basic_docx))
    payload = parser.to_comments()[0].to_dict()
    assert list(payload) == list(FIELD_NAMES)


# ─────────────────────────────────────────────────────────────────────────────
#  Date parsing
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("2024-03-01T10:00:00Z", datetime(2024, 3, 1, 10, 0, tzinfo=timezone.utc)),
        ("2024-03-01T10:00:00z", datetime(2024, 3, 1, 10, 0, tzinfo=timezone.utc)),
        ("2024-03-01T10:00:00", datetime(2024, 3, 1, 10, 0, tzinfo=timezone.utc)),
        ("2024-03-01T10:00:00+00:00", datetime(2024, 3, 1, 10, 0, tzinfo=timezone.utc)),
        ("  2024-03-01T10:00:00Z  ", datetime(2024, 3, 1, 10, 0, tzinfo=timezone.utc)),
    ],
)
def test_parse_ooxml_date_accepts_word_formats(raw: str, expected: datetime) -> None:
    assert parse_ooxml_date(raw) == expected


def test_parse_ooxml_date_handles_offsets() -> None:
    parsed = parse_ooxml_date("2024-03-01T12:00:00+02:00")
    assert parsed == datetime(2024, 3, 1, 10, 0, tzinfo=timezone.utc)


def test_parse_ooxml_date_clamps_excess_fractional_digits() -> None:
    """Python rejects more than 6 fractional digits; Word sometimes writes them."""
    parsed = parse_ooxml_date("2024-03-01T10:00:00.1234567Z")
    assert parsed is not None
    assert parsed.microsecond == 123456


@pytest.mark.parametrize("raw", ["", "   ", "not a date", "2024-13-45T99:99:99Z"])
def test_parse_ooxml_date_returns_none_for_bad_input(raw: str) -> None:
    assert parse_ooxml_date(raw) is None


def test_malformed_date_does_not_break_export(tmp_path: Path) -> None:
    """One bad timestamp must not abort the whole document."""
    specs = [
        CommentSpec(id=0, date="2024-03-01T10:00:00Z", text="Good."),
        CommentSpec(id=1, date="yesterday afternoon", text="Bad."),
        CommentSpec(id=2, date="", text="Missing."),
    ]
    path = build_docx(tmp_path / "dates.docx", specs)

    parser = dcp.DocxParser()
    parser.parse(str(path))
    comments = parser.to_comments()

    assert comments[0].date_parsed is not None
    assert comments[1].date_parsed is None
    assert comments[2].date_parsed is None
    # The raw strings survive untouched regardless.
    assert comments[1].date == "yesterday afternoon"


# ─────────────────────────────────────────────────────────────────────────────
#  Field mapping
# ─────────────────────────────────────────────────────────────────────────────


def test_field_mapping(basic_docx: Path) -> None:
    parser = dcp.DocxParser()
    parser.parse(str(basic_docx))
    by_id = {c.comment_id: c for c in parser.to_comments()}

    root = by_id[0]
    assert root.author == "Alice Tester"
    assert root.initials == "AT"
    assert root.parent_id is None          # -1 sentinel becomes None
    assert root.resolved is False          # `done` becomes `resolved`
    assert root.is_reply is False
    assert root.thread_depth == 0
    assert root.root_id == 0
    assert root.reply_count == 2
    assert root.document_name == str(basic_docx)
    assert root.paragraph_style == "CommentText"
    assert "quick brown fox" in root.referenced_text

    reply = by_id[2]
    assert reply.parent_id == 0
    assert reply.resolved is True
    assert reply.is_reply is True
    assert reply.thread_depth == 1
    assert reply.root_id == 0
    assert reply.reply_count == 0


def test_document_name_comes_from_parser(basic_docx: Path) -> None:
    parser = dcp.DocxParser()
    parser.parse(str(basic_docx))
    assert all(c.document_name == str(basic_docx) for c in parser.to_comments())


# ─────────────────────────────────────────────────────────────────────────────
#  Thread depth
# ─────────────────────────────────────────────────────────────────────────────


def test_nested_thread_depth(tmp_path: Path) -> None:
    """Depth must follow the full parent chain, not just is_reply."""
    specs = [
        CommentSpec(id=0, text="Root."),
        CommentSpec(id=1, text="Reply.", parent=0),
        CommentSpec(id=2, text="Reply to reply.", parent=1),
        CommentSpec(id=3, text="Third level.", parent=2),
        CommentSpec(id=4, text="Separate root."),
    ]
    path = build_docx(tmp_path / "nested.docx", specs)

    parser = dcp.DocxParser()
    parser.parse(str(path))
    depths = {c.comment_id: c.thread_depth for c in parser.to_comments()}
    roots = {c.comment_id: c.root_id for c in parser.to_comments()}

    assert depths == {0: 0, 1: 1, 2: 2, 3: 3, 4: 0}
    assert roots == {0: 0, 1: 0, 2: 0, 3: 0, 4: 4}


def test_dangling_parent_is_treated_as_root() -> None:
    """A parent id pointing at a comment that does not exist must not vanish."""

    class FakeMeta:
        def __init__(self, id: int, parent_id: int) -> None:
            self.id = id
            self.parent_id = parent_id
            self.author = self.initials = self.date = ""
            self.text = self.referenced_text = self.paragraph_style = ""
            self.para_id = self.para_id_parent = ""
            self.range_start_para_id = self.range_end_para_id = ""
            self.done = self.is_reply = False
            self.paragraph_index = self.run_index = -1
            self.replies: list = []

    rows = build_comments([FakeMeta(1, 999)], "x.docx")
    assert rows[0].thread_depth == 0
    assert rows[0].root_id == 1


def test_cyclic_parents_terminate() -> None:
    """A malformed document with a parent cycle must not hang the exporter."""

    class FakeMeta:
        def __init__(self, id: int, parent_id: int) -> None:
            self.id = id
            self.parent_id = parent_id
            self.author = self.initials = self.date = ""
            self.text = self.referenced_text = self.paragraph_style = ""
            self.para_id = self.para_id_parent = ""
            self.range_start_para_id = self.range_end_para_id = ""
            self.done = self.is_reply = False
            self.paragraph_index = self.run_index = -1
            self.replies: list = []

    # 1 -> 2 -> 1
    rows = build_comments([FakeMeta(1, 2), FakeMeta(2, 1)], "x.docx")
    assert len(rows) == 2
    assert all(row.thread_depth >= 0 for row in rows)


def test_empty_document_produces_no_rows(empty_docx: Path) -> None:
    parser = dcp.DocxParser()
    parser.parse(str(empty_docx))
    assert parser.to_comments() == []
