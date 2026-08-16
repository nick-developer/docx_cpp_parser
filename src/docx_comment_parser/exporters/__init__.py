"""Structured export layer.

Every exporter takes a sequence of :class:`~docx_comment_parser.Comment` and
produces one output format.  They are pure functions with no parser state, so
they can be used on comments from any source — a single document, a batch, or a
list you filtered yourself.

The convenience methods on ``DocxParser`` / ``BatchParser``
(``parser.to_dataframe()``, ``parser.export_csv(...)``, ...) are thin wrappers
around exactly these functions.
"""

from __future__ import annotations

from .csv_exporter import export_csv, to_csv_rows
from .dataframe import to_columns, to_dataframe, to_polars
from .json_exporter import export_json, to_dict, to_json

__all__ = [
    "to_dataframe",
    "to_polars",
    "to_columns",
    "to_dict",
    "to_json",
    "export_json",
    "export_csv",
    "to_csv_rows",
]
