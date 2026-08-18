"""Tests for the v1.3 reporting layer: analytics, charts, HTML and Markdown.

The HTML assertions deliberately avoid parsing the document with an HTML
library.  What matters about this report is not its DOM shape but three
promises made to users: it opens offline, it cannot be broken by document
content, and it contains the same numbers the analytics computed.  Those are
what is asserted here.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import List

import pytest

import docx_comment_parser as dcp
from docx_comment_parser.models import Comment, build_comments
from docx_comment_parser.reporting import (
    build_report_data,
    export_markdown_report,
    render_markdown_report,
)
from docx_comment_parser.reporting import analytics, charts, report_builder

from conftest import CommentSpec, build_docx, make_specs

FIXED_TIME = datetime(2026, 5, 1, 12, 0, tzinfo=timezone.utc)


def comments_of(path: Path) -> List[Comment]:
    parser = dcp.DocxParser()
    parser.parse(str(path))
    return parser.to_comments()


@pytest.fixture(scope="module")
def basic_comments(basic_docx: Path) -> List[Comment]:
    return comments_of(basic_docx)


# ═════════════════════════════════════════════════════════════════════════════
#  Analytics
# ═════════════════════════════════════════════════════════════════════════════


def test_overview_counts_match_the_document(basic_comments: List[Comment]) -> None:
    data = build_report_data(basic_comments, generated_at=FIXED_TIME)
    overview = data.overview

    assert overview.total_comments == 3
    assert overview.root_comments == 1
    assert overview.replies == 2
    assert overview.resolved == 1
    assert overview.unresolved == 2
    assert overview.unique_authors == 2
    assert overview.threads == 1
    assert overview.title == "basic.docx"
    assert overview.generated_at == FIXED_TIME


def test_resolution_rate_is_a_fraction_and_a_percent() -> None:
    rows = build_comments([])
    empty = build_report_data(rows)
    # No comments must not divide by zero.
    assert empty.overview.resolution_rate == 0.0
    assert empty.overview.resolution_percent == 0.0
    assert empty.is_empty


def test_overview_date_range(basic_comments: List[Comment]) -> None:
    overview = build_report_data(basic_comments).overview
    assert overview.earliest is not None and overview.latest is not None
    assert overview.earliest.date().isoformat() == "2024-03-01"
    assert overview.latest.date().isoformat() == "2024-03-03"


def test_authors_are_ranked_by_volume(basic_comments: List[Comment]) -> None:
    data = build_report_data(basic_comments)
    names = [author.author for author in data.authors]
    assert names == ["Alice Tester", "Bob Reviewer"]

    alice = data.authors[0]
    assert alice.total == 2
    assert alice.resolved == 1
    assert alice.unresolved == 1
    assert alice.resolution_rate == pytest.approx(0.5)
    assert alice.initials == "AT"
    assert alice.first is not None and alice.last is not None
    assert alice.first < alice.last


def test_author_totals_sum_to_the_document_total() -> None:
    rows = build_comments(_parse(make_specs(300, seed=11)))
    data = build_report_data(rows)
    assert sum(a.total for a in data.authors) == data.overview.total_comments
    assert sum(a.resolved for a in data.authors) == data.overview.resolved


def test_threads_group_root_with_replies(basic_comments: List[Comment]) -> None:
    data = build_report_data(basic_comments)
    assert len(data.threads) == 1

    thread = data.threads[0]
    assert thread.size == 3
    assert thread.reply_count == 2
    assert thread.root.comment_id == 0
    # Root first, then replies in order.
    assert [c.comment_id for c in thread.comments] == [0, 1, 2]
    assert thread.participants == ("Alice Tester", "Bob Reviewer")


def test_thread_is_resolved_only_when_every_member_is(
    basic_comments: List[Comment],
) -> None:
    """One open reply keeps the whole conversation open — that is how a
    reviewer reads it, and the summary must agree."""
    thread = build_report_data(basic_comments).threads[0]
    assert any(c.resolved for c in thread.comments)
    assert not thread.resolved


def test_daily_buckets_fill_calendar_gaps() -> None:
    specs = [
        CommentSpec(id=0, date="2024-03-01T09:00:00Z"),
        # Nothing on the 2nd or 3rd: the chart must still show those days.
        CommentSpec(id=1, date="2024-03-04T09:00:00Z"),
    ]
    data = build_report_data(build_comments(_parse(specs)))
    labels = [bucket.label for bucket in data.daily]
    assert labels == ["2024-03-01", "2024-03-02", "2024-03-03", "2024-03-04"]
    assert [bucket.total for bucket in data.daily] == [1, 0, 0, 1]


def test_weekly_buckets_use_iso_weeks() -> None:
    specs = [
        CommentSpec(id=0, date="2024-03-04T09:00:00Z"),  # Monday, ISO week 10
        CommentSpec(id=1, date="2024-03-08T09:00:00Z"),  # Friday, same week
        CommentSpec(id=2, date="2024-03-12T09:00:00Z"),  # next week
    ]
    data = build_report_data(build_comments(_parse(specs)))
    assert [b.label for b in data.weekly] == ["2024-W10", "2024-W11"]
    assert [b.total for b in data.weekly] == [2, 1]


def test_timeline_totals_never_exceed_the_document_total() -> None:
    rows = build_comments(_parse(make_specs(400, seed=4)))
    data = build_report_data(rows)
    assert sum(b.total for b in data.daily) == data.overview.total_comments
    assert sum(b.total for b in data.weekly) == data.overview.total_comments
    for bucket in data.daily:
        assert bucket.resolved <= bucket.total
        assert bucket.unresolved == bucket.total - bucket.resolved


def test_undated_comments_are_skipped_by_the_timeline_but_still_counted() -> None:
    """A malformed timestamp must not remove a comment from the report."""
    specs = [
        CommentSpec(id=0, date="not a date at all"),
        CommentSpec(id=1, date="2024-03-01T09:00:00Z"),
    ]
    data = build_report_data(build_comments(_parse(specs)))

    assert data.overview.total_comments == 2
    assert sum(b.total for b in data.daily) == 1  # only the dated one is plottable
    assert len(data.threads) == 2                 # but both still appear


def test_report_data_survives_a_document_with_no_comments(empty_docx: Path) -> None:
    data = build_report_data(comments_of(empty_docx))
    assert data.is_empty
    assert data.overview.total_comments == 0
    assert data.authors == ()
    assert data.daily == () and data.weekly == ()
    assert data.threads == ()


# ═════════════════════════════════════════════════════════════════════════════
#  Charts
# ═════════════════════════════════════════════════════════════════════════════


def test_chart_renders_one_column_group_per_bucket() -> None:
    rows = build_comments(_parse(make_specs(60, seed=2)))
    data = build_report_data(rows)
    svg = charts.column_chart(data.daily, "c", "caption")

    assert svg.startswith("<svg")
    assert svg.count('class="viz-col"') == len(data.daily)
    # Both series are drawn, and neither is hard-coded to a hex value.
    assert "viz-series-1" in svg and "viz-series-2" in svg
    assert "#" not in svg.replace("&#39;", "")


def test_chart_is_accessible_and_self_describing() -> None:
    data = build_report_data(build_comments(_parse(make_specs(20, seed=3))))
    svg = charts.column_chart(data.daily, "chart-x", "Comments per day.")

    assert 'role="img"' in svg
    assert 'aria-labelledby="chart-x-desc"' in svg
    assert "<desc id=\"chart-x-desc\">Comments per day.</desc>" in svg


def test_chart_labels_only_the_tallest_column() -> None:
    """A number on every column is noise; exactly one direct label is drawn."""
    data = build_report_data(build_comments(_parse(make_specs(50, seed=9))))
    svg = charts.column_chart(data.daily, "c", "caption")
    assert svg.count('class="viz-peak"') == 1


def test_empty_chart_degrades_to_a_message() -> None:
    svg = charts.column_chart([], "c", "caption", empty_message="Nothing here.")
    assert "<svg" not in svg
    assert "Nothing here." in svg


def test_chart_escapes_bucket_labels() -> None:
    hostile = analytics.TimelineBucket(
        label='<script>"&', start=datetime(2024, 3, 1).date(), total=1, resolved=0
    )
    svg = charts.column_chart([hostile], "c", "caption")
    assert "<script>" not in svg
    assert "&lt;script&gt;" in svg


def test_chart_axis_uses_round_numbers() -> None:
    top, step = charts._axis_max(37)
    assert step in (1, 2, 5, 10, 20, 50, 100)
    assert top >= 37 and top % step == 0


def test_chart_thins_x_labels_so_they_cannot_collide() -> None:
    """With many columns only a readable subset of dates is labelled."""
    indices = charts._label_indices(365)
    assert len(indices) <= charts._MAX_X_LABELS
    # The span must stay readable: first and last are always labelled.
    assert indices[0] == 0 and indices[-1] == 364
    assert indices == sorted(set(indices))

    # Below the cap, every column keeps its label.
    assert charts._label_indices(5) == [0, 1, 2, 3, 4]
    assert charts._label_indices(0) == []


def test_chart_axis_handles_a_degenerate_range() -> None:
    assert charts._nice_step(0) == 1.0
    assert charts._nice_step(-5) == 1.0
    assert charts._axis_max(0) == (1.0, 1.0)


def test_bar_path_is_empty_for_a_zero_height_column() -> None:
    """A bucket with no comments must draw nothing, not a zero-height artefact."""
    assert charts._bar_path(0, 0, 10, 0, 4) == ""
    # A column shorter than the corner radius still produces valid geometry.
    assert charts._bar_path(0, 0, 10, 1, 4).startswith("M")


def test_very_long_reviews_drop_the_empty_days() -> None:
    """Gap-filling a multi-year review would emit thousands of empty columns.

    The cap is applied per series, so the two behave differently on the same
    data: four years is ~1,460 days (over the cap, so only real activity is
    emitted) but only ~210 weeks (under it, so the calendar is preserved).
    """
    specs = [
        CommentSpec(id=0, date="2020-01-01T09:00:00Z"),
        CommentSpec(id=1, date="2024-01-01T09:00:00Z"),
    ]
    data = build_report_data(build_comments(_parse(specs)))

    assert len(data.daily) == 2
    assert [bucket.total for bucket in data.daily] == [1, 1]

    assert 1 < len(data.weekly) <= analytics._MAX_FILLED_BUCKETS
    assert sum(bucket.total for bucket in data.weekly) == 2

    # Whichever branch was taken, no series may exceed the cap.
    assert len(data.daily) <= analytics._MAX_FILLED_BUCKETS


def test_chart_handles_a_single_bucket() -> None:
    data = build_report_data(build_comments(_parse([CommentSpec(id=0)])))
    svg = charts.column_chart(data.daily, "c", "caption")
    assert svg.count('class="viz-col"') == 1


# ═════════════════════════════════════════════════════════════════════════════
#  Markdown report
# ═════════════════════════════════════════════════════════════════════════════


def test_markdown_report_contains_the_headline_figures(
    basic_comments: List[Comment],
) -> None:
    text = render_markdown_report(basic_comments, generated_at=FIXED_TIME)

    assert text.startswith("# Comment review — basic.docx")
    assert "| Total comments | 3 |" in text
    assert "| Still open | 2 |" in text
    assert "Alice Tester" in text and "Bob Reviewer" in text
    assert "## Open items" in text
    assert "This sentence needs revision." in text
    assert str(dcp.__version__) in text


def test_markdown_report_needs_no_optional_dependency(
    basic_comments: List[Comment], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Markdown must work on a bare install, like CSV and JSON do."""
    import sys

    monkeypatch.setitem(sys.modules, "jinja2", None)
    monkeypatch.setitem(sys.modules, "pandas", None)
    assert "# Comment review" in render_markdown_report(basic_comments)


def test_markdown_escapes_table_breaking_characters() -> None:
    specs = [CommentSpec(id=0, author="A | B", text="pipe | and\nnewline")]
    text = render_markdown_report(build_comments(_parse(specs)))

    # Every reviewer row must still have exactly the declared number of columns.
    rows = [line for line in text.splitlines() if line.startswith("| A ")]
    assert rows, "author row missing"
    for row in rows:
        assert row.count("|") - row.count("\\|") == 6


def test_markdown_report_handles_an_empty_document(empty_docx: Path) -> None:
    text = render_markdown_report(comments_of(empty_docx))
    assert "No comments were found" in text
    # No crash and no dangling section headers.
    assert "## Reviewers" not in text


def test_markdown_threads_can_be_omitted(basic_comments: List[Comment]) -> None:
    with_threads = render_markdown_report(basic_comments)
    without = render_markdown_report(basic_comments, include_threads=False)

    assert "## All conversations" in with_threads
    assert "## All conversations" not in without
    assert "## Overview" in without  # the summary is still there
    assert len(without) < len(with_threads)


def test_markdown_marks_resolution_with_checkboxes(
    basic_comments: List[Comment],
) -> None:
    text = render_markdown_report(basic_comments)
    assert "- [x] **Alice Tester**" in text  # the resolved reply
    assert "- [ ] **Alice Tester**" in text  # the open root


def test_markdown_lists_the_source_documents_for_a_batch(batch_dir: Path) -> None:
    batch = dcp.BatchParser()
    batch.parse_all([str(p) for p in sorted(batch_dir.glob("*.docx"))])

    text = batch.to_markdown_report()
    assert "**Documents**" in text
    for path in batch.parsed_files():
        assert path in text


def test_export_markdown_creates_missing_directories(
    basic_comments: List[Comment], tmp_path: Path
) -> None:
    target = tmp_path / "deep" / "nested" / "review.md"
    written = export_markdown_report(basic_comments, target)

    assert written == target
    assert target.read_text(encoding="utf-8").startswith("# Comment review")


# ═════════════════════════════════════════════════════════════════════════════
#  HTML report
# ═════════════════════════════════════════════════════════════════════════════

jinja2 = pytest.importorskip("jinja2", reason="HTML reports need the [report] extra")


def test_html_report_is_a_complete_document(basic_comments: List[Comment]) -> None:
    html = report_builder.render_html_report(basic_comments, generated_at=FIXED_TIME)

    assert html.startswith("<!doctype html>")
    assert html.rstrip().endswith("</html>")
    assert "basic.docx" in html


def test_html_report_has_no_external_references(
    basic_comments: List[Comment],
) -> None:
    """The roadmap's core acceptance criterion: it must open without internet."""
    html = report_builder.render_html_report(basic_comments)

    references = re.findall(r'(?:src|href)\s*=\s*["\'](?!#)([^"\']+)', html)
    assert references == [], f"report reaches out to {references}"
    for scheme in ("http://", "https://", "//cdn", "cdnjs", "unpkg"):
        assert scheme not in html


def test_html_report_inlines_its_css_and_js(basic_comments: List[Comment]) -> None:
    html = report_builder.render_html_report(basic_comments)
    assert "--series-1" in html          # from report.css
    assert "report-data" in html         # from report.js
    assert "prefers-color-scheme" in html


def test_html_report_contains_every_section(basic_comments: List[Comment]) -> None:
    html = report_builder.render_html_report(basic_comments)
    for section in ("Overview", "Reviewers", "Timeline", "Comments"):
        assert f">{section}</h2>" in html


def test_html_report_embeds_all_comments(basic_comments: List[Comment]) -> None:
    html = report_builder.render_html_report(basic_comments)
    payload = _payload_of(html)

    assert len(payload["rows"]) == len(basic_comments)
    assert set(payload["authors"]) == {"Alice Tester", "Bob Reviewer"}
    assert payload["documents"]


def test_payload_interns_authors_and_documents() -> None:
    """The de-duplication is what keeps a large report emailable."""
    rows = build_comments(_parse(make_specs(500, seed=8)), "some/doc.docx")
    payload = report_builder.build_payload(rows)

    assert len(payload["rows"]) == 500
    assert len(payload["authors"]) == 3        # not 500 repeated strings
    assert payload["documents"] == ["some/doc.docx"]
    # Every row references authors by index, not by value.
    assert all(isinstance(row[1], int) for row in payload["rows"])


def test_document_content_cannot_escape_the_script_block() -> None:
    """A comment containing a closing script tag must not break the page."""
    hostile = "</script><script>window.PWNED=1</script><img src=x onerror=alert(1)>"
    specs = [CommentSpec(id=0, author="</script>", text=hostile, anchor=hostile)]
    html = report_builder.render_html_report(build_comments(_parse(specs)))

    # Exactly the two script blocks the template itself opens: the document's
    # own text created no new ones.
    assert html.count("<script") == 2
    assert html.count("</script>") == 2

    # The hostile text is present only in its escaped, inert form. Asserting on
    # the escaped spelling matters more than asserting the substring is absent:
    # "window.PWNED" legitimately appears as JSON *data*, and what makes it safe
    # is that the angle brackets around it can no longer form a tag.
    assert "\\u003c/script\\u003e" in html
    assert "<script>window.PWNED" not in html
    assert "<img src=x onerror=alert(1)>" not in html

    # ...and the text still survives intact once the JSON is decoded.
    assert _payload_of(html)["rows"][0][4] == hostile


def test_html_escapes_content_rendered_outside_the_payload() -> None:
    """Author names reach the filter dropdown through Jinja, not the payload."""
    specs = [CommentSpec(id=0, author='Bad<script>"Name', text="hi")]
    html = report_builder.render_html_report(build_comments(_parse(specs)))

    assert "Bad<script>" not in html
    assert "&lt;script&gt;" in html


def test_html_report_survives_unicode(unicode_docx: Path) -> None:
    html = report_builder.render_html_report(comments_of(unicode_docx))
    payload = _payload_of(html)

    assert "Νίκος Ηλιόπουλος" in payload["authors"]
    assert any("🚀" in row[4] for row in payload["rows"])
    assert 'charset="utf-8"' in html


def test_html_report_handles_an_empty_document(empty_docx: Path) -> None:
    html = report_builder.render_html_report(comments_of(empty_docx))

    assert "<!doctype html>" in html
    assert _payload_of(html)["rows"] == []
    assert "No comments to summarise." in html


def test_html_title_can_be_overridden(basic_comments: List[Comment]) -> None:
    html = report_builder.render_html_report(basic_comments, title="Q3 Review")
    assert "<h1>Q3 Review</h1>" in html


def test_html_report_is_reproducible(basic_comments: List[Comment]) -> None:
    """A fixed timestamp must give byte-identical output, so reports diff."""
    first = report_builder.render_html_report(basic_comments, generated_at=FIXED_TIME)
    second = report_builder.render_html_report(basic_comments, generated_at=FIXED_TIME)
    assert first == second


def test_chart_scale_defaults_to_weeks_for_long_reviews() -> None:
    """A year of daily columns is unreadable, so the weekly view leads."""
    long_specs = [
        CommentSpec(id=i, date=f"2024-{(i % 12) + 1:02d}-{(i % 28) + 1:02d}T09:00:00Z")
        for i in range(200)
    ]
    html = report_builder.render_html_report(build_comments(_parse(long_specs)))
    assert 'data-chart-scale="weekly" aria-pressed="true"' in html

    short = report_builder.render_html_report(
        build_comments(_parse(make_specs(50, seed=1)))
    )
    assert 'data-chart-scale="daily" aria-pressed="true"' in short


def test_custom_template_is_honoured(
    basic_comments: List[Comment], tmp_path: Path
) -> None:
    template = tmp_path / "tiny.html.j2"
    template.write_text(
        "<h1>{{ data.overview.total_comments }} comments</h1>", encoding="utf-8"
    )
    html = report_builder.render_html_report(basic_comments, template=template)
    assert html == "<h1>3 comments</h1>"


def test_missing_jinja2_explains_how_to_install_it(
    basic_comments: List[Comment], monkeypatch: pytest.MonkeyPatch
) -> None:
    import sys

    monkeypatch.setitem(sys.modules, "jinja2", None)
    with pytest.raises(ImportError, match=r"docx-comment-parser\[report\]"):
        report_builder.render_html_report(basic_comments)


def test_export_html_creates_missing_directories(
    basic_comments: List[Comment], tmp_path: Path
) -> None:
    target = tmp_path / "reports" / "2026" / "review.html"
    written = report_builder.export_html_report(basic_comments, target)

    assert written == target
    assert target.read_text(encoding="utf-8").startswith("<!doctype html>")


# ═════════════════════════════════════════════════════════════════════════════
#  Parser and BatchParser methods
# ═════════════════════════════════════════════════════════════════════════════


def test_parser_report_methods(basic_docx: Path, tmp_path: Path) -> None:
    parser = dcp.DocxParser()
    parser.parse(str(basic_docx))

    assert parser.to_markdown_report().startswith("# Comment review")
    assert parser.to_html_report().startswith("<!doctype html>")

    html = parser.export_html_report(tmp_path / "r.html")
    markdown = parser.export_markdown_report(tmp_path / "r.md")
    assert html.exists() and markdown.exists()


def test_batch_report_covers_every_parsed_file(batch_dir: Path, tmp_path: Path) -> None:
    batch = dcp.BatchParser(max_threads=2)
    batch.parse_all([str(p) for p in sorted(batch_dir.glob("*.docx"))])

    html = batch.export_html_report(tmp_path / "batch.html")
    payload = _payload_of(html.read_text(encoding="utf-8"))

    # Three good files; the corrupt one is skipped rather than aborting.
    assert len(payload["documents"]) == 3
    assert len(payload["rows"]) == sum(
        len(batch.comments(path)) for path in batch.parsed_files()
    )


def test_batch_report_can_be_restricted_to_some_files(batch_dir: Path) -> None:
    batch = dcp.BatchParser()
    batch.parse_all([str(p) for p in sorted(batch_dir.glob("*.docx"))])

    only = batch.parsed_files()[:1]
    payload = _payload_of(batch.to_html_report(file_paths=only))
    assert len(payload["documents"]) == 1


def test_reports_reflect_filtered_comments(basic_docx: Path) -> None:
    """Reports take a comment list, so any filter applies before rendering."""
    from docx_comment_parser.filters import filter_comments

    parser = dcp.DocxParser()
    parser.parse(str(basic_docx))
    unresolved = filter_comments(parser.to_comments(), resolved=False)

    text = render_markdown_report(unresolved)
    assert "| Total comments | 2 |" in text


def test_reporting_is_reachable_from_the_top_level_package() -> None:
    """``dcp.reporting`` resolves lazily but behaves like a normal attribute."""
    assert dcp.reporting.__name__ == "docx_comment_parser.reporting"
    assert dcp.build_report_data is analytics.build_report_data
    assert "reporting" in dir(dcp)

    with pytest.raises(AttributeError, match="no attribute 'not_a_thing'"):
        dcp.not_a_thing  # type: ignore[attr-defined]


def test_importing_the_package_does_not_load_the_reporting_layer() -> None:
    """Parsing-only programs must not pay for the report aggregates.

    Run in a subprocess because this test module has already imported the
    reporting package, so the in-process sys.modules cannot answer the question.
    """
    import subprocess
    import sys

    code = (
        "import sys, docx_comment_parser;"
        "assert 'docx_comment_parser.reporting' not in sys.modules, 'imported eagerly';"
        "print('lazy')"
    )
    result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert "lazy" in result.stdout


# ═════════════════════════════════════════════════════════════════════════════
#  Helpers
# ═════════════════════════════════════════════════════════════════════════════


def _parse(specs) -> List:
    """Build a .docx from specs in a temp dir and return its CommentMetadata."""
    import tempfile

    directory = Path(tempfile.mkdtemp())
    path = build_docx(directory / "gen.docx", specs)
    parser = dcp.DocxParser()
    parser.parse(str(path))
    return parser.comments()


def _payload_of(html: str) -> dict:
    """Extract and decode the JSON the report embeds for its JavaScript."""
    match = re.search(
        r'<script type="application/json" id="report-data">(.*?)</script>',
        html,
        re.DOTALL,
    )
    assert match, "report-data block missing"
    return json.loads(match.group(1))
