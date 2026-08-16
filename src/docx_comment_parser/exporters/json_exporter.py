"""Plain-dict and JSON export.

Uses nothing but the standard library, so these functions work on a bare
``pip install docx-comment-parser`` with no extras.

The JSON produced is *directly serialisable and directly parseable* — datetimes
are rendered as ISO-8601 strings, never as Python ``repr`` output — so the
result can be posted to an API or handed to ``json.loads`` on the other side
without a custom decoder.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Sequence, Union

from ..models import FIELD_NAMES, Comment

__all__ = ["to_dict", "to_json", "export_json"]

PathLike = Union[str, "Path"]


def _jsonable(value: Any) -> Any:
    """Convert a single field value into something ``json.dumps`` accepts."""
    if isinstance(value, datetime):
        return value.isoformat()
    return value


def to_dict(comments: Sequence[Comment]) -> List[Dict[str, Any]]:
    """Return comments as a list of plain dicts, in canonical field order.

    Values keep their native Python types — ``date_parsed`` stays a
    :class:`datetime.datetime`.  Use :func:`to_json` if you need something that
    ``json.dumps`` can consume unaided.
    """
    return [comment.to_dict() for comment in comments]


def to_json(comments: Sequence[Comment], indent: int = 2) -> str:
    """Serialise comments to a JSON array string.

    Parameters
    ----------
    indent:
        Passed through to :func:`json.dumps`. Use ``0`` for the most compact
        output; the default of 2 stays readable in a terminal or a diff.
    """
    payload = [
        {name: _jsonable(getattr(comment, name)) for name in FIELD_NAMES}
        for comment in comments
    ]
    return json.dumps(
        payload,
        indent=indent if indent > 0 else None,
        ensure_ascii=False,
    )


def export_json(
    comments: Sequence[Comment],
    path: PathLike,
    indent: int = 2,
) -> "Path":
    """Write comments to a UTF-8 JSON file and return the path written.

    Parent directories are created if missing.
    """
    target = Path(path)
    if target.parent and not target.parent.exists():
        target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(to_json(comments, indent=indent), encoding="utf-8")
    return target
