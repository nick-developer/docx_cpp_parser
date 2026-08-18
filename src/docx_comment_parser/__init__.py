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
    from datetime import datetime
    from pathlib import Path

    import pandas
    import polars

__version__ = "1.4.0"

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
    # reporting layer (new in 1.3)
    "reporting",
    "build_report_data",
    # comparison layer (new in 1.4)
    "comparison",
    "compare_comments",
    "DiffResult",
    "__version__",
]

PathLike = Union[str, "Path"]


# ═════════════════════════════════════════════════════════════════════════════
#  Lazy access to the reporting and comparison layers
# ═════════════════════════════════════════════════════════════════════════════
# ``dcp.reporting``, ``dcp.compare_comments`` and friends resolve on first use
# rather than at import time.  Building the aggregate dataclasses costs a few
# milliseconds, and a program that only parses documents should not pay it —
# the same reasoning that keeps pandas out of the import path.  PEP 562 makes
# this invisible: the attributes behave exactly as if they had been imported.

#: Attribute name → (submodule, attribute in it, or None for the module itself).
_LAZY: Dict[str, Any] = {
    "reporting": (".reporting", None),
    "build_report_data": (".reporting.analytics", "build_report_data"),
    "comparison": (".comparison", None),
    "compare_comments": (".comparison.diff_engine", "compare_comments"),
    "DiffResult": (".comparison.diff_engine", "DiffResult"),
}


def __getattr__(name: str) -> Any:
    # importlib.import_module, not `from . import reporting`: the `from` form
    # asks the import machinery to getattr the name off this very module, which
    # lands straight back here and recurses until the stack runs out.
    import importlib

    target = _LAZY.get(name)
    if target is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

    module_name, attribute = target
    module = importlib.import_module(module_name, __name__)
    return module if attribute is None else getattr(module, attribute)


def __dir__() -> List[str]:
    return sorted(__all__)


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


def _parser_to_html_report(
    self: DocxParser,
    title: Optional[str] = None,
    generated_at: "Optional[datetime]" = None,
    template: Optional[PathLike] = None,
) -> str:
    """Return this document's review report as an HTML string.

    Requires the ``report`` extra: ``pip install docx-comment-parser[report]``.
    """
    from .reporting import report_builder

    return report_builder.render_html_report(
        _parser_to_comments(self),
        title=title,
        generated_at=generated_at,
        template=template,
    )


def _parser_export_html_report(
    self: DocxParser,
    path: PathLike,
    title: Optional[str] = None,
    generated_at: "Optional[datetime]" = None,
    template: Optional[PathLike] = None,
) -> "Path":
    """Write a self-contained interactive HTML review report.

    The file has no external references, so it opens offline.  Returns the path
    written.  Requires the ``report`` extra.
    """
    from .reporting import report_builder

    return report_builder.export_html_report(
        _parser_to_comments(self),
        path,
        title=title,
        generated_at=generated_at,
        template=template,
    )


def _parser_to_markdown_report(
    self: DocxParser,
    title: Optional[str] = None,
    generated_at: "Optional[datetime]" = None,
    include_threads: bool = True,
) -> str:
    """Return this document's review report as a Markdown string. No extras needed."""
    from .reporting import markdown_report

    return markdown_report.render_markdown_report(
        _parser_to_comments(self),
        title=title,
        generated_at=generated_at,
        include_threads=include_threads,
    )


def _parser_export_markdown_report(
    self: DocxParser,
    path: PathLike,
    title: Optional[str] = None,
    generated_at: "Optional[datetime]" = None,
    include_threads: bool = True,
) -> "Path":
    """Write a Markdown review report. Returns the path written. No extras needed."""
    from .reporting import markdown_report

    return markdown_report.export_markdown_report(
        _parser_to_comments(self),
        path,
        title=title,
        generated_at=generated_at,
        include_threads=include_threads,
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


def _batch_to_html_report(
    self: BatchParser,
    title: Optional[str] = None,
    generated_at: "Optional[datetime]" = None,
    template: Optional[PathLike] = None,
    file_paths: Optional[Iterable[str]] = None,
) -> str:
    """Return one HTML review report covering every parsed file."""
    from .reporting import report_builder

    return report_builder.render_html_report(
        _batch_to_comments(self, file_paths),
        title=title,
        generated_at=generated_at,
        template=template,
    )


def _batch_export_html_report(
    self: BatchParser,
    path: PathLike,
    title: Optional[str] = None,
    generated_at: "Optional[datetime]" = None,
    template: Optional[PathLike] = None,
    file_paths: Optional[Iterable[str]] = None,
) -> "Path":
    """Write one self-contained HTML report covering every parsed file.

    The report's ``Document`` column identifies which file each comment came
    from.  Requires the ``report`` extra.
    """
    from .reporting import report_builder

    return report_builder.export_html_report(
        _batch_to_comments(self, file_paths),
        path,
        title=title,
        generated_at=generated_at,
        template=template,
    )


def _batch_to_markdown_report(
    self: BatchParser,
    title: Optional[str] = None,
    generated_at: "Optional[datetime]" = None,
    include_threads: bool = True,
    file_paths: Optional[Iterable[str]] = None,
) -> str:
    """Return one Markdown review report covering every parsed file."""
    from .reporting import markdown_report

    return markdown_report.render_markdown_report(
        _batch_to_comments(self, file_paths),
        title=title,
        generated_at=generated_at,
        include_threads=include_threads,
    )


def _batch_export_markdown_report(
    self: BatchParser,
    path: PathLike,
    title: Optional[str] = None,
    generated_at: "Optional[datetime]" = None,
    include_threads: bool = True,
    file_paths: Optional[Iterable[str]] = None,
) -> "Path":
    """Write one Markdown review report covering every parsed file."""
    from .reporting import markdown_report

    return markdown_report.export_markdown_report(
        _batch_to_comments(self, file_paths),
        path,
        title=title,
        generated_at=generated_at,
        include_threads=include_threads,
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
    "to_html_report": _parser_to_html_report,
    "export_html_report": _parser_export_html_report,
    "to_markdown_report": _parser_to_markdown_report,
    "export_markdown_report": _parser_export_markdown_report,
}

_BATCH_METHODS: Dict[str, Any] = {
    "to_comments": _batch_to_comments,
    "to_dataframe": _batch_to_dataframe,
    "to_polars": _batch_to_polars,
    "to_dict": _batch_to_dict,
    "to_json": _batch_to_json,
    "export_json": _batch_export_json,
    "export_csv": _batch_export_csv,
    "to_html_report": _batch_to_html_report,
    "export_html_report": _batch_export_html_report,
    "to_markdown_report": _batch_to_markdown_report,
    "export_markdown_report": _batch_export_markdown_report,
}

for _name, _method in _PARSER_METHODS.items():
    setattr(DocxParser, _name, _method)

for _name, _method in _BATCH_METHODS.items():
    setattr(BatchParser, _name, _method)

del _name, _method
