"""Pandas and Polars export.

Both back-ends are imported lazily, inside the functions that need them.  The
core package must stay dependency-free, so importing ``docx_comment_parser``
never pulls in pandas — you only pay for a DataFrame library if you ask for a
DataFrame.

Rows are built column-first rather than row-first: constructing one list per
field and handing the whole mapping to the DataFrame constructor avoids the
per-row dict allocation and the dtype inference pass that
``DataFrame([{...}, {...}])`` performs, which is what keeps a 10,000-comment
export comfortably inside the one-second budget.
"""

from __future__ import annotations

import operator
from typing import TYPE_CHECKING, Any, Dict, List, Sequence

from ..models import FIELD_NAMES, Comment

if TYPE_CHECKING:  # pragma: no cover - typing only
    import pandas
    import polars

__all__ = ["to_dataframe", "to_polars", "to_columns"]


# Column groups, used to pin dtypes so that an empty export has the same schema
# as a populated one.
_INT_COLUMNS = (
    "comment_id",
    "thread_depth",
    "root_id",
    "reply_count",
    "paragraph_index",
    "run_index",
)
_NULLABLE_INT_COLUMNS = ("parent_id",)
_BOOL_COLUMNS = ("resolved", "is_reply")
_DATETIME_COLUMNS = ("date_parsed",)

_GETTERS = tuple(operator.attrgetter(name) for name in FIELD_NAMES)


def to_columns(comments: Sequence[Comment]) -> Dict[str, List[Any]]:
    """Return ``{field_name: [values...]}`` in canonical field order.

    This is the shared substrate for both DataFrame back-ends, and is useful on
    its own for feeding other columnar tools.
    """
    return {
        name: [getter(comment) for comment in comments]
        for name, getter in zip(FIELD_NAMES, _GETTERS)
    }


def _require(package: str, extra: str) -> Any:
    """Import an optional back-end, or raise an error that says how to get it."""
    try:
        return __import__(package)
    except ImportError as exc:  # pragma: no cover - depends on environment
        raise ImportError(
            f"{package} is required for this export but is not installed.\n"
            f"Install it with:  pip install docx-comment-parser[{extra}]"
        ) from exc


def to_dataframe(comments: Sequence[Comment]) -> "pandas.DataFrame":
    """Convert comments to a :class:`pandas.DataFrame`.

    Dtypes are pinned rather than inferred, so the schema is identical whether
    the document has ten thousand comments or none:

    * integer columns are ``int64``
    * ``parent_id`` is the nullable ``Int64`` (a root comment is ``<NA>``, not
      ``NaN`` — it stays an integer column)
    * ``resolved`` / ``is_reply`` are ``bool``
    * ``date_parsed`` is ``datetime64[ns, UTC]``, with ``NaT`` for unparseable
      timestamps
    * everything else is ``object`` (str)

    Requires the ``pandas`` extra.
    """
    pd = _require("pandas", "pandas")

    frame = pd.DataFrame(to_columns(comments), columns=list(FIELD_NAMES))

    for column in _INT_COLUMNS:
        frame[column] = frame[column].astype("int64")
    for column in _NULLABLE_INT_COLUMNS:
        frame[column] = frame[column].astype("Int64")
    for column in _BOOL_COLUMNS:
        frame[column] = frame[column].astype("bool")
    for column in _DATETIME_COLUMNS:
        frame[column] = pd.to_datetime(frame[column], utc=True)

    return frame


def to_polars(comments: Sequence[Comment]) -> "polars.DataFrame":
    """Convert comments to a :class:`polars.DataFrame`.

    An explicit schema is supplied so that an empty result still has the full
    set of correctly typed columns instead of collapsing to ``Null`` dtypes.

    Requires the ``polars`` extra.
    """
    pl = _require("polars", "polars")

    schema: Dict[str, Any] = {}
    for name in FIELD_NAMES:
        if name in _BOOL_COLUMNS:
            schema[name] = pl.Boolean
        elif name in _DATETIME_COLUMNS:
            schema[name] = pl.Datetime(time_unit="us", time_zone="UTC")
        elif name in _INT_COLUMNS or name in _NULLABLE_INT_COLUMNS:
            schema[name] = pl.Int64
        else:
            schema[name] = pl.Utf8

    return pl.DataFrame(to_columns(comments), schema=schema)
