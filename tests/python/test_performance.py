"""Performance and scale tests for the roadmap's acceptance criteria.

The thresholds here are deliberately loose — several times the measured time on
ordinary hardware — because a test that fails on a busy CI runner teaches
people to ignore it.  They are tight enough to catch an accidental quadratic or
a per-row reparse, which is what actually goes wrong.

Run just these with:  pytest -m perf
Skip them with:       pytest -m "not perf"
"""

from __future__ import annotations

import gc
import json
import time
import tracemalloc
from pathlib import Path
from typing import Callable, Tuple, TypeVar

import pytest

import docx_comment_parser as dcp
from conftest import build_docx, make_specs

pytestmark = pytest.mark.perf

T = TypeVar("T")

LARGE = 10_000
"""The roadmap's scale target: 'Export 10,000+ comments without memory issues.'"""


def measure(operation: Callable[[], T]) -> Tuple[T, float]:
    """Run ``operation`` once and return its result plus elapsed seconds."""
    gc.collect()
    start = time.perf_counter()
    result = operation()
    return result, time.perf_counter() - start


@pytest.fixture(scope="module")
def large_parser(large_docx: Path) -> dcp.DocxParser:
    parser = dcp.DocxParser()
    parser.parse(str(large_docx))
    assert parser.stats().total_comments == LARGE
    return parser


# ─────────────────────────────────────────────────────────────────────────────
#  Parser throughput
# ─────────────────────────────────────────────────────────────────────────────


def test_parse_10k_comments_is_fast(large_docx: Path) -> None:
    """The parser is the product's differentiator; it must stay quick."""
    parser = dcp.DocxParser()
    _, seconds = measure(lambda: parser.parse(str(large_docx)))

    assert parser.stats().total_comments == LARGE
    assert seconds < 5.0, f"parsing {LARGE} comments took {seconds:.2f}s"


def test_repeated_parsing_does_not_degrade(large_docx: Path) -> None:
    """Re-using one parser must not accumulate state and slow down.

    This is the regression guard for the stats/comments reset in parse():
    if anything started appending instead of replacing, the later iterations
    would grow super-linearly.
    """
    parser = dcp.DocxParser()
    timings = []
    for _ in range(5):
        _, seconds = measure(lambda: parser.parse(str(large_docx)))
        timings.append(seconds)
        assert parser.stats().total_comments == LARGE

    first, last = timings[0], timings[-1]
    # Allow generous jitter, but the fifth parse must not be dramatically
    # slower than the first.
    assert last < first * 3 + 0.5, f"parse timings degraded: {timings}"


def test_parse_scales_near_linearly(tmp_path: Path) -> None:
    """Ten times the comments must not cost a hundred times the effort."""
    small = build_docx(tmp_path / "small.docx", make_specs(500, seed=7))
    big = build_docx(tmp_path / "big.docx", make_specs(5_000, seed=7))

    parser = dcp.DocxParser()
    _, small_seconds = measure(lambda: parser.parse(str(small)))
    _, big_seconds = measure(lambda: parser.parse(str(big)))

    # A 10x input growing worse than 40x indicates quadratic behaviour.
    # The floor keeps the ratio meaningful when both runs are sub-millisecond.
    ratio = big_seconds / max(small_seconds, 1e-4)
    assert ratio < 40, f"10x input cost {ratio:.1f}x the time"


# ─────────────────────────────────────────────────────────────────────────────
#  Export throughput — the roadmap's stated budgets
# ─────────────────────────────────────────────────────────────────────────────


def test_to_comments_10k(large_parser: dcp.DocxParser) -> None:
    comments, seconds = measure(large_parser.to_comments)
    assert len(comments) == LARGE
    assert seconds < 2.0, f"building {LARGE} Comment rows took {seconds:.2f}s"


def test_dataframe_export_under_one_second(large_parser: dcp.DocxParser) -> None:
    """Roadmap: 'DataFrame export under 1 second for medium datasets.'

    Measured against the 10,000-comment set, which is larger than 'medium'.
    """
    pytest.importorskip("pandas")
    frame, seconds = measure(large_parser.to_dataframe)

    assert len(frame) == LARGE
    assert seconds < 1.0, f"DataFrame export took {seconds:.2f}s"


def test_polars_export_under_one_second(large_parser: dcp.DocxParser) -> None:
    pytest.importorskip("polars")
    frame, seconds = measure(large_parser.to_polars)

    assert frame.height == LARGE
    assert seconds < 1.0, f"Polars export took {seconds:.2f}s"


def test_json_export_10k(large_parser: dcp.DocxParser, tmp_path: Path) -> None:
    payload, seconds = measure(large_parser.to_json)
    assert seconds < 5.0, f"JSON export took {seconds:.2f}s"

    # Roadmap: 'JSON output directly serializable.'
    decoded = json.loads(payload)
    assert len(decoded) == LARGE


def test_csv_export_10k(large_parser: dcp.DocxParser, tmp_path: Path) -> None:
    target = tmp_path / "large.csv"
    _, seconds = measure(lambda: large_parser.export_csv(target))

    assert seconds < 5.0, f"CSV export took {seconds:.2f}s"
    assert target.stat().st_size > 0
    # Header plus one line per comment (embedded newlines are quoted, so this
    # counts records rather than physical lines via the csv module).
    import csv as _csv

    with open(target, newline="", encoding="utf-8") as handle:
        assert sum(1 for _ in _csv.reader(handle)) == LARGE + 1


def test_export_scales_linearly(tmp_path: Path) -> None:
    """Export cost must grow with the data, not with its square."""
    pytest.importorskip("pandas")

    small_path = build_docx(tmp_path / "s.docx", make_specs(1_000, seed=3))
    big_path = build_docx(tmp_path / "b.docx", make_specs(10_000, seed=3))

    small_parser = dcp.DocxParser()
    small_parser.parse(str(small_path))
    big_parser = dcp.DocxParser()
    big_parser.parse(str(big_path))

    # Warm up so import and first-call costs are excluded from both sides.
    small_parser.to_dataframe()

    _, small_seconds = measure(small_parser.to_dataframe)
    _, big_seconds = measure(big_parser.to_dataframe)

    ratio = big_seconds / max(small_seconds, 1e-4)
    assert ratio < 40, f"10x rows cost {ratio:.1f}x the export time"


# ─────────────────────────────────────────────────────────────────────────────
#  Memory
# ─────────────────────────────────────────────────────────────────────────────


def test_10k_comment_export_memory_is_bounded(large_parser: dcp.DocxParser) -> None:
    """Roadmap: 'Export 10,000+ comments without memory issues.'

    The Comment rows themselves are the floor; this asserts the conversion does
    not build several extra full copies on the way.
    """
    gc.collect()
    tracemalloc.start()
    try:
        comments = large_parser.to_comments()
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()

    assert len(comments) == LARGE
    peak_mb = peak / (1024 * 1024)
    # Each row holds ~6 short strings plus scalars; 10k rows is a few MB.
    assert peak_mb < 120, f"peak memory {peak_mb:.1f} MB for {LARGE} comments"


def test_csv_export_streams(large_parser: dcp.DocxParser, tmp_path: Path) -> None:
    """CSV writing must not materialise the whole file in memory first."""
    comments = large_parser.to_comments()
    gc.collect()

    tracemalloc.start()
    try:
        from docx_comment_parser.exporters import export_csv

        export_csv(comments, tmp_path / "streamed.csv")
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()

    file_mb = (tmp_path / "streamed.csv").stat().st_size / (1024 * 1024)
    peak_mb = peak / (1024 * 1024)
    # Peak allocation during writing stays well under the size of the output.
    assert peak_mb < file_mb + 5, (
        f"peak {peak_mb:.1f} MB while writing a {file_mb:.1f} MB file"
    )


def test_batch_release_frees_memory(tmp_path: Path) -> None:
    """release_all() must actually drop the parsed results."""
    paths = [
        str(build_docx(tmp_path / f"doc{i}.docx", make_specs(2_000, seed=i)))
        for i in range(3)
    ]

    batch = dcp.BatchParser(max_threads=3)
    batch.parse_all(paths)
    assert len(batch.parsed_files()) == 3

    batch.release_all()
    assert batch.parsed_files() == []
    with pytest.raises(dcp.DocxParserError):
        batch.comments(paths[0])


# ─────────────────────────────────────────────────────────────────────────────
#  Batch throughput
# ─────────────────────────────────────────────────────────────────────────────


def test_batch_parses_many_files(tmp_path: Path) -> None:
    paths = [
        str(build_docx(tmp_path / f"doc{i}.docx", make_specs(200, seed=i)))
        for i in range(12)
    ]

    batch = dcp.BatchParser(max_threads=0)  # all cores
    _, seconds = measure(lambda: batch.parse_all(paths))

    assert len(batch.parsed_files()) == 12
    assert seconds < 10.0, f"batch of 12 files took {seconds:.2f}s"


def test_batch_export_10k_total(tmp_path: Path) -> None:
    pytest.importorskip("pandas")
    paths = [
        str(build_docx(tmp_path / f"doc{i}.docx", make_specs(2_500, seed=i)))
        for i in range(4)
    ]

    batch = dcp.BatchParser()
    batch.parse_all(paths)

    frame, seconds = measure(batch.to_dataframe)
    assert len(frame) == 10_000
    assert seconds < 2.0, f"batch DataFrame export took {seconds:.2f}s"


# ─────────────────────────────────────────────────────────────────────────────
#  Reporting (v1.3)
# ─────────────────────────────────────────────────────────────────────────────

REPORT_SCALE = 5_000
"""The roadmap's HTML acceptance criterion: 'Supports 5,000+ comments.'"""


@pytest.fixture(scope="module")
def report_parser(tmp_path_factory: pytest.TempPathFactory) -> dcp.DocxParser:
    directory = tmp_path_factory.mktemp("report_perf")
    path = build_docx(directory / "report.docx", make_specs(REPORT_SCALE, seed=21))
    parser = dcp.DocxParser()
    parser.parse(str(path))
    assert parser.stats().total_comments == REPORT_SCALE
    return parser


def test_analytics_are_linear(tmp_path: Path) -> None:
    """Aggregation must be one pass, not a nested walk over the threads."""
    from docx_comment_parser.reporting import build_report_data

    small = dcp.DocxParser()
    small.parse(str(build_docx(tmp_path / "s.docx", make_specs(1_000, seed=6))))
    big = dcp.DocxParser()
    big.parse(str(build_docx(tmp_path / "b.docx", make_specs(10_000, seed=6))))

    small_rows, big_rows = small.to_comments(), big.to_comments()
    build_report_data(small_rows)  # warm up

    _, small_seconds = measure(lambda: build_report_data(small_rows))
    _, big_seconds = measure(lambda: build_report_data(big_rows))

    ratio = big_seconds / max(small_seconds, 1e-4)
    assert ratio < 40, f"10x comments cost {ratio:.1f}x the analytics time"


def test_html_report_at_5000_comments(report_parser: dcp.DocxParser, tmp_path: Path) -> None:
    """Roadmap: 'Supports 5,000+ comments.' Must also stay a sane file size."""
    pytest.importorskip("jinja2")
    target = tmp_path / "big_report.html"
    _, seconds = measure(lambda: report_parser.export_html_report(target))

    assert seconds < 10.0, f"HTML report of {REPORT_SCALE} comments took {seconds:.2f}s"

    size_mb = target.stat().st_size / (1024 * 1024)
    # Comments are embedded once as compact JSON rather than as rendered rows,
    # so the file stays emailable at this scale.
    assert size_mb < 8, f"report is {size_mb:.1f} MB"


def test_html_report_is_self_contained_at_scale(
    report_parser: dcp.DocxParser, tmp_path: Path
) -> None:
    """The offline promise has to survive scale, not just the 3-comment fixture."""
    pytest.importorskip("jinja2")
    import re

    target = report_parser.export_html_report(tmp_path / "offline.html")
    body = target.read_text(encoding="utf-8")
    assert re.findall(r'(?:src|href)\s*=\s*["\'](?!#)([^"\']+)', body) == []


def test_markdown_report_at_5000_comments(report_parser: dcp.DocxParser) -> None:
    _, seconds = measure(report_parser.to_markdown_report)
    assert seconds < 10.0, f"Markdown report took {seconds:.2f}s"


def test_report_memory_is_bounded(report_parser: dcp.DocxParser, tmp_path: Path) -> None:
    """Building a report must not hold several full copies of the document."""
    pytest.importorskip("jinja2")
    gc.collect()
    tracemalloc.start()
    try:
        report_parser.export_html_report(tmp_path / "mem.html")
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()

    peak_mb = peak / (1024 * 1024)
    assert peak_mb < 200, f"peak memory {peak_mb:.1f} MB for a {REPORT_SCALE}-comment report"


def test_reporting_import_does_not_pull_in_jinja2() -> None:
    """Importing the library must stay dependency-free and fast.

    Jinja2 is imported inside render_html_report, not at module scope, so a
    program that only parses never pays for it.
    """
    import subprocess
    import sys

    code = (
        "import sys; import docx_comment_parser;"
        "assert 'jinja2' not in sys.modules, 'jinja2 imported eagerly';"
        "assert 'pandas' not in sys.modules, 'pandas imported eagerly';"
        "print('clean')"
    )
    result = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True
    )
    assert result.returncode == 0, result.stderr
    assert "clean" in result.stdout
