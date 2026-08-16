"""Regression suite for the pre-1.2 public API.

Version 1.2 moved the compiled extension from the top level into
``docx_comment_parser._core`` and wrapped it in a Python package.  Nothing about
the API a user sees was supposed to change.  These tests pin that promise: every
class, attribute, method and exception that 1.1.x exposed must still be
reachable by the same name, from the same import, returning the same values.

If any test in this file fails, the restructure broke backward compatibility.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import docx_comment_parser as dcp
from conftest import CommentSpec, build_docx


# ─────────────────────────────────────────────────────────────────────────────
#  Module surface
# ─────────────────────────────────────────────────────────────────────────────

LEGACY_PUBLIC_NAMES = [
    "DocxParser",
    "BatchParser",
    "CommentMetadata",
    "CommentRef",
    "DocumentCommentStats",
    "DocxParserError",
    "DocxFileError",
    "DocxFormatError",
]


@pytest.mark.parametrize("name", LEGACY_PUBLIC_NAMES)
def test_legacy_names_still_importable(name: str) -> None:
    assert hasattr(dcp, name), f"{name} disappeared from the package namespace"


@pytest.mark.parametrize("name", LEGACY_PUBLIC_NAMES)
def test_legacy_names_importable_via_from_import(name: str) -> None:
    # `from docx_comment_parser import DocxParser` is the most common form in
    # existing code and must resolve to the very same object.
    module = __import__("docx_comment_parser", fromlist=[name])
    assert getattr(module, name) is getattr(dcp, name)


def test_exception_hierarchy_unchanged() -> None:
    assert issubclass(dcp.DocxFileError, dcp.DocxParserError)
    assert issubclass(dcp.DocxFormatError, dcp.DocxParserError)
    # The pybind11 registration maps these onto builtin types; downstream code
    # catching OSError/ValueError must keep working.
    assert issubclass(dcp.DocxFileError, OSError)
    assert issubclass(dcp.DocxFormatError, ValueError)


def test_version_is_reported() -> None:
    assert dcp.__version__ == "1.2.0"


# ─────────────────────────────────────────────────────────────────────────────
#  DocxParser — same assertions as the C++ suite
# ─────────────────────────────────────────────────────────────────────────────


def test_basic_parsing(basic_docx: Path) -> None:
    parser = dcp.DocxParser()
    parser.parse(str(basic_docx))

    assert len(parser.comments()) == 3

    first = parser.find_by_id(0)
    assert first is not None
    assert first.author == "Alice Tester"
    assert first.initials == "AT"
    assert first.date == "2024-03-01T10:00:00Z"
    assert "revision" in first.text
    assert first.paragraph_style == "CommentText"


def test_threading(basic_docx: Path) -> None:
    parser = dcp.DocxParser()
    parser.parse(str(basic_docx))

    root = parser.find_by_id(0)
    reply_one = parser.find_by_id(1)
    reply_two = parser.find_by_id(2)
    assert root is not None and reply_one is not None and reply_two is not None

    assert not root.is_reply
    assert reply_one.is_reply and reply_one.parent_id == 0
    assert reply_two.is_reply and reply_two.parent_id == 0

    assert len(root.replies) == 2
    assert root.thread_ids[0] == 0
    assert len(root.thread_ids) == 3
    assert len(parser.thread(0)) == 3


def test_done_flag(basic_docx: Path) -> None:
    parser = dcp.DocxParser()
    parser.parse(str(basic_docx))

    assert parser.find_by_id(2).done is True  # type: ignore[union-attr]
    assert parser.find_by_id(0).done is False  # type: ignore[union-attr]


def test_anchor_text(basic_docx: Path) -> None:
    parser = dcp.DocxParser()
    parser.parse(str(basic_docx))

    first = parser.find_by_id(0)
    assert first is not None
    assert "quick brown fox" in first.referenced_text


def test_by_author(basic_docx: Path) -> None:
    parser = dcp.DocxParser()
    parser.parse(str(basic_docx))

    assert len(parser.by_author("Alice Tester")) == 2
    assert len(parser.by_author("Bob Reviewer")) == 1
    assert parser.by_author("Nobody") == []


def test_root_comments(basic_docx: Path) -> None:
    parser = dcp.DocxParser()
    parser.parse(str(basic_docx))

    roots = parser.root_comments()
    assert len(roots) == 1
    assert roots[0].id == 0


def test_find_by_id_missing_returns_none(basic_docx: Path) -> None:
    parser = dcp.DocxParser()
    parser.parse(str(basic_docx))
    assert parser.find_by_id(9999) is None


def test_stats(basic_docx: Path) -> None:
    parser = dcp.DocxParser()
    parser.parse(str(basic_docx))
    stats = parser.stats()

    assert stats.total_comments == 3
    assert stats.total_resolved == 1
    assert stats.total_replies == 2
    assert stats.total_root_comments == 1
    assert sorted(stats.unique_authors) == ["Alice Tester", "Bob Reviewer"]
    assert stats.file_path == str(basic_docx)
    assert stats.earliest_date <= stats.latest_date


# ─────────────────────────────────────────────────────────────────────────────
#  The C++ to_dict() methods — distinct from the new export layer
# ─────────────────────────────────────────────────────────────────────────────

CPP_COMMENT_DICT_KEYS = {
    "id", "author", "date", "initials", "text", "paragraph_style",
    "range_start_para_id", "range_end_para_id", "referenced_text", "is_reply",
    "parent_id", "para_id", "para_id_parent", "done", "paragraph_index",
    "run_index", "thread_ids", "replies",
}


def test_comment_metadata_to_dict_keys_unchanged(basic_docx: Path) -> None:
    parser = dcp.DocxParser()
    parser.parse(str(basic_docx))

    payload = parser.comments()[0].to_dict()
    assert set(payload) == CPP_COMMENT_DICT_KEYS
    assert payload["id"] == 0
    assert payload["author"] == "Alice Tester"
    # Replies are nested dicts, not objects.
    assert isinstance(payload["replies"], list)
    assert set(payload["replies"][0]) == {"id", "author", "date", "text_snippet"}


def test_stats_to_dict_keys_unchanged(basic_docx: Path) -> None:
    parser = dcp.DocxParser()
    parser.parse(str(basic_docx))

    payload = parser.stats().to_dict()
    assert set(payload) == {
        "file_path", "total_comments", "total_resolved", "total_replies",
        "total_root_comments", "unique_authors", "earliest_date", "latest_date",
    }


def test_reprs_still_render(basic_docx: Path) -> None:
    parser = dcp.DocxParser()
    parser.parse(str(basic_docx))

    assert "CommentMetadata" in repr(parser.comments()[0])
    assert "[reply]" in repr(parser.comments()[1])
    assert "DocumentCommentStats" in repr(parser.stats())
    assert "CommentRef" in repr(parser.comments()[0].replies[0])


# ─────────────────────────────────────────────────────────────────────────────
#  Error handling
# ─────────────────────────────────────────────────────────────────────────────


def test_missing_file_raises_file_error(tmp_path: Path) -> None:
    parser = dcp.DocxParser()
    with pytest.raises(dcp.DocxFileError):
        parser.parse(str(tmp_path / "does_not_exist.docx"))


def test_corrupt_file_raises_parser_error(tmp_path: Path) -> None:
    corrupt = tmp_path / "corrupt.docx"
    corrupt.write_bytes(b"definitely not a zip file")

    parser = dcp.DocxParser()
    with pytest.raises(dcp.DocxParserError):
        parser.parse(str(corrupt))


@pytest.mark.parametrize(
    "caught_as", [dcp.DocxFileError, dcp.DocxParserError, OSError, RuntimeError]
)
def test_file_error_is_catchable_as_every_documented_base(
    tmp_path: Path, caught_as: type
) -> None:
    """A missing file must be catchable specifically *and* by any base.

    Before 1.2 the base-class translator was registered last and therefore ran
    first, so `except DocxFileError` never matched anything.  Code written
    against the README's exception table silently fell through.
    """
    parser = dcp.DocxParser()
    with pytest.raises(caught_as):
        parser.parse(str(tmp_path / "does_not_exist.docx"))


def test_stats_are_reset_between_parses(basic_docx: Path, empty_docx: Path) -> None:
    """Parsing a comment-free document must clear the previous document's stats.

    The two early returns in DocxParser::Impl::parse used to skip
    compute_stats(), leaking the prior file's totals and path.
    """
    parser = dcp.DocxParser()
    parser.parse(str(basic_docx))
    parser.parse(str(empty_docx))

    stats = parser.stats()
    assert stats.file_path == str(empty_docx)
    assert stats.total_comments == 0
    assert stats.total_resolved == 0
    assert stats.total_replies == 0
    assert stats.total_root_comments == 0
    assert stats.unique_authors == []
    assert stats.earliest_date == ""
    assert stats.latest_date == ""


# ─────────────────────────────────────────────────────────────────────────────
#  Documents with unusual shapes
# ─────────────────────────────────────────────────────────────────────────────


def test_document_with_no_comments(empty_docx: Path) -> None:
    parser = dcp.DocxParser()
    parser.parse(str(empty_docx))

    assert parser.comments() == []
    assert parser.stats().total_comments == 0
    assert parser.root_comments() == []


def test_document_without_comments_extended(legacy_docx: Path) -> None:
    """Pre-2016 documents have no threading or resolved information."""
    parser = dcp.DocxParser()
    parser.parse(str(legacy_docx))

    comments = parser.comments()
    assert len(comments) == 2
    assert all(not c.is_reply for c in comments)
    assert all(not c.done for c in comments)


def test_unicode_round_trip(unicode_docx: Path) -> None:
    parser = dcp.DocxParser()
    parser.parse(str(unicode_docx))

    first = parser.find_by_id(0)
    assert first is not None
    assert first.author == "Νίκος Ηλιόπουλος"
    assert "日本語" in first.text
    assert "🚀" in first.text

    second = parser.find_by_id(1)
    assert second is not None
    assert second.author == "Zoë Müller"
    # XML-escaped characters must come back as the literal characters.
    assert '"and"' in second.text
    assert "<angle>" in second.text
    assert "&" in second.text


def test_parser_is_reusable_across_files(basic_docx: Path, empty_docx: Path) -> None:
    """A second parse() must fully replace the first result, not append to it."""
    parser = dcp.DocxParser()
    parser.parse(str(basic_docx))
    assert len(parser.comments()) == 3

    parser.parse(str(empty_docx))
    assert parser.comments() == []
    assert parser.stats().file_path == str(empty_docx)


# ─────────────────────────────────────────────────────────────────────────────
#  BatchParser
# ─────────────────────────────────────────────────────────────────────────────


def test_batch_parsing(batch_dir: Path) -> None:
    paths = sorted(str(p) for p in batch_dir.glob("doc*.docx"))
    parser = dcp.BatchParser(max_threads=4)
    parser.parse_all(paths)

    assert parser.errors() == {}
    for path in paths:
        assert parser.stats(path).total_comments > 0
        assert len(parser.comments(path)) == parser.stats(path).total_comments


def test_batch_records_errors_without_raising(batch_dir: Path) -> None:
    paths = sorted(str(p) for p in batch_dir.glob("*.docx"))
    parser = dcp.BatchParser(max_threads=2)
    parser.parse_all(paths)  # must not raise despite corrupt.docx

    errors = parser.errors()
    assert len(errors) == 1
    assert str(batch_dir / "corrupt.docx") in errors


def test_batch_release(batch_dir: Path) -> None:
    paths = sorted(str(p) for p in batch_dir.glob("doc*.docx"))
    parser = dcp.BatchParser()
    parser.parse_all(paths)

    parser.release(paths[0])
    with pytest.raises(dcp.DocxParserError):
        parser.comments(paths[0])

    parser.release_all()
    with pytest.raises(dcp.DocxParserError):
        parser.comments(paths[1])


def test_batch_parsed_files_is_sorted_and_excludes_failures(batch_dir: Path) -> None:
    """parsed_files() is new in 1.2; it must never list a failed or freed file."""
    paths = sorted(str(p) for p in batch_dir.glob("*.docx"))
    parser = dcp.BatchParser()
    parser.parse_all(paths)

    parsed = parser.parsed_files()
    assert parsed == sorted(parsed)
    assert str(batch_dir / "corrupt.docx") not in parsed
    assert len(parsed) == 3

    parser.release(parsed[0])
    assert len(parser.parsed_files()) == 2


def test_batch_matches_single_file_parser(batch_dir: Path) -> None:
    """The parallel path must produce byte-identical results to the serial one."""
    path = str(batch_dir / "doc0.docx")

    single = dcp.DocxParser()
    single.parse(path)

    batch = dcp.BatchParser(max_threads=4)
    batch.parse_all([path])

    assert [c.to_dict() for c in single.comments()] == [
        c.to_dict() for c in batch.comments(path)
    ]
    assert single.stats().to_dict() == batch.stats(path).to_dict()


def test_large_document_parses(large_docx: Path) -> None:
    parser = dcp.DocxParser()
    parser.parse(str(large_docx))
    assert parser.stats().total_comments == 10_000


def test_duplicate_ids_do_not_crash(tmp_path: Path) -> None:
    """Malformed input must degrade gracefully rather than abort."""
    specs = [
        CommentSpec(id=0, text="First."),
        CommentSpec(id=0, text="Duplicate id."),
    ]
    path = build_docx(tmp_path / "dupe.docx", specs)

    parser = dcp.DocxParser()
    parser.parse(str(path))
    assert len(parser.comments()) >= 1
