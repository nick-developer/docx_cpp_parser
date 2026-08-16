"""Tests for comment filtering."""

from __future__ import annotations

from pathlib import Path
from typing import List

import pytest

import docx_comment_parser as dcp
from docx_comment_parser.filters import filter_comments
from docx_comment_parser.models import Comment
from conftest import CommentSpec, build_docx


@pytest.fixture(scope="module")
def mixed(tmp_path_factory: pytest.TempPathFactory) -> List[Comment]:
    """A document mixing threads, standalone comments and resolved states."""
    directory = tmp_path_factory.mktemp("filters")
    specs = [
        CommentSpec(id=0, author="Alice Smith", text="Security concern here.",
                    anchor="The login endpoint."),
        CommentSpec(id=1, author="Bob Jones", text="Agreed.", parent=0, done=True),
        CommentSpec(id=2, author="Alice Smith", text="Typo in this word.",
                    done=True, anchor="teh"),
        CommentSpec(id=3, author="Carol White", text="Standalone note."),
    ]
    path = build_docx(directory / "mixed.docx", specs)
    parser = dcp.DocxParser()
    parser.parse(str(path))
    return parser.to_comments()


def _ids(comments: List[Comment]) -> List[int]:
    return sorted(c.comment_id for c in comments)


def test_no_criteria_returns_everything(mixed: List[Comment]) -> None:
    assert _ids(filter_comments(mixed)) == [0, 1, 2, 3]


def test_filter_returns_a_new_list(mixed: List[Comment]) -> None:
    result = filter_comments(mixed)
    assert result is not mixed


def test_author_is_case_insensitive_substring(mixed: List[Comment]) -> None:
    assert _ids(filter_comments(mixed, author="alice")) == [0, 2]
    assert _ids(filter_comments(mixed, author="ALICE SMITH")) == [0, 2]
    assert filter_comments(mixed, author="Nobody") == []


def test_contains_searches_comment_text(mixed: List[Comment]) -> None:
    assert _ids(filter_comments(mixed, contains="security")) == [0]
    assert _ids(filter_comments(mixed, contains="TYPO")) == [2]


def test_contains_also_searches_anchored_text(mixed: List[Comment]) -> None:
    """The interesting keyword is often in the document, not the comment."""
    assert _ids(filter_comments(mixed, contains="login endpoint")) == [0]


def test_resolved_tristate(mixed: List[Comment]) -> None:
    assert _ids(filter_comments(mixed, resolved=True)) == [1, 2]
    assert _ids(filter_comments(mixed, resolved=False)) == [0, 3]
    assert _ids(filter_comments(mixed, resolved=None)) == [0, 1, 2, 3]


def test_threads_only(mixed: List[Comment]) -> None:
    """Only comment 0 and its reply 1 form a real thread."""
    assert _ids(filter_comments(mixed, threads_only=True)) == [0, 1]


def test_threads_only_uses_original_thread_sizes(mixed: List[Comment]) -> None:
    """Combining --author with --threads-only must not erase a real thread.

    Thread membership is a property of the document, so it is computed over the
    full comment set. Filtering to Alice alone still recognises comment 0 as
    part of a thread even though its reply (Bob's) has been filtered away.
    """
    result = filter_comments(mixed, author="alice", threads_only=True)
    assert _ids(result) == [0]


def test_criteria_combine_with_and(mixed: List[Comment]) -> None:
    assert _ids(filter_comments(mixed, author="alice", resolved=True)) == [2]
    assert filter_comments(mixed, author="alice", resolved=True,
                           contains="security") == []


def test_empty_input(empty_docx: Path) -> None:
    parser = dcp.DocxParser()
    parser.parse(str(empty_docx))
    assert filter_comments(parser.to_comments(), author="x") == []
