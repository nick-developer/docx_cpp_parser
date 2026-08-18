"""Tests for the docx-comments command-line interface.

Driven through Typer's CliRunner, which exercises the real argument parsing,
the real Rich rendering and the real exit codes.
"""

from __future__ import annotations

import csv
import io
import json
from contextlib import contextmanager
from pathlib import Path

import pytest

typer = pytest.importorskip("typer")
from typer.testing import CliRunner  # noqa: E402

from docx_comment_parser._cli_app import app  # noqa: E402
from conftest import CommentSpec, build_docx  # noqa: E402

# A wide terminal keeps Rich from wrapping table cells mid-word, which would
# make substring assertions flaky.
runner = CliRunner(env={"COLUMNS": "250", "TERM": "dumb", "NO_COLOR": "1"})


def invoke(*args: str):
    return runner.invoke(app, list(args))


# ─────────────────────────────────────────────────────────────────────────────
#  Help and version
# ─────────────────────────────────────────────────────────────────────────────


def test_help_lists_every_command() -> None:
    result = invoke("--help")
    assert result.exit_code == 0
    for command in ("parse", "stats", "export", "unresolved", "batch", "report", "diff"):
        assert command in result.output


@pytest.mark.parametrize(
    "command", ["parse", "stats", "export", "unresolved", "batch", "report", "diff"]
)
def test_every_command_has_help(command: str) -> None:
    """Roadmap acceptance criterion: useful help for every command."""
    result = invoke(command, "--help")
    assert result.exit_code == 0
    assert len(result.output.strip()) > 50


def test_version() -> None:
    import docx_comment_parser

    result = invoke("--version")
    assert result.exit_code == 0
    # Assert against the package's own version rather than a literal, so a
    # release bump does not have to be remembered in two places.
    assert docx_comment_parser.__version__ in result.output


def test_no_args_shows_help() -> None:
    result = invoke()
    assert "Usage" in result.output


# ─────────────────────────────────────────────────────────────────────────────
#  parse
# ─────────────────────────────────────────────────────────────────────────────


def test_parse_lists_comments(basic_docx: Path) -> None:
    result = invoke("parse", str(basic_docx))
    assert result.exit_code == 0
    assert "Alice Tester" in result.output
    assert "Bob Reviewer" in result.output
    assert "3 comment(s)" in result.output


def test_parse_author_filter(basic_docx: Path) -> None:
    result = invoke("parse", str(basic_docx), "--author", "bob")
    assert result.exit_code == 0
    assert "Bob Reviewer" in result.output
    assert "1 comment(s)" in result.output


def test_parse_contains_filter(basic_docx: Path) -> None:
    result = invoke("parse", str(basic_docx), "--contains", "revision")
    assert result.exit_code == 0
    assert "1 comment(s)" in result.output


def test_parse_resolved_and_unresolved(basic_docx: Path) -> None:
    resolved = invoke("parse", str(basic_docx), "--resolved")
    assert "1 comment(s)" in resolved.output

    unresolved = invoke("parse", str(basic_docx), "--unresolved")
    assert "2 comment(s)" in unresolved.output


def test_parse_rejects_contradictory_flags(basic_docx: Path) -> None:
    result = invoke("parse", str(basic_docx), "--resolved", "--unresolved")
    assert result.exit_code != 0


def test_parse_threads_only(basic_docx: Path) -> None:
    result = invoke("parse", str(basic_docx), "--threads-only")
    assert result.exit_code == 0
    assert "3 comment(s)" in result.output  # all three form one thread


def test_parse_limit(basic_docx: Path) -> None:
    result = invoke("parse", str(basic_docx), "--limit", "1")
    assert result.exit_code == 0
    assert "Showing first 1 of 3" in result.output


def test_parse_no_matches(basic_docx: Path) -> None:
    result = invoke("parse", str(basic_docx), "--author", "Nobody")
    assert result.exit_code == 0
    assert "No comments matched" in result.output


def test_parse_missing_file() -> None:
    result = invoke("parse", "no_such_file.docx")
    assert result.exit_code != 0


def test_parse_corrupt_file(tmp_path: Path) -> None:
    corrupt = tmp_path / "corrupt.docx"
    corrupt.write_bytes(b"not a zip")
    result = invoke("parse", str(corrupt))
    assert result.exit_code == 1


def test_parse_empty_document(empty_docx: Path) -> None:
    result = invoke("parse", str(empty_docx))
    assert result.exit_code == 0
    assert "No comments matched" in result.output


# ─────────────────────────────────────────────────────────────────────────────
#  stats
# ─────────────────────────────────────────────────────────────────────────────


def test_stats(basic_docx: Path) -> None:
    result = invoke("stats", str(basic_docx))
    assert result.exit_code == 0
    assert "Total comments" in result.output
    assert "Alice Tester" in result.output
    assert "By author" in result.output


def test_stats_on_empty_document(empty_docx: Path) -> None:
    result = invoke("stats", str(empty_docx))
    assert result.exit_code == 0
    assert "Total comments" in result.output


# ─────────────────────────────────────────────────────────────────────────────
#  unresolved
# ─────────────────────────────────────────────────────────────────────────────


def test_unresolved_exits_nonzero_when_work_remains(basic_docx: Path) -> None:
    """Documented CI behaviour: non-zero exit means the review is not finished."""
    result = invoke("unresolved", str(basic_docx))
    assert result.exit_code == 1
    assert "2 unresolved" in result.output


def test_unresolved_exits_zero_when_all_done(tmp_path: Path) -> None:
    specs = [CommentSpec(id=0, text="Done.", done=True)]
    path = build_docx(tmp_path / "done.docx", specs)

    result = invoke("unresolved", str(path))
    assert result.exit_code == 0
    assert "No unresolved comments" in result.output


# ─────────────────────────────────────────────────────────────────────────────
#  export
# ─────────────────────────────────────────────────────────────────────────────


def test_export_json_to_stdout(basic_docx: Path) -> None:
    result = invoke("export", str(basic_docx), "--json")
    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert len(payload) == 3
    assert payload[0]["author"] == "Alice Tester"


def test_export_csv_to_stdout(basic_docx: Path) -> None:
    result = invoke("export", str(basic_docx), "--csv")
    assert result.exit_code == 0
    rows = list(csv.DictReader(io.StringIO(result.output)))
    assert len(rows) == 3


def test_export_json_to_file(basic_docx: Path, tmp_path: Path) -> None:
    target = tmp_path / "out.json"
    result = invoke("export", str(basic_docx), "--json", "-o", str(target))
    assert result.exit_code == 0
    assert len(json.loads(target.read_text(encoding="utf-8"))) == 3


def test_export_infers_format_from_extension(basic_docx: Path, tmp_path: Path) -> None:
    target = tmp_path / "out.csv"
    result = invoke("export", str(basic_docx), "-o", str(target))
    assert result.exit_code == 0
    with open(target, newline="", encoding="utf-8") as handle:
        assert len(list(csv.DictReader(handle))) == 3


def test_export_defaults_to_json(basic_docx: Path) -> None:
    result = invoke("export", str(basic_docx))
    assert result.exit_code == 0
    assert json.loads(result.output)


def test_export_rejects_both_formats(basic_docx: Path) -> None:
    result = invoke("export", str(basic_docx), "--json", "--csv")
    assert result.exit_code != 0


def test_export_applies_filters(basic_docx: Path) -> None:
    result = invoke("export", str(basic_docx), "--json", "--author", "alice")
    payload = json.loads(result.output)
    assert len(payload) == 2
    assert {row["author"] for row in payload} == {"Alice Tester"}


def test_export_unresolved_only(basic_docx: Path) -> None:
    result = invoke("export", str(basic_docx), "--json", "--unresolved")
    payload = json.loads(result.output)
    assert len(payload) == 2
    assert all(row["resolved"] is False for row in payload)


def test_export_stdout_json_is_pipeable(basic_docx: Path) -> None:
    """Nothing but JSON may reach stdout, or piping into jq breaks."""
    result = invoke("export", str(basic_docx), "--json")
    assert result.output.lstrip().startswith("[")
    assert result.output.rstrip().endswith("]")


def test_export_unicode_to_file(unicode_docx: Path, tmp_path: Path) -> None:
    target = tmp_path / "u.json"
    result = invoke("export", str(unicode_docx), "-o", str(target))
    assert result.exit_code == 0
    payload = json.loads(target.read_text(encoding="utf-8"))
    assert payload[0]["author"] == "Νίκος Ηλιόπουλος"


# ─────────────────────────────────────────────────────────────────────────────
#  batch
# ─────────────────────────────────────────────────────────────────────────────


def test_batch_summarises_directory(batch_dir: Path) -> None:
    result = invoke("batch", str(batch_dir))
    # Exit code 1 because the directory contains a deliberately corrupt file.
    assert result.exit_code == 1
    assert "doc0.docx" in result.output
    assert "3 file(s) parsed" in result.output


def test_batch_reports_corrupt_files(batch_dir: Path) -> None:
    result = invoke("batch", str(batch_dir))
    assert "corrupt.docx" in result.output


def test_batch_exports_combined_file(batch_dir: Path, tmp_path: Path) -> None:
    target = tmp_path / "all.csv"
    result = invoke("batch", str(batch_dir), "-o", str(target))
    assert target.exists()

    with open(target, newline="", encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))
    assert len({row["document_name"] for row in rows}) == 3


def test_batch_export_json(batch_dir: Path, tmp_path: Path) -> None:
    target = tmp_path / "all.json"
    invoke("batch", str(batch_dir), "-o", str(target))
    assert len(json.loads(target.read_text(encoding="utf-8"))) > 0


def test_batch_on_clean_directory_exits_zero(tmp_path: Path) -> None:
    build_docx(tmp_path / "a.docx", [CommentSpec(id=0)])
    build_docx(tmp_path / "b.docx", [CommentSpec(id=0)])

    result = invoke("batch", str(tmp_path))
    assert result.exit_code == 0
    assert "2 file(s) parsed" in result.output


def test_batch_empty_directory(tmp_path: Path) -> None:
    result = invoke("batch", str(tmp_path))
    assert result.exit_code == 1


def test_batch_ignores_word_lock_files(tmp_path: Path) -> None:
    """Word leaves ~$name.docx owner files behind; they are not documents."""
    build_docx(tmp_path / "real.docx", [CommentSpec(id=0)])
    (tmp_path / "~$real.docx").write_bytes(b"lock file junk")

    result = invoke("batch", str(tmp_path))
    assert result.exit_code == 0
    assert "1 file(s) parsed" in result.output


def test_batch_recursive(tmp_path: Path) -> None:
    build_docx(tmp_path / "top.docx", [CommentSpec(id=0)])
    build_docx(tmp_path / "sub" / "nested.docx", [CommentSpec(id=0)])

    shallow = invoke("batch", str(tmp_path))
    assert "1 file(s) parsed" in shallow.output

    deep = invoke("batch", str(tmp_path), "--recursive")
    assert "2 file(s) parsed" in deep.output


def test_batch_thread_count_option(batch_dir: Path) -> None:
    result = invoke("batch", str(batch_dir), "--threads", "2")
    assert "3 file(s) parsed" in result.output


# ─────────────────────────────────────────────────────────────────────────────
#  Console encoding robustness
# ─────────────────────────────────────────────────────────────────────────────
# A Windows console defaults to a legacy code page (cp1252) that cannot encode
# ✓, ○ or ↳. Rendering them there raised UnicodeEncodeError and killed the
# command. CliRunner writes into a UTF-8 buffer, so these paths need testing
# directly rather than through invoke().


def test_glyph_constants_are_defined() -> None:
    from docx_comment_parser import _cli_app

    for name in (
        "GLYPH_RESOLVED", "GLYPH_OPEN", "GLYPH_REPLY", "GLYPH_ELLIPSIS",
        "GLYPH_EMPTY", "GLYPH_DASH", "GLYPH_OK", "GLYPH_FAIL",
    ):
        assert getattr(_cli_app, name)


def test_encoding_support_detection() -> None:
    from docx_comment_parser._cli_app import _encoding_supports

    class Stream:
        def __init__(self, encoding: str) -> None:
            self.encoding = encoding

    import docx_comment_parser._cli_app as mod

    original = (mod.sys.stdout, mod.sys.stderr)
    try:
        mod.sys.stdout = Stream("cp1252")  # type: ignore[assignment]
        mod.sys.stderr = Stream("cp1252")  # type: ignore[assignment]
        assert _encoding_supports("plain ascii") is True
        assert _encoding_supports("✓○↳") is False

        mod.sys.stdout = Stream("utf-8")  # type: ignore[assignment]
        mod.sys.stderr = Stream("utf-8")  # type: ignore[assignment]
        assert _encoding_supports("✓○↳") is True
    finally:
        mod.sys.stdout, mod.sys.stderr = original


class _FakeStream:
    """Just enough of a stream for the module's encoding probe."""

    def __init__(self, encoding: str) -> None:
        self.encoding = encoding


@contextmanager
def ascii_glyph_mode():
    """Reload the CLI module as it loads on a console that cannot do Unicode.

    The module picks its glyphs at import time from ``sys.stdout.encoding``, so
    the encoding is what has to be faked. Patching ``_encoding_supports``
    directly would not work: reloading re-executes the ``def`` and restores the
    real implementation before the glyph constants are computed.
    """
    import importlib
    import sys as _sys

    import docx_comment_parser._cli_app as mod

    saved = (_sys.stdout, _sys.stderr)
    try:
        _sys.stdout = _FakeStream("cp1252")  # type: ignore[assignment]
        _sys.stderr = _FakeStream("cp1252")  # type: ignore[assignment]
        reloaded = importlib.reload(mod)
        assert not reloaded._UNICODE_OK, "fixture failed to force ASCII mode"
        _sys.stdout, _sys.stderr = saved
        yield reloaded
    finally:
        _sys.stdout, _sys.stderr = saved
        importlib.reload(mod)


def test_ascii_glyphs_contain_no_rich_markup() -> None:
    """Square brackets in a fallback would be eaten as a Rich style tag.

    "[x]" renders as nothing at all, silently blanking the status column, so no
    ASCII fallback may contain brackets.
    """
    with ascii_glyph_mode() as mod:
        fallbacks = [
            mod.GLYPH_RESOLVED, mod.GLYPH_OPEN, mod.GLYPH_REPLY,
            mod.GLYPH_ELLIPSIS, mod.GLYPH_EMPTY, mod.GLYPH_DASH,
            mod.GLYPH_OK, mod.GLYPH_FAIL,
        ]
        for glyph in fallbacks:
            assert glyph.isascii(), f"non-ASCII fallback {glyph!r}"
            assert "[" not in glyph and "]" not in glyph, f"markup in {glyph!r}"


def test_table_renders_on_a_legacy_codepage(basic_docx: Path) -> None:
    """The original crash: rendering the table to a strict cp1252 stream.

    On such a console the module picks ASCII glyphs, so every character it
    emits is encodable and nothing raises.
    """
    from rich.console import Console

    with ascii_glyph_mode() as mod:
        comments = mod._load(basic_docx)

        buffer = io.BytesIO()
        stream = io.TextIOWrapper(buffer, encoding="cp1252", errors="strict")
        console = Console(file=stream, width=200, legacy_windows=False)

        console.print(mod._comment_table(comments, f"Comments {mod.GLYPH_DASH} test"))
        console.print(mod._summary_line(comments, len(comments)))
        stream.flush()

    text = buffer.getvalue().decode("cp1252")
    assert "Alice Tester" in text
    assert "3 comment(s)" in text


def test_status_column_is_visible_with_ascii_glyphs(basic_docx: Path) -> None:
    """Both statuses must actually appear, not be swallowed by markup parsing."""
    from rich.console import Console

    with ascii_glyph_mode() as mod:
        comments = mod._load(basic_docx)
        buffer = io.StringIO()
        console = Console(file=buffer, width=200, legacy_windows=False)
        console.print(mod._comment_table(comments, "t"))
        text = buffer.getvalue()

        assert mod.GLYPH_RESOLVED in text  # the one resolved comment
        assert mod.GLYPH_OPEN in text      # the two open ones


def test_unencodable_comment_text_does_not_crash(unicode_docx: Path) -> None:
    """Greek/emoji in a comment must degrade, not abort the command.

    ASCII glyphs cannot help here — the unencodable characters come from the
    document itself — so the entry point puts the streams into errors="replace".
    """
    import docx_comment_parser._cli_app as mod
    from rich.console import Console

    with ascii_glyph_mode() as ascii_mod:
        comments = ascii_mod._load(unicode_docx)

        buffer = io.BytesIO()
        stream = io.TextIOWrapper(buffer, encoding="cp1252", errors="replace")
        console = Console(file=stream, width=200, legacy_windows=False)
        console.print(ascii_mod._comment_table(comments, "t"))
        stream.flush()

    assert len(buffer.getvalue()) > 0
    assert mod is not None


def test_make_output_lossy_is_safe_to_call() -> None:
    from docx_comment_parser._cli_app import _make_output_lossy

    _make_output_lossy()  # must never raise, whatever the streams are


# ─────────────────────────────────────────────────────────────────────────────
#  Entry point
# ─────────────────────────────────────────────────────────────────────────────


def test_entry_point_is_importable() -> None:
    from docx_comment_parser.cli import main

    assert callable(main)


def test_entry_point_reports_missing_dependencies(monkeypatch: pytest.MonkeyPatch) -> None:
    """Without the [cli] extra the command must explain itself, not traceback."""
    import docx_comment_parser.cli as cli_module

    def fake_import(name: str, *args: object, **kwargs: object):
        raise ImportError("No module named 'typer'", name="typer")

    monkeypatch.setattr(cli_module, "__import__", fake_import, raising=False)
    monkeypatch.setitem(__import__("sys").modules, "docx_comment_parser._cli_app", None)

    # Force the lazy import to fail the way a missing extra would.
    import builtins

    real_import = builtins.__import__

    def guard(name: str, *args: object, **kwargs: object):
        if name.endswith("_cli_app"):
            raise ImportError("No module named 'typer'", name="typer")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", guard)

    with pytest.raises(SystemExit) as excinfo:
        cli_module.main()
    assert excinfo.value.code == 1


# ─────────────────────────────────────────────────────────────────────────────
#  report  (new in v1.3)
# ─────────────────────────────────────────────────────────────────────────────


@pytest.fixture()
def report_docx(tmp_path: Path) -> Path:
    specs = [
        CommentSpec(id=0, author="Alice", text="Check the security model.",
                    anchor="The system authenticates users."),
        CommentSpec(id=1, author="Bob", text="Agreed, will revise.", parent=0, done=True),
        CommentSpec(id=2, author="Carol", text="Typo in the heading."),
    ]
    return build_docx(tmp_path / "spec.docx", specs)


def test_report_writes_html_by_default(report_docx: Path, tmp_path: Path) -> None:
    pytest.importorskip("jinja2")
    target = tmp_path / "review.html"
    result = invoke("report", str(report_docx), "-o", str(target))

    assert result.exit_code == 0, result.output
    assert target.exists()
    body = target.read_text(encoding="utf-8")
    assert body.startswith("<!doctype html>")
    assert "Check the security model." in body


def test_report_default_filename_is_derived_from_the_document(
    report_docx: Path,
) -> None:
    pytest.importorskip("jinja2")
    result = invoke("report", str(report_docx))

    assert result.exit_code == 0, result.output
    assert (report_docx.parent / "spec_report.html").exists()


def test_report_infers_markdown_from_the_extension(
    report_docx: Path, tmp_path: Path
) -> None:
    target = tmp_path / "review.md"
    result = invoke("report", str(report_docx), "-o", str(target))

    assert result.exit_code == 0, result.output
    assert target.read_text(encoding="utf-8").startswith("# Comment review")


def test_report_markdown_flag_needs_no_optional_dependency(
    report_docx: Path, tmp_path: Path
) -> None:
    target = tmp_path / "out.txt"
    result = invoke("report", str(report_docx), "--markdown", "-o", str(target))

    assert result.exit_code == 0, result.output
    assert "# Comment review" in target.read_text(encoding="utf-8")


def test_report_rejects_both_formats_at_once(report_docx: Path) -> None:
    result = invoke("report", str(report_docx), "--html", "--markdown")
    assert result.exit_code == 2


def test_report_applies_filters(report_docx: Path, tmp_path: Path) -> None:
    target = tmp_path / "alice.md"
    result = invoke("report", str(report_docx), "--author", "alice", "-o", str(target))

    assert result.exit_code == 0, result.output
    body = target.read_text(encoding="utf-8")
    assert "| Total comments | 1 |" in body
    assert "Typo in the heading." not in body


def test_report_title_can_be_overridden(report_docx: Path, tmp_path: Path) -> None:
    target = tmp_path / "t.md"
    invoke("report", str(report_docx), "--title", "Sprint 4 review", "-o", str(target))
    assert target.read_text(encoding="utf-8").startswith(
        "# Comment review — Sprint 4 review"
    )


def test_report_on_an_unreadable_file_exits_one(tmp_path: Path) -> None:
    broken = tmp_path / "broken.docx"
    broken.write_bytes(b"not a zip")
    result = invoke("report", str(broken), "-o", str(tmp_path / "x.md"))
    assert result.exit_code == 1


def test_batch_can_write_a_combined_html_report(tmp_path: Path) -> None:
    pytest.importorskip("jinja2")
    folder = tmp_path / "docs"
    folder.mkdir()
    for index in range(2):
        build_docx(folder / f"d{index}.docx", [CommentSpec(id=0, text=f"Note {index}.")])

    target = tmp_path / "all.html"
    result = invoke("batch", str(folder), "-o", str(target))

    assert result.exit_code == 0, result.output
    body = target.read_text(encoding="utf-8")
    assert "Note 0." in body and "Note 1." in body


# ─────────────────────────────────────────────────────────────────────────────
#  diff (v1.4)
# ─────────────────────────────────────────────────────────────────────────────


@pytest.fixture()
def cycle_docs(tmp_path: Path) -> tuple[Path, Path]:
    """Two review rounds: one resolved, one edited, one removed, one added."""
    before = build_docx(tmp_path / "v1.docx", [
        CommentSpec(id=0, author="Alice", date="2026-03-01T09:00:00Z",
                    text="Needs a clearer opening.", anchor="The methodology."),
        CommentSpec(id=1, author="Bob", date="2026-03-02T09:00:00Z",
                    text="Agreed.", parent=0),
        CommentSpec(id=2, author="Carol", date="2026-03-03T09:00:00Z",
                    text="Legal sign-off needed.", anchor="Clause 4.2."),
    ])
    after = build_docx(tmp_path / "v2.docx", [
        CommentSpec(id=0, author="Alice", date="2026-03-01T09:00:00Z",
                    text="Needs a clearer opening.", anchor="The methodology.",
                    done=True),
        CommentSpec(id=1, author="Bob", date="2026-03-02T09:00:00Z",
                    text="Agreed, rewritten now.", parent=0),
        CommentSpec(id=3, author="Dan", date="2026-03-20T09:00:00Z",
                    text="New security concern.", anchor="Annexe B."),
    ])
    return before, after


def test_diff_reports_every_bucket(cycle_docs: tuple[Path, Path]) -> None:
    before, after = cycle_docs
    result = invoke("diff", str(before), str(after))

    assert result.exit_code == 0, result.output
    for label in ("New comments", "Removed", "Newly resolved", "Re-opened", "Edited"):
        assert label in result.output


def test_diff_shows_the_changes(cycle_docs: tuple[Path, Path]) -> None:
    before, after = cycle_docs
    result = invoke("diff", str(before), str(after))

    assert "New security concern." in result.output
    assert "Legal sign-off needed." in result.output
    assert "Agreed, rewritten now." in result.output


def test_diff_lists_risk_hotspots(cycle_docs: tuple[Path, Path]) -> None:
    before, after = cycle_docs
    result = invoke("diff", str(before), str(after))
    assert "Risk hotspots" in result.output


def test_diff_writes_html(cycle_docs: tuple[Path, Path], tmp_path: Path) -> None:
    pytest.importorskip("jinja2")
    before, after = cycle_docs
    target = tmp_path / "diff.html"
    result = invoke("diff", str(before), str(after), "-o", str(target))

    assert result.exit_code == 0, result.output
    assert "<!doctype html>" in target.read_text(encoding="utf-8")


def test_diff_writes_markdown_without_any_extra(
    cycle_docs: tuple[Path, Path], tmp_path: Path
) -> None:
    before, after = cycle_docs
    target = tmp_path / "diff.md"
    result = invoke("diff", str(before), str(after), "-o", str(target))

    assert result.exit_code == 0, result.output
    assert target.read_text(encoding="utf-8").startswith("# Comment diff")


def test_diff_writes_csv(cycle_docs: tuple[Path, Path], tmp_path: Path) -> None:
    before, after = cycle_docs
    target = tmp_path / "diff.csv"
    invoke("diff", str(before), str(after), "-o", str(target))

    with open(target, newline="", encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))
    assert {row["change"] for row in rows} >= {"added", "removed", "resolved"}


def test_diff_writes_json(cycle_docs: tuple[Path, Path], tmp_path: Path) -> None:
    before, after = cycle_docs
    target = tmp_path / "diff.json"
    invoke("diff", str(before), str(after), "-o", str(target))

    payload = json.loads(target.read_text(encoding="utf-8"))
    assert payload["velocity"]["added"] == 1
    assert payload["velocity"]["removed"] == 1


def test_diff_names_the_documents_the_user_typed(
    cycle_docs: tuple[Path, Path], tmp_path: Path
) -> None:
    before, after = cycle_docs
    target = tmp_path / "diff.md"
    invoke("diff", str(before), str(after), "-o", str(target))
    assert "v1.docx → v2.docx" in target.read_text(encoding="utf-8")


def test_diff_threshold_is_passed_through(
    cycle_docs: tuple[Path, Path], tmp_path: Path
) -> None:
    before, after = cycle_docs
    target = tmp_path / "diff.json"
    invoke("diff", str(before), str(after), "--threshold", "0.5", "-o", str(target))
    assert json.loads(target.read_text(encoding="utf-8"))["threshold"] == 0.5


def test_diff_rejects_an_out_of_range_threshold(cycle_docs: tuple[Path, Path]) -> None:
    before, after = cycle_docs
    assert invoke("diff", str(before), str(after), "--threshold", "1.5").exit_code == 2


def test_diff_no_fuzzy_option(cycle_docs: tuple[Path, Path]) -> None:
    before, after = cycle_docs
    assert invoke("diff", str(before), str(after), "--no-fuzzy").exit_code == 0


def test_diff_show_limits_the_listing(cycle_docs: tuple[Path, Path]) -> None:
    before, after = cycle_docs
    result = invoke("diff", str(before), str(after), "--show", "1")
    assert "use --output for all of them" in result.output


def test_diff_show_zero_prints_no_change_table(cycle_docs: tuple[Path, Path]) -> None:
    before, after = cycle_docs
    result = invoke("diff", str(before), str(after), "--show", "0")
    # The summary panel and the hotspot table stay; only the per-change listing
    # is suppressed.
    assert "New comments" in result.output
    assert "Changes" not in result.output


def test_diff_of_identical_documents_says_nothing_changed(basic_docx: Path) -> None:
    result = invoke("diff", str(basic_docx), str(basic_docx))
    assert result.exit_code == 0
    assert "Nothing changed" in result.output


def test_diff_fail_on_open_gates_a_build(cycle_docs: tuple[Path, Path]) -> None:
    before, after = cycle_docs
    assert invoke("diff", str(before), str(after), "--fail-on-open").exit_code == 1


def test_diff_fail_on_open_passes_when_everything_is_resolved(tmp_path: Path) -> None:
    specs = [CommentSpec(id=0, text="All done.", done=True)]
    both = build_docx(tmp_path / "done.docx", specs)
    assert invoke("diff", str(both), str(both), "--fail-on-open").exit_code == 0


def test_diff_on_an_unreadable_file_exits_one(basic_docx: Path, tmp_path: Path) -> None:
    broken = tmp_path / "broken.docx"
    broken.write_bytes(b"not a zip")
    assert invoke("diff", str(basic_docx), str(broken)).exit_code == 1


def test_diff_title_can_be_overridden(
    cycle_docs: tuple[Path, Path], tmp_path: Path
) -> None:
    before, after = cycle_docs
    target = tmp_path / "diff.md"
    invoke("diff", str(before), str(after), "--title", "Round two", "-o", str(target))
    assert target.read_text(encoding="utf-8").startswith("# Comment diff — Round two")
