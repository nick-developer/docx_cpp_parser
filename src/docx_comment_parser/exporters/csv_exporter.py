"""CSV export.

Standard-library only — no pandas required to get a spreadsheet.

Rows are written one at a time straight to the file handle rather than being
materialised into an intermediate list of strings, so exporting a very large
document costs no more memory than exporting a small one.
"""

from __future__ import annotations

import csv
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Sequence, Union

from ..models import FIELD_NAMES, Comment

__all__ = ["export_csv", "to_csv_rows"]

PathLike = Union[str, "Path"]


def _cell(value: Any) -> Any:
    """Render one field value for a CSV cell.

    ``None`` becomes an empty cell (rather than the literal text ``None``), and
    datetimes become ISO-8601 so the column round-trips through Excel and
    ``pandas.read_csv(parse_dates=...)`` alike.
    """
    if value is None:
        return ""
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, bool):
        # Explicit, so the column reads the same on every platform and locale.
        return "true" if value else "false"
    return value


def to_csv_rows(comments: Iterable[Comment]) -> Iterable[Sequence[Any]]:
    """Yield CSV-ready rows (header first) for the given comments."""
    yield list(FIELD_NAMES)
    for comment in comments:
        yield [_cell(getattr(comment, name)) for name in FIELD_NAMES]


def export_csv(
    comments: Iterable[Comment],
    path: PathLike,
    encoding: str = "utf-8",
    delimiter: str = ",",
) -> "Path":
    """Write comments to a CSV file and return the path written.

    Parameters
    ----------
    encoding:
        Defaults to plain UTF-8.  Pass ``"utf-8-sig"`` if the file will be
        opened by double-clicking it in Excel on Windows, which needs the BOM to
        detect UTF-8 and will otherwise mangle accented author names.
    delimiter:
        Pass ``";"`` for locales where Excel expects semicolon-separated values.

    Parent directories are created if missing.
    """
    target = Path(path)
    if target.parent and not target.parent.exists():
        target.parent.mkdir(parents=True, exist_ok=True)

    # newline="" is required by the csv module; without it Windows turns the
    # writer's \r\n into \r\r\n and every other row comes out blank.
    with open(target, "w", newline="", encoding=encoding) as handle:
        writer = csv.writer(handle, delimiter=delimiter)
        writer.writerows(to_csv_rows(comments))

    return target
