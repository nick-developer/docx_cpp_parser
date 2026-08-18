"""The ``docx-comments`` Typer application.

This module imports Typer and Rich unconditionally.  It is loaded only from
:func:`docx_comment_parser.cli.main`, which catches the ImportError and prints
an install hint — so the core package keeps its zero-dependency install while
the console script still exists and explains itself.
"""

from __future__ import annotations

import sys
from dataclasses import replace
from pathlib import Path
from typing import Any, List, Optional, Sequence

import typer
from rich.console import Console
from rich.panel import Panel
from rich.progress import BarColumn, Progress, SpinnerColumn, TextColumn
from rich.table import Table

from . import __version__
from ._core import BatchParser, DocxParser, DocxParserError
from .comparison import DEFAULT_THRESHOLD, ChangeKind, compare_comments
from .exporters import csv_exporter, json_exporter
from .filters import filter_comments
from .models import Comment, build_comments
from .reporting import markdown_report, report_builder

app = typer.Typer(
    name="docx-comments",
    help=(
        "Extract, inspect and export comments from Microsoft Word .docx files.\n\n"
        "Every command accepts filters such as --author, --contains and "
        "--unresolved. Run 'docx-comments COMMAND --help' for details."
    ),
    no_args_is_help=True,
    add_completion=True,
)

console = Console()
err_console = Console(stderr=True)


# ─────────────────────────────────────────────────────────────────────────────
#  Console character set
# ─────────────────────────────────────────────────────────────────────────────
# A Windows console still defaults to a legacy code page such as cp1252, which
# cannot encode ✓, ○ or ↳.  Writing them raises UnicodeEncodeError and takes the
# whole command down.  Pick the glyph set from what the output stream can
# actually represent, and fall back to ASCII when in doubt.


def _encoding_supports(sample: str) -> bool:
    """True when both output streams can encode every character in ``sample``."""
    for stream in (sys.stdout, sys.stderr):
        encoding = getattr(stream, "encoding", None)
        if not encoding:
            continue
        try:
            sample.encode(encoding)
        except (UnicodeEncodeError, LookupError):
            return False
    return True


_UNICODE_OK = _encoding_supports("✓○↳…—✗→")

# NB: ASCII fallbacks must not contain square brackets. Rich would parse "[x]"
# as a style tag and render nothing at all, silently blanking the column.
GLYPH_RESOLVED = "✓" if _UNICODE_OK else "done"
GLYPH_OPEN = "○" if _UNICODE_OK else "open"
GLYPH_REPLY = "↳ " if _UNICODE_OK else "> "
GLYPH_ELLIPSIS = "…" if _UNICODE_OK else "..."
GLYPH_EMPTY = "—" if _UNICODE_OK else "-"
GLYPH_DASH = "—" if _UNICODE_OK else "-"
GLYPH_OK = "✓" if _UNICODE_OK else "OK"
GLYPH_FAIL = "✗" if _UNICODE_OK else "X"
GLYPH_ARROW = "→" if _UNICODE_OK else "->"


# ─────────────────────────────────────────────────────────────────────────────
#  Reusable options
# ─────────────────────────────────────────────────────────────────────────────
# Typer OptionInfo objects are safe to share between commands, which keeps the
# filter flags identical everywhere instead of drifting per command.

AUTHOR_OPT = typer.Option(
    None, "--author", "-a",
    help="Keep only comments whose author contains this text (case-insensitive).",
)
CONTAINS_OPT = typer.Option(
    None, "--contains", "-c",
    help="Keep only comments whose text or anchored text contains this string.",
)
RESOLVED_OPT = typer.Option(
    False, "--resolved",
    help="Keep only comments marked resolved.",
)
UNRESOLVED_OPT = typer.Option(
    False, "--unresolved",
    help="Keep only comments not yet resolved.",
)
THREADS_OPT = typer.Option(
    False, "--threads-only",
    help="Keep only comments that are part of a multi-comment thread.",
)
LIMIT_OPT = typer.Option(
    None, "--limit", "-n",
    help="Show at most this many comments.",
)


def _resolved_flag(resolved: bool, unresolved: bool) -> Optional[bool]:
    """Collapse the two mutually exclusive flags into a tri-state."""
    if resolved and unresolved:
        raise typer.BadParameter(
            "--resolved and --unresolved are mutually exclusive."
        )
    if resolved:
        return True
    if unresolved:
        return False
    return None


def _load(path: Path) -> List[Comment]:
    """Parse one .docx and return its comments, exiting cleanly on failure."""
    parser = DocxParser()
    try:
        parser.parse(str(path))
    except DocxParserError as exc:
        err_console.print(f"[bold red]Error[/] parsing {path}: {exc}")
        raise typer.Exit(code=1)
    return build_comments(parser.comments(), parser.stats().file_path)


def _truncate(text: str, width: int) -> str:
    """Collapse whitespace and clip to ``width`` so table rows stay one line."""
    flat = " ".join(text.split())
    if len(flat) <= width:
        return flat
    return flat[: width - 1] + GLYPH_ELLIPSIS


def _when(comment: Comment) -> str:
    """Human-friendly timestamp, falling back to the raw XML value."""
    if comment.date_parsed is not None:
        return comment.date_parsed.strftime("%Y-%m-%d %H:%M")
    return comment.date or GLYPH_EMPTY


def _comment_table(comments: Sequence[Comment], title: str) -> Table:
    """Build the standard comment listing table."""
    table = Table(title=title, header_style="bold cyan", expand=True)
    table.add_column("ID", justify="right", style="dim", no_wrap=True)
    table.add_column("Author", style="green", no_wrap=True)
    table.add_column("Date", style="dim", no_wrap=True)
    table.add_column("St", justify="center", no_wrap=True)
    table.add_column("Comment", overflow="fold")
    table.add_column("Anchored to", style="dim", overflow="fold")

    for comment in comments:
        status = (
            f"[green]{GLYPH_RESOLVED}[/]" if comment.resolved
            else f"[yellow]{GLYPH_OPEN}[/]"
        )
        # Indent replies so thread structure is visible at a glance.
        indent = "  " * comment.thread_depth
        prefix = GLYPH_REPLY if comment.is_reply else ""
        table.add_row(
            str(comment.comment_id),
            comment.author or GLYPH_EMPTY,
            _when(comment),
            status,
            f"{indent}{prefix}{_truncate(comment.text, 90)}",
            _truncate(comment.referenced_text, 40),
        )
    return table


def _summary_line(comments: Sequence[Comment], total: int) -> str:
    """One-line footer describing what was shown versus what exists."""
    resolved = sum(1 for c in comments if c.resolved)
    shown = len(comments)
    suffix = "" if shown == total else f"  (filtered from {total})"
    return (
        f"[bold]{shown}[/] comment(s)  "
        f"[green]{resolved} resolved[/]  "
        f"[yellow]{shown - resolved} open[/]{suffix}"
    )


# ─────────────────────────────────────────────────────────────────────────────
#  Commands
# ─────────────────────────────────────────────────────────────────────────────

@app.command()
def parse(
    file: Path = typer.Argument(
        ..., exists=True, dir_okay=False, readable=True,
        help="Path to the .docx file.",
    ),
    author: Optional[str] = AUTHOR_OPT,
    contains: Optional[str] = CONTAINS_OPT,
    resolved: bool = RESOLVED_OPT,
    unresolved: bool = UNRESOLVED_OPT,
    threads_only: bool = THREADS_OPT,
    limit: Optional[int] = LIMIT_OPT,
) -> None:
    """List the comments in a document as a table."""
    all_comments = _load(file)
    selected = filter_comments(
        all_comments,
        author=author,
        contains=contains,
        resolved=_resolved_flag(resolved, unresolved),
        threads_only=threads_only,
    )
    shown = selected[:limit] if limit else selected

    if not shown:
        console.print("[yellow]No comments matched.[/]")
        return

    console.print(_comment_table(shown, f"Comments {GLYPH_DASH} {file.name}"))
    console.print(_summary_line(selected, len(all_comments)))
    if limit and len(selected) > limit:
        console.print(f"[dim]Showing first {limit} of {len(selected)}.[/]")


@app.command()
def stats(
    file: Path = typer.Argument(
        ..., exists=True, dir_okay=False, readable=True,
        help="Path to the .docx file.",
    ),
) -> None:
    """Show summary statistics and a per-author breakdown."""
    parser = DocxParser()
    try:
        parser.parse(str(file))
    except DocxParserError as exc:
        err_console.print(f"[bold red]Error[/] parsing {file}: {exc}")
        raise typer.Exit(code=1)

    document_stats = parser.stats()
    comments = build_comments(parser.comments(), document_stats.file_path)

    overview = Table.grid(padding=(0, 2))
    overview.add_column(style="bold")
    overview.add_column()
    overview.add_row("Total comments", str(document_stats.total_comments))
    overview.add_row("Root comments", str(document_stats.total_root_comments))
    overview.add_row("Replies", str(document_stats.total_replies))
    overview.add_row("Resolved", str(document_stats.total_resolved))
    overview.add_row(
        "Unresolved",
        str(document_stats.total_comments - document_stats.total_resolved),
    )
    overview.add_row("Unique authors", str(len(document_stats.unique_authors)))
    overview.add_row("Earliest comment", document_stats.earliest_date or GLYPH_EMPTY)
    overview.add_row("Latest comment", document_stats.latest_date or GLYPH_EMPTY)
    console.print(Panel(overview, title=f"[bold]{file.name}[/]", expand=False))

    if not comments:
        return

    by_author: dict[str, List[Comment]] = {}
    for comment in comments:
        by_author.setdefault(comment.author or GLYPH_EMPTY, []).append(comment)

    table = Table(title="By author", header_style="bold cyan")
    table.add_column("Author", style="green")
    table.add_column("Comments", justify="right")
    table.add_column("Resolved", justify="right")
    table.add_column("Open", justify="right")
    table.add_column("Resolution rate", justify="right")

    for name, group in sorted(by_author.items(), key=lambda kv: -len(kv[1])):
        done = sum(1 for c in group if c.resolved)
        rate = f"{done / len(group) * 100:.0f}%"
        table.add_row(name, str(len(group)), str(done), str(len(group) - done), rate)

    console.print(table)


@app.command()
def unresolved(
    file: Path = typer.Argument(
        ..., exists=True, dir_okay=False, readable=True,
        help="Path to the .docx file.",
    ),
    author: Optional[str] = AUTHOR_OPT,
    contains: Optional[str] = CONTAINS_OPT,
    limit: Optional[int] = LIMIT_OPT,
) -> None:
    """List only the comments that are still open.

    Exits with status 1 when open comments exist, so it can gate a CI job:
    ``docx-comments unresolved spec.docx || echo "review not finished"``.
    """
    all_comments = _load(file)
    selected = filter_comments(
        all_comments, author=author, contains=contains, resolved=False
    )

    if not selected:
        console.print(f"[bold green]{GLYPH_OK} No unresolved comments in {file.name}.[/]")
        return

    shown = selected[:limit] if limit else selected
    console.print(_comment_table(shown, f"Unresolved {GLYPH_DASH} {file.name}"))
    console.print(
        f"[bold yellow]{len(selected)}[/] unresolved of "
        f"{len(all_comments)} total."
    )
    raise typer.Exit(code=1)


@app.command()
def export(
    file: Path = typer.Argument(
        ..., exists=True, dir_okay=False, readable=True,
        help="Path to the .docx file.",
    ),
    as_json: bool = typer.Option(False, "--json", help="Export as JSON."),
    as_csv: bool = typer.Option(False, "--csv", help="Export as CSV."),
    output: Optional[Path] = typer.Option(
        None, "--output", "-o",
        help="Write to this file. Defaults to standard output.",
    ),
    author: Optional[str] = AUTHOR_OPT,
    contains: Optional[str] = CONTAINS_OPT,
    resolved: bool = RESOLVED_OPT,
    unresolved_only: bool = UNRESOLVED_OPT,
    threads_only: bool = THREADS_OPT,
    encoding: str = typer.Option(
        "utf-8", "--encoding",
        help="Output encoding. Use utf-8-sig for Excel on Windows.",
    ),
    delimiter: str = typer.Option(
        ",", "--delimiter", help="CSV field separator."
    ),
    indent: int = typer.Option(2, "--indent", help="JSON indent; 0 for compact."),
) -> None:
    """Export comments to JSON or CSV.

    Writes to standard output unless --output is given, so it pipes:
    ``docx-comments export spec.docx --json | jq '.[].author'``
    """
    if as_json and as_csv:
        raise typer.BadParameter("Choose either --json or --csv, not both.")
    if not as_json and not as_csv:
        # Infer from the output extension when the user gave one, otherwise
        # default to JSON.
        if output is not None and output.suffix.lower() == ".csv":
            as_csv = True
        else:
            as_json = True

    selected = filter_comments(
        _load(file),
        author=author,
        contains=contains,
        resolved=_resolved_flag(resolved, unresolved_only),
        threads_only=threads_only,
    )

    if as_json:
        if output is None:
            # print() rather than console.print(): Rich would re-wrap and
            # highlight the payload, which breaks piping into jq.
            print(json_exporter.to_json(selected, indent=indent))
        else:
            json_exporter.export_json(selected, output, indent=indent)
    else:
        if output is None:
            writer = csv_exporter.to_csv_rows(selected)
            import csv as _csv

            out = _csv.writer(sys.stdout, delimiter=delimiter, lineterminator="\n")
            out.writerows(writer)
        else:
            csv_exporter.export_csv(
                selected, output, encoding=encoding, delimiter=delimiter
            )

    if output is not None:
        err_console.print(
            f"[green]{GLYPH_OK}[/] Wrote {len(selected)} comment(s) to [bold]{output}[/]"
        )


@app.command()
def report(
    file: Path = typer.Argument(
        ..., exists=True, dir_okay=False, readable=True,
        help="Path to the .docx file.",
    ),
    output: Optional[Path] = typer.Option(
        None, "--output", "-o",
        help="Where to write the report. Defaults to <name>_report.html.",
    ),
    as_html: bool = typer.Option(False, "--html", help="Write an HTML report."),
    as_markdown: bool = typer.Option(
        False, "--markdown", "--md", help="Write a Markdown report."
    ),
    title: Optional[str] = typer.Option(
        None, "--title", help="Heading for the report. Defaults to the file name."
    ),
    template: Optional[Path] = typer.Option(
        None, "--template", exists=True, dir_okay=False,
        help="Custom Jinja2 template for the HTML report.",
    ),
    author: Optional[str] = AUTHOR_OPT,
    contains: Optional[str] = CONTAINS_OPT,
    resolved: bool = RESOLVED_OPT,
    unresolved_only: bool = UNRESOLVED_OPT,
    threads_only: bool = THREADS_OPT,
) -> None:
    """Build a shareable review report.

    The HTML report is a single self-contained file — charts, search and a
    thread explorer, no internet needed — so it can simply be emailed:

        docx-comments report spec.docx -o review.html
    """
    if as_html and as_markdown:
        raise typer.BadParameter("Choose either --html or --markdown, not both.")

    # Work out the format first, so the default file name matches it.
    if not as_html and not as_markdown:
        suffix = output.suffix.lower() if output is not None else ""
        as_markdown = suffix in {".md", ".markdown"}
        as_html = not as_markdown
    if output is None:
        output = file.with_name(f"{file.stem}_report{'.md' if as_markdown else '.html'}")

    selected = filter_comments(
        _load(file),
        author=author,
        contains=contains,
        resolved=_resolved_flag(resolved, unresolved_only),
        threads_only=threads_only,
    )

    if as_markdown:
        markdown_report.export_markdown_report(selected, output, title=title)
    else:
        try:
            report_builder.export_html_report(
                selected, output, title=title, template=template
            )
        except ImportError as exc:
            # Jinja2 missing: say how to fix it rather than dumping a traceback.
            err_console.print(f"[bold red]Error[/] {exc}")
            raise typer.Exit(code=1)

    console.print(
        f"[green]{GLYPH_OK}[/] Wrote a report of {len(selected)} comment(s) "
        f"to [bold]{output}[/]"
    )


@app.command()
def diff(
    before: Path = typer.Argument(
        ..., exists=True, dir_okay=False, readable=True,
        help="The earlier .docx — the review you already read.",
    ),
    after: Path = typer.Argument(
        ..., exists=True, dir_okay=False, readable=True,
        help="The later .docx — the one you want to know about.",
    ),
    output: Optional[Path] = typer.Option(
        None, "--output", "-o",
        help="Write the full comparison to this .html, .md, .csv or .json file.",
    ),
    threshold: float = typer.Option(
        DEFAULT_THRESHOLD, "--threshold", "-t", min=0.0, max=1.0,
        help="Similarity a fuzzy match must reach, 0.0-1.0.",
    ),
    no_fuzzy: bool = typer.Option(
        False, "--no-fuzzy",
        help="Match on ids and anchors only, never on text similarity.",
    ),
    title: Optional[str] = typer.Option(
        None, "--title", help="Heading for the report. Defaults to both file names."
    ),
    template: Optional[Path] = typer.Option(
        None, "--template", exists=True, dir_okay=False,
        help="Custom Jinja2 template for the HTML report.",
    ),
    show: int = typer.Option(
        10, "--show", "-n", help="Changes listed in the terminal. 0 for none.",
    ),
    fail_on_open: bool = typer.Option(
        False, "--fail-on-open",
        help="Exit 1 if any comment is still unresolved, for use as a CI gate.",
    ),
) -> None:
    """Compare two review cycles and report what changed.

    Answers the question a second round of review actually raises — what is new,
    what got closed, and what came back open:

        docx-comments diff spec_v1.docx spec_v2.docx -o changed.html
    """
    result = compare_comments(
        _load(before),
        _load(after),
        threshold=threshold,
        fuzzy=not no_fuzzy,
    )
    # _load hands over Comment rows, so the result names itself after the rows'
    # document_name.  Restore the names the user actually typed.
    result = replace(result, before_name=before.name, after_name=after.name)
    velocity = result.velocity

    table = Table.grid(padding=(0, 2))
    table.add_column(style="bold")
    table.add_column(justify="right")
    table.add_column(style="dim")
    table.add_row("New comments", str(velocity.added), "")
    table.add_row("Removed", str(velocity.removed), "")
    table.add_row("Newly resolved", str(velocity.resolved), "")
    table.add_row("Re-opened", str(velocity.reopened), "")
    table.add_row("Edited", str(velocity.edited), "")
    table.add_row("Unchanged", str(velocity.unchanged), "")
    table.add_row("", "", "")
    table.add_row(
        "Comments",
        f"{velocity.comments_before} {GLYPH_ARROW} {velocity.comments_after}",
        _delta(velocity.net_change),
    )
    table.add_row(
        "Still open",
        f"{velocity.open_before} {GLYPH_ARROW} {velocity.open_after}",
        _delta(velocity.open_after - velocity.open_before),
    )
    table.add_row(
        "Resolution rate",
        f"{velocity.resolution_rate_before * 100:.0f}% {GLYPH_ARROW} "
        f"{velocity.resolution_rate_after * 100:.0f}%",
        f"{velocity.resolution_rate_delta * 100:+.0f} pts",
    )
    console.print(
        Panel(
            table,
            title=f"[bold]{before.name} {GLYPH_ARROW} {after.name}[/]",
            subtitle=(
                "[green]converging[/]" if velocity.is_converging
                else "[yellow]not converging yet[/]"
            ),
            expand=False,
        )
    )

    if show:
        interesting = [
            change
            for change in result.changes
            if not change.has(ChangeKind.UNCHANGED)
        ]
        if interesting:
            console.print(_change_table(interesting[:show]))
            if len(interesting) > show:
                console.print(
                    f"[dim]Showing {show} of {len(interesting)} changes; "
                    f"use --output for all of them.[/]"
                )
        else:
            console.print("[green]Nothing changed between these two versions.[/]")

    if result.hotspots:
        spots = Table(title="Risk hotspots", header_style="bold cyan")
        spots.add_column("Thread", justify="right", style="dim", no_wrap=True)
        spots.add_column("Reviewer", style="green", no_wrap=True)
        spots.add_column("Open", justify="right")
        spots.add_column("Comment", overflow="fold")
        spots.add_column("Why", style="dim", overflow="fold")
        for spot in result.hotspots[:5]:
            spots.add_row(
                f"#{spot.root_id}",
                spot.author or GLYPH_EMPTY,
                str(spot.open_comments),
                _truncate(spot.text, 70),
                ", ".join(spot.reasons) or "still open",
            )
        console.print(spots)

    if output is not None:
        suffix = output.suffix.lower()
        if suffix == ".csv":
            result.export_csv(output)
        elif suffix in {".md", ".markdown"}:
            result.export_markdown(output, title=title)
        elif suffix == ".json":
            result.export_json(output)
        else:
            try:
                result.export_html(output, title=title, template=template)
            except ImportError as exc:
                # Jinja2 missing: say how to fix it rather than dumping a traceback.
                err_console.print(f"[bold red]Error[/] {exc}")
                raise typer.Exit(code=1)
        console.print(f"[green]{GLYPH_OK}[/] Wrote the comparison to [bold]{output}[/]")

    if fail_on_open and velocity.open_after:
        err_console.print(
            f"[yellow]{velocity.open_after} comment(s) still unresolved "
            f"in {after.name}.[/]"
        )
        raise typer.Exit(code=1)


def _delta(value: int) -> str:
    """A signed change, coloured by whether it is good news."""
    if value == 0:
        return ""
    return f"[{'yellow' if value > 0 else 'green'}]{value:+d}[/]"


def _change_table(changes: Sequence[Any]) -> Table:
    """The per-change listing shown by ``diff``."""
    table = Table(title="Changes", header_style="bold cyan", expand=True)
    table.add_column("Change", no_wrap=True)
    table.add_column("ID", justify="right", style="dim", no_wrap=True)
    table.add_column("Author", style="green", no_wrap=True)
    table.add_column("Comment", overflow="fold")
    table.add_column("Was", style="dim", overflow="fold")

    for change in changes:
        kinds = ", ".join(kind.value for kind in change.kinds)
        was = (
            _truncate(change.before.text, 50)
            if change.has(ChangeKind.EDITED) and change.before is not None
            else ""
        )
        table.add_row(
            f"[bold]{kinds}[/]",
            str(change.comment_id),
            change.author or GLYPH_EMPTY,
            _truncate(change.comment.text, 80),
            was,
        )
    return table


@app.command()
def batch(
    directory: Path = typer.Argument(
        ..., exists=True, file_okay=False, readable=True,
        help="Directory containing .docx files.",
    ),
    recursive: bool = typer.Option(
        False, "--recursive", "-r", help="Search subdirectories too."
    ),
    threads: int = typer.Option(
        0, "--threads", "-j",
        help="Worker threads. 0 uses every CPU core.",
    ),
    output: Optional[Path] = typer.Option(
        None, "--output", "-o",
        help="Combine every file's comments into this .json, .csv, .html or .md file.",
    ),
    author: Optional[str] = AUTHOR_OPT,
    contains: Optional[str] = CONTAINS_OPT,
    resolved: bool = RESOLVED_OPT,
    unresolved_only: bool = UNRESOLVED_OPT,
    threads_only: bool = THREADS_OPT,
) -> None:
    """Parse every .docx in a directory in parallel and summarise the results."""
    pattern = "**/*.docx" if recursive else "*.docx"
    # Word writes lock files as ~$name.docx; they are not real documents.
    paths = sorted(
        p for p in directory.glob(pattern)
        if p.is_file() and not p.name.startswith("~$")
    )

    if not paths:
        err_console.print(f"[yellow]No .docx files found in {directory}.[/]")
        raise typer.Exit(code=1)

    parser = BatchParser(max_threads=threads)
    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        TextColumn("{task.completed}/{task.total}"),
        console=err_console,
        transient=True,
    ) as progress:
        # parse_all releases the GIL and blocks until every file is done, so the
        # bar shows the batch as a single indeterminate step rather than
        # pretending to track per-file progress it cannot observe.
        task = progress.add_task(f"Parsing {len(paths)} file(s)", total=len(paths))
        parser.parse_all([str(p) for p in paths])
        progress.update(task, completed=len(paths))

    errors = parser.errors()
    table = Table(title=f"Batch {GLYPH_DASH} {directory}", header_style="bold cyan")
    table.add_column("File", style="green", overflow="fold")
    table.add_column("Comments", justify="right")
    table.add_column("Resolved", justify="right")
    table.add_column("Open", justify="right")
    table.add_column("Authors", justify="right")

    total_comments = 0
    for path in paths:
        key = str(path)
        if key in errors:
            table.add_row(
                path.name, "[red]error[/]",
                GLYPH_EMPTY, GLYPH_EMPTY, GLYPH_EMPTY,
            )
            continue
        file_stats = parser.stats(key)
        open_count = file_stats.total_comments - file_stats.total_resolved
        total_comments += file_stats.total_comments
        table.add_row(
            path.name,
            str(file_stats.total_comments),
            str(file_stats.total_resolved),
            str(open_count),
            str(len(file_stats.unique_authors)),
        )

    console.print(table)
    console.print(
        f"[bold]{len(paths) - len(errors)}[/] file(s) parsed, "
        f"[bold]{total_comments}[/] comment(s) total."
    )

    for failed_path, message in sorted(errors.items()):
        err_console.print(f"[red]{GLYPH_FAIL}[/] {failed_path}: {message}")

    if output is not None:
        rows = filter_comments(
            parser.to_comments(),
            author=author,
            contains=contains,
            resolved=_resolved_flag(resolved, unresolved_only),
            threads_only=threads_only,
        )
        suffix = output.suffix.lower()
        if suffix == ".csv":
            csv_exporter.export_csv(rows, output)
        elif suffix in {".html", ".htm"}:
            try:
                report_builder.export_html_report(rows, output)
            except ImportError as exc:
                err_console.print(f"[bold red]Error[/] {exc}")
                raise typer.Exit(code=1)
        elif suffix in {".md", ".markdown"}:
            markdown_report.export_markdown_report(rows, output)
        else:
            json_exporter.export_json(rows, output)
        console.print(
            f"[green]{GLYPH_OK}[/] Wrote {len(rows)} comment(s) to [bold]{output}[/]"
        )

    if errors:
        raise typer.Exit(code=1)


def _version_callback(value: bool) -> None:
    if value:
        console.print(f"docx-comments {__version__}")
        raise typer.Exit()


@app.callback()
def _main(
    version: bool = typer.Option(
        False, "--version", "-V",
        callback=_version_callback, is_eager=True,
        help="Show the version and exit.",
    ),
) -> None:
    """Extract, inspect and export comments from Word .docx files."""


def _make_output_lossy() -> None:
    """Stop an unencodable character in a comment from killing the command.

    The GLYPH_* constants keep *our* decorations printable, but the comment text
    itself belongs to the document: a Greek author name or an emoji in a comment
    is still unencodable on a cp1252 console and would raise UnicodeEncodeError
    mid-render.  Degrading those characters to a replacement marker is much
    better than aborting the run.

    Only applied here, at the CLI entry point — importing the library must never
    reconfigure a host application's streams.
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        encoding = (getattr(stream, "encoding", "") or "").lower()
        if encoding.replace("-", "") in {"utf8", "utf16", "utf32"}:
            continue  # Already able to represent everything.
        try:
            reconfigure(errors="replace")
        except (ValueError, OSError):  # pragma: no cover - stream-dependent
            pass


def run() -> Any:
    """Invoke the Typer application."""
    _make_output_lossy()
    return app()
