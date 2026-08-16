"""Tests for the DataFrame, JSON and CSV export layer."""

from __future__ import annotations

import csv
import json
from pathlib import Path

import pytest

import docx_comment_parser as dcp
from docx_comment_parser.exporters import (
    export_csv,
    export_json,
    to_columns,
    to_dataframe,
    to_dict,
    to_json,
    to_polars,
)
from docx_comment_parser.models import FIELD_NAMES
from conftest import CommentSpec, build_docx

pd = pytest.importorskip("pandas")
pl = pytest.importorskip("polars")


@pytest.fixture()
def parser(basic_docx: Path) -> dcp.DocxParser:
    p = dcp.DocxParser()
    p.parse(str(basic_docx))
    return p


# ─────────────────────────────────────────────────────────────────────────────
#  Pandas
# ─────────────────────────────────────────────────────────────────────────────


def test_to_dataframe_shape_and_columns(parser: dcp.DocxParser) -> None:
    frame = parser.to_dataframe()
    assert list(frame.columns) == list(FIELD_NAMES)
    assert len(frame) == 3


def _assert_utc_datetime(dtype: object) -> None:
    """Assert a UTC-aware datetime dtype without pinning the time resolution.

    pandas 2.x reports datetime64[ns, UTC] and pandas 3.x datetime64[us, UTC];
    the resolution is pandas' business, the timezone awareness is ours.
    """
    assert isinstance(dtype, pd.DatetimeTZDtype)
    assert str(dtype.tz) == "UTC"


def test_to_dataframe_dtypes(parser: dcp.DocxParser) -> None:
    frame = parser.to_dataframe()
    assert frame["comment_id"].dtype == "int64"
    assert frame["thread_depth"].dtype == "int64"
    # Nullable integer, so a root comment stays an integer column rather than
    # being promoted to float by a NaN.
    assert str(frame["parent_id"].dtype) == "Int64"
    assert frame["resolved"].dtype == "bool"
    assert frame["is_reply"].dtype == "bool"
    _assert_utc_datetime(frame["date_parsed"].dtype)


def test_to_dataframe_values(parser: dcp.DocxParser) -> None:
    frame = parser.to_dataframe().set_index("comment_id")

    assert frame.loc[0, "author"] == "Alice Tester"
    assert pd.isna(frame.loc[0, "parent_id"])
    assert frame.loc[1, "parent_id"] == 0
    assert bool(frame.loc[2, "resolved"]) is True
    assert bool(frame.loc[0, "resolved"]) is False
    assert frame.loc[0, "thread_depth"] == 0
    assert frame.loc[1, "thread_depth"] == 1


def test_empty_dataframe_keeps_full_schema(empty_docx: Path) -> None:
    """An empty result must still be groupable/concatenable, so dtypes matter."""
    p = dcp.DocxParser()
    p.parse(str(empty_docx))
    frame = p.to_dataframe()

    assert len(frame) == 0
    assert list(frame.columns) == list(FIELD_NAMES)
    assert frame["comment_id"].dtype == "int64"
    assert frame["resolved"].dtype == "bool"
    _assert_utc_datetime(frame["date_parsed"].dtype)


def test_dataframe_datetimes_are_usable(parser: dcp.DocxParser) -> None:
    frame = parser.to_dataframe()
    # A real datetime column supports datetime arithmetic and .dt accessors.
    # Fixture spans 2024-03-01T10:00Z to 2024-03-03T09:00Z.
    span = frame["date_parsed"].max() - frame["date_parsed"].min()
    assert span == pd.Timedelta(days=1, hours=23)
    assert set(frame["date_parsed"].dt.year) == {2024}


def test_malformed_date_becomes_nat(tmp_path: Path) -> None:
    specs = [
        CommentSpec(id=0, date="2024-03-01T10:00:00Z"),
        CommentSpec(id=1, date="rubbish"),
    ]
    p = dcp.DocxParser()
    p.parse(str(build_docx(tmp_path / "d.docx", specs)))
    frame = p.to_dataframe()

    assert frame["date_parsed"].isna().sum() == 1
    _assert_utc_datetime(frame["date_parsed"].dtype)


# ─────────────────────────────────────────────────────────────────────────────
#  Polars
# ─────────────────────────────────────────────────────────────────────────────


def test_to_polars_shape_and_schema(parser: dcp.DocxParser) -> None:
    frame = parser.to_polars()
    assert frame.columns == list(FIELD_NAMES)
    assert frame.height == 3
    assert frame.schema["comment_id"] == pl.Int64
    assert frame.schema["resolved"] == pl.Boolean
    assert frame.schema["author"] == pl.Utf8


def test_to_polars_values(parser: dcp.DocxParser) -> None:
    frame = parser.to_polars().sort("comment_id")
    assert frame["author"].to_list()[0] == "Alice Tester"
    assert frame["parent_id"].to_list() == [None, 0, 0]
    assert frame["resolved"].to_list() == [False, False, True]


def test_empty_polars_keeps_full_schema(empty_docx: Path) -> None:
    p = dcp.DocxParser()
    p.parse(str(empty_docx))
    frame = p.to_polars()

    assert frame.height == 0
    assert frame.columns == list(FIELD_NAMES)
    assert frame.schema["comment_id"] == pl.Int64


def test_pandas_and_polars_agree(parser: dcp.DocxParser) -> None:
    pandas_frame = parser.to_dataframe()
    polars_frame = parser.to_polars()

    assert pandas_frame["comment_id"].tolist() == polars_frame["comment_id"].to_list()
    assert pandas_frame["author"].tolist() == polars_frame["author"].to_list()
    assert pandas_frame["resolved"].tolist() == polars_frame["resolved"].to_list()


# ─────────────────────────────────────────────────────────────────────────────
#  JSON
# ─────────────────────────────────────────────────────────────────────────────


def test_to_dict(parser: dcp.DocxParser) -> None:
    rows = parser.to_dict()
    assert len(rows) == 3
    assert list(rows[0]) == list(FIELD_NAMES)
    assert rows[0]["author"] == "Alice Tester"


def test_to_json_is_valid_and_round_trips(parser: dcp.DocxParser) -> None:
    payload = json.loads(parser.to_json())
    assert len(payload) == 3
    assert payload[0]["comment_id"] == 0
    assert payload[1]["parent_id"] == 0
    assert payload[0]["parent_id"] is None
    assert payload[2]["resolved"] is True
    # Datetimes are ISO strings, not Python reprs.
    assert payload[0]["date_parsed"].startswith("2024-03-01T10:00:00")


def test_to_json_is_directly_serialisable(parser: dcp.DocxParser) -> None:
    """The roadmap's acceptance criterion: JSON output needs no custom encoder."""
    rows = parser.to_dict()
    # to_dict keeps real datetimes, which plain json.dumps cannot handle...
    with pytest.raises(TypeError):
        json.dumps(rows)
    # ...whereas to_json handles the conversion itself.
    assert json.loads(parser.to_json())


def test_to_json_compact(parser: dcp.DocxParser) -> None:
    compact = parser.to_json(indent=0)
    assert "\n" not in compact
    assert json.loads(compact)


def test_export_json_writes_utf8(unicode_docx: Path, tmp_path: Path) -> None:
    p = dcp.DocxParser()
    p.parse(str(unicode_docx))
    target = p.export_json(tmp_path / "out.json")

    payload = json.loads(target.read_text(encoding="utf-8"))
    assert payload[0]["author"] == "Νίκος Ηλιόπουλος"
    assert "🚀" in payload[0]["text"]


def test_export_json_creates_parent_directories(
    parser: dcp.DocxParser, tmp_path: Path
) -> None:
    target = parser.export_json(tmp_path / "nested" / "deep" / "out.json")
    assert target.exists()


# ─────────────────────────────────────────────────────────────────────────────
#  CSV
# ─────────────────────────────────────────────────────────────────────────────


def test_export_csv_header_and_rows(parser: dcp.DocxParser, tmp_path: Path) -> None:
    target = parser.export_csv(tmp_path / "out.csv")

    with open(target, newline="", encoding="utf-8") as handle:
        rows = list(csv.reader(handle))

    assert rows[0] == list(FIELD_NAMES)
    assert len(rows) == 4  # header + 3 comments
    assert rows[1][FIELD_NAMES.index("author")] == "Alice Tester"


def test_csv_booleans_and_nulls(parser: dcp.DocxParser, tmp_path: Path) -> None:
    target = parser.export_csv(tmp_path / "out.csv")
    with open(target, newline="", encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))

    assert rows[0]["resolved"] == "false"
    assert rows[2]["resolved"] == "true"
    # A root comment's parent is an empty cell, not the text "None".
    assert rows[0]["parent_id"] == ""
    assert rows[1]["parent_id"] == "0"


def test_csv_has_no_blank_rows_on_windows(
    parser: dcp.DocxParser, tmp_path: Path
) -> None:
    """Regression guard for the classic csv+Windows \\r\\r\\n bug."""
    target = parser.export_csv(tmp_path / "out.csv")
    raw = target.read_bytes()
    assert b"\r\r\n" not in raw


def test_csv_survives_embedded_delimiters_and_newlines(
    unicode_docx: Path, tmp_path: Path
) -> None:
    p = dcp.DocxParser()
    p.parse(str(unicode_docx))
    target = p.export_csv(tmp_path / "out.csv")

    with open(target, newline="", encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))

    assert len(rows) == 2
    # The comment text contains a comma, quotes, angle brackets and a newline;
    # all must survive the round trip intact.
    assert '"and"' in rows[1]["text"]
    assert "<angle>" in rows[1]["text"]
    assert "\n" in rows[1]["text"]


def test_csv_round_trips_through_pandas(parser: dcp.DocxParser, tmp_path: Path) -> None:
    target = parser.export_csv(tmp_path / "out.csv")
    frame = pd.read_csv(target)
    assert list(frame.columns) == list(FIELD_NAMES)
    assert len(frame) == 3


def test_csv_custom_delimiter_and_encoding(
    parser: dcp.DocxParser, tmp_path: Path
) -> None:
    target = parser.export_csv(
        tmp_path / "out.csv", encoding="utf-8-sig", delimiter=";"
    )
    raw = target.read_bytes()
    assert raw.startswith(b"\xef\xbb\xbf")  # BOM for Excel
    assert b";" in raw


def test_empty_csv_still_has_header(empty_docx: Path, tmp_path: Path) -> None:
    p = dcp.DocxParser()
    p.parse(str(empty_docx))
    target = p.export_csv(tmp_path / "out.csv")

    with open(target, newline="", encoding="utf-8") as handle:
        rows = list(csv.reader(handle))
    assert rows == [list(FIELD_NAMES)]


# ─────────────────────────────────────────────────────────────────────────────
#  Standalone exporter functions
# ─────────────────────────────────────────────────────────────────────────────


def test_exporters_work_on_arbitrary_comment_lists(parser: dcp.DocxParser) -> None:
    """Exporters are pure functions, so a hand-filtered list works too."""
    subset = [c for c in parser.to_comments() if c.author == "Alice Tester"]

    assert len(to_dataframe(subset)) == 2
    assert to_polars(subset).height == 2
    assert len(to_dict(subset)) == 2
    assert len(json.loads(to_json(subset))) == 2


def test_to_columns_matches_field_order(parser: dcp.DocxParser) -> None:
    columns = to_columns(parser.to_comments())
    assert list(columns) == list(FIELD_NAMES)
    assert len(columns["comment_id"]) == 3


def test_module_level_export_helpers(parser: dcp.DocxParser, tmp_path: Path) -> None:
    comments = parser.to_comments()
    assert export_json(comments, tmp_path / "a.json").exists()
    assert export_csv(comments, tmp_path / "a.csv").exists()


# ─────────────────────────────────────────────────────────────────────────────
#  BatchParser export
# ─────────────────────────────────────────────────────────────────────────────


def test_batch_export_combines_documents(batch_dir: Path) -> None:
    paths = sorted(str(p) for p in batch_dir.glob("doc*.docx"))
    batch = dcp.BatchParser(max_threads=4)
    batch.parse_all(paths)

    frame = batch.to_dataframe()
    # Every source document is identifiable in the combined table.
    assert set(frame["document_name"]) == set(paths)
    assert len(frame) == sum(batch.stats(p).total_comments for p in paths)


def test_batch_export_subset(batch_dir: Path) -> None:
    paths = sorted(str(p) for p in batch_dir.glob("doc*.docx"))
    batch = dcp.BatchParser()
    batch.parse_all(paths)

    frame = batch.to_dataframe(file_paths=[paths[0]])
    assert set(frame["document_name"]) == {paths[0]}


def test_batch_export_all_formats(batch_dir: Path, tmp_path: Path) -> None:
    """Every export entry point on BatchParser produces the same row count."""
    paths = sorted(str(p) for p in batch_dir.glob("doc*.docx"))
    batch = dcp.BatchParser()
    batch.parse_all(paths)

    expected = sum(batch.stats(p).total_comments for p in paths)

    assert len(batch.to_comments()) == expected
    assert batch.to_polars().height == expected
    assert len(batch.to_dict()) == expected
    assert len(json.loads(batch.to_json())) == expected

    json_target = batch.export_json(tmp_path / "batch.json")
    assert len(json.loads(json_target.read_text(encoding="utf-8"))) == expected

    csv_target = batch.export_csv(tmp_path / "batch.csv")
    with open(csv_target, newline="", encoding="utf-8") as handle:
        assert len(list(csv.DictReader(handle))) == expected


def test_batch_export_empty_when_nothing_parsed() -> None:
    batch = dcp.BatchParser()
    assert batch.to_comments() == []
    assert len(batch.to_dataframe()) == 0


def test_export_csv_creates_parent_directories(
    parser: dcp.DocxParser, tmp_path: Path
) -> None:
    target = parser.export_csv(tmp_path / "nested" / "deep" / "out.csv")
    assert target.exists()


def test_batch_export_skips_failed_files(batch_dir: Path, tmp_path: Path) -> None:
    """to_comments() defaults to parsed_files(), so a corrupt file is skipped."""
    paths = sorted(str(p) for p in batch_dir.glob("*.docx"))
    batch = dcp.BatchParser()
    batch.parse_all(paths)

    target = batch.export_csv(tmp_path / "batch.csv")
    with open(target, newline="", encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))

    assert str(batch_dir / "corrupt.docx") not in {r["document_name"] for r in rows}
    assert len(rows) > 0
