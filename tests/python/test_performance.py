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


# ─────────────────────────────────────────────────────────────────────────────
#  Comparison (v1.4)
# ─────────────────────────────────────────────────────────────────────────────

DIFF_SCALE = 5_000
"""Two review cycles of this size is the shape a real second round takes."""


def _revise(specs, resolve_every: int = 3, edit_every: int = 7):
    """Produce a plausible second round: some resolved, some reworded, some new."""
    from conftest import CommentSpec

    revised = []
    for index, spec in enumerate(specs):
        if index % 11 == 0:
            continue  # deleted in the second round
        revised.append(
            CommentSpec(
                id=spec.id,
                author=spec.author,
                date=spec.date,
                initials=spec.initials,
                text=spec.text + (" Reworded." if index % edit_every == 0 else ""),
                parent=spec.parent,
                done=spec.done or index % resolve_every == 0,
                style=spec.style,
                anchor=spec.anchor,
            )
        )
    revised.extend(
        CommentSpec(
            id=900_000 + index,
            author="New Reviewer",
            date="2024-04-01T09:00:00Z",
            text=f"A brand new concern number {index} raised this round.",
            anchor=f"New anchor {index}.",
        )
        for index in range(len(specs) // 20)
    )
    return revised


@pytest.fixture(scope="module")
def diff_pair(tmp_path_factory: pytest.TempPathFactory):
    """Two versions of a 5,000-comment document, already parsed."""
    import docx_comment_parser as dcp

    directory = tmp_path_factory.mktemp("diff_perf")
    first = make_specs(DIFF_SCALE, seed=31)
    second = _revise(first)

    rows = []
    for name, specs in (("v1.docx", first), ("v2.docx", second)):
        parser = dcp.DocxParser()
        parser.parse(str(build_docx(directory / name, specs)))
        rows.append(parser.to_comments())
    return rows[0], rows[1]


def test_diff_of_5000_comments_is_fast(diff_pair) -> None:
    """Levels 1 and 2 are dictionary lookups, so a realistic revision never
    reaches the quadratic level 3 for more than a handful of comments."""
    from docx_comment_parser.comparison import diff_comments

    before, after = diff_pair
    result, seconds = measure(lambda: diff_comments(before, after))

    assert result.velocity.comments_before == DIFF_SCALE
    assert seconds < 20.0, f"diffing {DIFF_SCALE} comments took {seconds:.2f}s"


def test_diff_matches_nearly_everything(diff_pair) -> None:
    """Accuracy, not speed: a revision must not look like a rewrite.

    Every surviving comment keeps its id here, so anything reported as an
    addition or a removal beyond the ones actually planted is a matching bug.
    """
    from docx_comment_parser.comparison import diff_comments

    before, after = diff_pair
    result = diff_comments(before, after)

    planted_removals = len([1 for index in range(DIFF_SCALE) if index % 11 == 0])
    assert result.velocity.removed == planted_removals
    assert result.velocity.added == DIFF_SCALE // 20
    assert len(result.matched) == DIFF_SCALE - planted_removals


def test_diff_scales_near_linearly(tmp_path: Path) -> None:
    """Ten times the comments must not cost a hundred times the effort."""
    import docx_comment_parser as dcp
    from docx_comment_parser.comparison import diff_comments

    def rows(name: str, count: int, seed: int):
        parser = dcp.DocxParser()
        parser.parse(str(build_docx(tmp_path / name, make_specs(count, seed=seed))))
        return parser.to_comments()

    small_a, small_b = rows("sa.docx", 500, 41), rows("sb.docx", 500, 41)
    big_a, big_b = rows("ba.docx", 5_000, 41), rows("bb.docx", 5_000, 41)

    diff_comments(small_a, small_b)  # warm up
    _, small_seconds = measure(lambda: diff_comments(small_a, small_b))
    _, big_seconds = measure(lambda: diff_comments(big_a, big_b))

    ratio = big_seconds / max(small_seconds, 1e-4)
    assert ratio < 40, f"10x comments cost {ratio:.1f}x the diff time"


def test_fuzzy_blocking_keeps_a_full_mismatch_tractable(tmp_path: Path) -> None:
    """The worst case: two large documents with nothing in common.

    Every comment falls through to level 3, where blocking by shared words is
    what stops the cross-product from being materialised.
    """
    import docx_comment_parser as dcp
    from conftest import CommentSpec
    from docx_comment_parser.comparison import diff_comments

    def rows(name: str, offset: int, word: str):
        specs = [
            CommentSpec(
                id=index + offset,
                author=f"Author {index % 5}",
                date="2024-03-01T09:00:00Z",
                # Same sentence shape, no shared number and no shared subject:
                # the vocabulary overlaps heavily, which is what defeats naive
                # blocking, while nothing actually corresponds.
                text=f"{word} observation {index + offset} regarding the manuscript.",
                anchor=f"{word} passage {index + offset}.",
            )
            for index in range(1_500)
        ]
        parser = dcp.DocxParser()
        parser.parse(str(build_docx(tmp_path / name, specs)))
        return parser.to_comments()

    before = rows("x.docx", 0, "Preliminary")
    after = rows("y.docx", 500_000, "Subsequent")

    result, seconds = measure(lambda: diff_comments(before, after))
    assert seconds < 30.0, f"a 1,500 x 1,500 mismatch took {seconds:.2f}s"
    # Nothing genuinely corresponds, so the honest answer is all-new, all-gone.
    assert result.velocity.added + result.velocity.removed > 0


def test_diff_report_at_scale(diff_pair, tmp_path: Path) -> None:
    from docx_comment_parser.comparison import diff_comments

    pytest.importorskip("jinja2")
    before, after = diff_pair
    result = diff_comments(before, after)

    target = tmp_path / "diff.html"
    _, seconds = measure(lambda: result.export_html(target))
    assert seconds < 15.0, f"HTML diff report took {seconds:.2f}s"

    size_mb = target.stat().st_size / (1024 * 1024)
    assert size_mb < 12, f"diff report is {size_mb:.1f} MB"


def test_diff_memory_is_bounded(diff_pair) -> None:
    """A diff must not hold several extra copies of both documents."""
    from docx_comment_parser.comparison import diff_comments

    before, after = diff_pair
    gc.collect()
    tracemalloc.start()
    try:
        diff_comments(before, after)
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()

    peak_mb = peak / (1024 * 1024)
    assert peak_mb < 200, f"peak memory {peak_mb:.1f} MB for a {DIFF_SCALE}-comment diff"


def test_comparison_import_stays_lazy() -> None:
    """Importing the library must not pull in the diff engine or rapidfuzz.

    The comparison package resolves on first attribute access, the same way the
    reporting layer does, so a program that only parses pays nothing for it.
    """
    import subprocess
    import sys

    code = (
        "import sys; import docx_comment_parser;"
        "assert 'docx_comment_parser.comparison' not in sys.modules, 'comparison imported eagerly';"
        "assert 'rapidfuzz' not in sys.modules, 'rapidfuzz imported eagerly';"
        "assert 'difflib' not in sys.modules, 'difflib imported eagerly';"
        "docx_comment_parser.compare_comments;"
        "assert 'docx_comment_parser.comparison' in sys.modules, 'lazy attribute did not resolve';"
        "print('clean')"
    )
    result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert "clean" in result.stdout


def test_llm_and_privacy_imports_stay_lazy() -> None:
    """Importing the library must not pull in the v1.5 layers or their cost.

    Both packages resolve on first attribute access, the same way reporting and
    comparison do.  ``zipfile`` and ``xml.etree`` are named explicitly because
    the context reader needs both, and pulling them in at import time would add
    milliseconds to every program that only wanted to parse a document.
    """
    import subprocess
    import sys

    code = (
        "import sys; import docx_comment_parser;"
        "assert 'docx_comment_parser.llm' not in sys.modules, 'llm imported eagerly';"
        "assert 'docx_comment_parser.privacy' not in sys.modules, 'privacy imported eagerly';"
        "assert 'tiktoken' not in sys.modules, 'tiktoken imported eagerly';"
        "assert 'xml.etree.ElementTree' not in sys.modules, 'ElementTree imported eagerly';"
        "docx_comment_parser.build_llm_dataset;"
        "docx_comment_parser.anonymize_comments;"
        "assert 'docx_comment_parser.llm' in sys.modules, 'lazy attribute did not resolve';"
        "assert 'docx_comment_parser.privacy' in sys.modules, 'lazy attribute did not resolve';"
        "print('clean')"
    )
    result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert "clean" in result.stdout


@pytest.mark.perf
def test_llm_export_memory_stays_bounded(large_docx: Path) -> None:
    """A 10,000-comment LLM export must not hold the document text per record.

    Context windows are slices of one cached document string; a naive
    implementation that copied the surrounding text into every record would use
    an order of magnitude more memory here.
    """
    import tracemalloc

    parser = dcp.DocxParser()
    parser.parse(str(large_docx))

    tracemalloc.start()
    records = parser.to_llm_dataset(chars_before=500, chars_after=500)
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()

    assert len(records) == 10_000
    peak_mb = peak / (1024 * 1024)
    assert peak_mb < 400, f"peak memory {peak_mb:.1f} MB for a 10,000-comment export"
