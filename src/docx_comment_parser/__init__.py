"""docx_comment_parser — fast .docx comment extraction with structured export.

The parsing engine is C++ (``docx_comment_parser._core``); this package re-exports
it unchanged and adds a pure-Python export layer on top.  Existing code keeps
working verbatim::

    import docx_comment_parser as dcp

    parser = dcp.DocxParser()
    parser.parse("review.docx")
    for comment in parser.comments():
        print(comment.author, comment.text)

New in 1.2, the same parser can hand you a table instead of objects::

    df = parser.to_dataframe()          # pandas
    parser.export_csv("comments.csv")   # no extra dependencies
    payload = parser.to_json()

and the whole thing is available from a terminal via ``docx-comments``.

Export methods are attached to the compiled ``DocxParser`` / ``BatchParser``
classes at import time.  That is deliberate: it keeps a single parser object as
the one thing a user needs to hold, rather than introducing a second wrapper
type that would have to mirror the C++ API forever.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Dict, Iterable, List, Optional, Sequence, Union

# ─── Compiled core ───────────────────────────────────────────────────────────
# Re-exported verbatim so `dcp.DocxParser` etc. resolve exactly as they did
# when the extension itself was the top-level module.
from ._core import (
    BatchParser,
    CommentMetadata,
    CommentRef,
    DocumentCommentStats,
    DocxFileError,
    DocxFormatError,
    DocxParser,
    DocxParserError,
)

from . import exporters
from .exporters import csv_exporter, dataframe, json_exporter
from .models import Comment, FIELD_NAMES, build_comments, parse_ooxml_date

if TYPE_CHECKING:  # pragma: no cover - typing only
    from pathlib import Path

    import pandas
    import polars

__version__ = "1.2.0"

__all__ = [
    # compiled core (unchanged public API)
    "DocxParser",
    "BatchParser",
    "CommentMetadata",
    "CommentRef",
    "DocumentCommentStats",
    "DocxParserError",
    "DocxFileError",
    "DocxFormatError",
    # export layer
    "Comment",
    "FIELD_NAMES",
    "build_comments",
    "parse_ooxml_date",
    "exporters",
    "__version__",
]

PathLike = Union[str, "Path"]


# ═════════════════════════════════════════════════════════════════════════════
#  DocxParser export methods
# ═════════════════════════════════════════════════════════════════════════════

def _parser_to_comments(self: DocxParser) -> List[Comment]:
    """Return this document's comments as flat :class:`Comment` rows.

    Thread depth and root ids are resolved here; ``document_name`` is taken from
    the path the parser was given.
    """
    return build_comments(self.comments(), self.stats().file_path)


def _parser_to_dataframe(self: DocxParser) -> "pandas.DataFrame":
    """Return this document's comments as a pandas DataFrame.

    Requires the ``pandas`` extra: ``pip install docx-comment-parser[pandas]``.
    """
    return dataframe.to_dataframe(_parser_to_comments(self))


def _parser_to_polars(self: DocxParser) -> "polars.DataFrame":
    """Return this document's comments as a Polars DataFrame.

    Requires the ``polars`` extra: ``pip install docx-comment-parser[polars]``.
    """
    return dataframe.to_polars(_parser_to_comments(self))


def _parser_to_dict(self: DocxParser) -> List[Dict[str, Any]]:
    """Return this document's comments as a list of plain dicts."""
    return json_exporter.to_dict(_parser_to_comments(self))


def _parser_to_json(self: DocxParser, indent: int = 2) -> str:
    """Return this document's comments as a JSON array string."""
    return json_exporter.to_json(_parser_to_comments(self), indent=indent)


def _parser_export_json(
    self: DocxParser, path: PathLike, indent: int = 2
) -> "Path":
    """Write this document's comments to a JSON file. Returns the path written."""
    return json_exporter.export_json(_parser_to_comments(self), path, indent=indent)


def _parser_export_csv(
    self: DocxParser,
    path: PathLike,
    encoding: str = "utf-8",
    delimiter: str = ",",
) -> "Path":
    """Write this document's comments to a CSV file. Returns the path written."""
    return csv_exporter.export_csv(
        _parser_to_comments(self), path, encoding=encoding, delimiter=delimiter
    )


# ═════════════════════════════════════════════════════════════════════════════
#  BatchParser export methods
# ═════════════════════════════════════════════════════════════════════════════

def _batch_to_comments(
    self: BatchParser, file_paths: Optional[Iterable[str]] = None
) -> List[Comment]:
    """Return comments from every parsed file as one flat list.

    Each row's ``document_name`` identifies its source file, so the result can
    be grouped or filtered per document after the fact.

    Parameters
    ----------
    file_paths:
        Restrict the export to these files.  Defaults to every file that parsed
        successfully, in sorted order.
    """
    paths = list(file_paths) if file_paths is not None else self.parsed_files()
    rows: List[Comment] = []
    for path in paths:
        rows.extend(build_comments(self.comments(path), self.stats(path).file_path))
    return rows


def _batch_to_dataframe(
    self: BatchParser, file_paths: Optional[Iterable[str]] = None
) -> "pandas.DataFrame":
    """Return every parsed file's comments as one pandas DataFrame."""
    return dataframe.to_dataframe(_batch_to_comments(self, file_paths))


def _batch_to_polars(
    self: BatchParser, file_paths: Optional[Iterable[str]] = None
) -> "polars.DataFrame":
    """Return every parsed file's comments as one Polars DataFrame."""
    return dataframe.to_polars(_batch_to_comments(self, file_paths))


def _batch_to_dict(
    self: BatchParser, file_paths: Optional[Iterable[str]] = None
) -> List[Dict[str, Any]]:
    """Return every parsed file's comments as a list of plain dicts."""
    return json_exporter.to_dict(_batch_to_comments(self, file_paths))


def _batch_to_json(
    self: BatchParser,
    indent: int = 2,
    file_paths: Optional[Iterable[str]] = None,
) -> str:
    """Return every parsed file's comments as a JSON array string."""
    return json_exporter.to_json(_batch_to_comments(self, file_paths), indent=indent)


def _batch_export_json(
    self: BatchParser,
    path: PathLike,
    indent: int = 2,
    file_paths: Optional[Iterable[str]] = None,
) -> "Path":
    """Write every parsed file's comments to one JSON file."""
    return json_exporter.export_json(
        _batch_to_comments(self, file_paths), path, indent=indent
    )


def _batch_export_csv(
    self: BatchParser,
    path: PathLike,
    encoding: str = "utf-8",
    delimiter: str = ",",
    file_paths: Optional[Iterable[str]] = None,
) -> "Path":
    """Write every parsed file's comments to one CSV file."""
    return csv_exporter.export_csv(
        _batch_to_comments(self, file_paths),
        path,
        encoding=encoding,
        delimiter=delimiter,
    )


# ═════════════════════════════════════════════════════════════════════════════
#  Attach to the compiled classes
# ═════════════════════════════════════════════════════════════════════════════
# pybind11 types are heap types, so class-level assignment works.  Instances
# have no __dict__, which is exactly what we want: the parser stays as compact
# as it was, and these methods add no per-object overhead.

_PARSER_METHODS: Dict[str, Any] = {
    "to_comments": _parser_to_comments,
    "to_dataframe": _parser_to_dataframe,
    "to_polars": _parser_to_polars,
    "to_dict": _parser_to_dict,
    "to_json": _parser_to_json,
    "export_json": _parser_export_json,
    "export_csv": _parser_export_csv,
}

_BATCH_METHODS: Dict[str, Any] = {
    "to_comments": _batch_to_comments,
    "to_dataframe": _batch_to_dataframe,
    "to_polars": _batch_to_polars,
    "to_dict": _batch_to_dict,
    "to_json": _batch_to_json,
    "export_json": _batch_export_json,
    "export_csv": _batch_export_csv,
}

for _name, _method in _PARSER_METHODS.items():
    setattr(DocxParser, _name, _method)

for _name, _method in _BATCH_METHODS.items():
    setattr(BatchParser, _name, _method)

del _name, _method
