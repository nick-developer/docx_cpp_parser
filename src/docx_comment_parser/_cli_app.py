"""The ``docx-comments`` Typer application.

This module imports Typer and Rich unconditionally.  It is loaded only from
:func:`docx_comment_parser.cli.main`, which catches the ImportError and prints
an install hint — so the core package keeps its zero-dependency install while
the console script still exists and explains itself.
"""

from __future__ import annotations

import json
import sys
from dataclasses import replace
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence

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


# ─────────────────────────────────────────────────────────────────────────────
#  LLM export, classification and anonymisation (new in 1.5)
# ─────────────────────────────────────────────────────────────────────────────

POLICY_OPT = typer.Option(
    None, "--anonymize", "-A",
    help=(
        "Redact private information before exporting. One of: "
        "balanced, strict, names-only, secrets-only, gdpr."
    ),
)
MASK_VALUE_OPT = typer.Option(
    None, "--mask-value",
    help="An exact value to mask, e.g. a codename or account number. Repeatable. "
         "Implies --anonymize balanced when no policy is given.",
)
MASK_PATTERN_OPT = typer.Option(
    None, "--mask-pattern",
    help="A regular expression whose matches are masked. Repeatable. Checked "
         "before use; an invalid or over-broad pattern is refused with a reason.",
)
CONTEXT_UNIT_OPT = typer.Option(
    "chars", "--context-unit",
    help="Measure --before/--after in chars, words, sentences or paragraphs.",
)
BEFORE_OPT = typer.Option(
    None, "--before",
    help="Context before the anchor, in --context-unit "
         "(default 500 chars, 80 words, 2 sentences or 1 paragraph).",
)
AFTER_OPT = typer.Option(
    None, "--after", help="Context after the anchor, in --context-unit."
)
COMPRESS_OPT = typer.Option(
    None, "--compress",
    help="Reduce tokens: none, light, balanced or aggressive.",
)
OVERRIDES_OPT = typer.Option(
    None, "--overrides",
    help="Manual category/priority/intent/action_required tags from a .csv, "
         ".json or .jsonl file (see 'classify --export-labels').",
)


def _resolve_policy_option(name: Optional[str]) -> Optional[str]:
    """Validate an --anonymize value at parse time, not mid-export."""
    if name is None:
        return None
    from .privacy.policy import PRESET_NAMES, resolve_policy

    try:
        resolve_policy(name)
    except ValueError:
        raise typer.BadParameter(
            f"unknown policy {name!r}. Choose one of: "
            + ", ".join(sorted(PRESET_NAMES)).replace("_", "-")
        )
    return name.replace("-", "_")


def _build_policy(
    name: Optional[str],
    mask_values: Optional[List[str]],
    mask_patterns: Optional[List[str]],
) -> Optional[Any]:
    """The policy for --anonymize plus any --mask-value / --mask-pattern.

    Patterns are checked here, before a document is opened: an invalid regex
    is reported with its position, an over-broad one with what it would have
    redacted, and a merely suspicious one as a warning that still proceeds.
    """
    preset = _resolve_policy_option(name)
    if not mask_values and not mask_patterns:
        return preset
    from .privacy import InvalidPatternError, make_policy

    try:
        policy = make_policy(
            preset or "balanced",
            values=mask_values or (),
            patterns=mask_patterns or (),
        )
    except InvalidPatternError as exc:
        raise typer.BadParameter(str(exc), param_hint="--mask-value / --mask-pattern")
    for issue in policy.pattern_warnings():
        err_console.print(f"[yellow]Pattern warning:[/] {issue.describe()}", highlight=False)
    return policy


def _context_option(unit: str, before: Optional[int], after: Optional[int]) -> Any:
    from .llm.context import ContextSpec

    try:
        return ContextSpec.of(unit, before, after)
    except ValueError as exc:
        raise typer.BadParameter(str(exc))


def _compress_option(level: Optional[str]) -> Any:
    if level is None:
        return None
    from .llm.compression import resolve_compression

    try:
        return resolve_compression(level)
    except ValueError as exc:
        raise typer.BadParameter(str(exc), param_hint="--compress")


def _overrides_option(path: Optional[Path]) -> Any:
    if path is None:
        return None
    from .llm.overrides import OverrideError, load_overrides

    try:
        return load_overrides(path)
    except OverrideError as exc:
        raise typer.BadParameter(str(exc), param_hint="--overrides")


def _apply_overrides_or_exit(dataset: Any, overrides: Any) -> Any:
    """Apply loaded overrides, turning a mismatch into a clean CLI error."""
    if overrides is None:
        return dataset
    from .llm.overrides import OverrideError

    try:
        return dataset.with_overrides(overrides)
    except OverrideError as exc:
        raise typer.BadParameter(str(exc), param_hint="--overrides")


def _warn_if_private(comments: Sequence[Comment], anonymized: bool) -> None:
    """Scan an export that is about to leave the machine and say what is in it.

    Not a refusal and not a prompt — the export still happens.  But a credential
    pasted into a review comment is the single failure this feature exists to
    prevent, and letting it go to a model provider's logs without a word on the
    terminal would be indefensible.
    """
    if anonymized:
        return
    from .privacy import scan_comments

    scan = scan_comments(comments)
    if not scan.findings:
        return

    kinds = ", ".join(
        f"{entity} x{count}"
        for entity, count in sorted(
            scan.findings.items(), key=lambda item: (-item[1], str(item[0]))
        )[:6]
    )
    style = "bold red" if scan.has_secrets else "yellow"
    err_console.print(
        f"[{style}]Privacy:[/] {scan.total} finding(s) in this export "
        f"({kinds}). Re-run with --anonymize strict to redact them."
    )


@app.command()
def llm(
    file: Path = typer.Argument(
        ..., exists=True, dir_okay=False, readable=True,
        help="Path to the .docx file.",
    ),
    output: Optional[Path] = typer.Option(
        None, "--output", "-o",
        help="Write here. Defaults to standard output. .jsonl writes JSON Lines.",
    ),
    as_jsonl: bool = typer.Option(
        False, "--jsonl", help="Write JSON Lines instead of a JSON array."
    ),
    embeddings: bool = typer.Option(
        False, "--embeddings",
        help="Emit retrieval documents ({id, text, metadata}) instead of records.",
    ),
    granularity: str = typer.Option(
        "thread", "--granularity",
        help="For --embeddings: one document per 'thread' or per 'comment'.",
    ),
    context_unit: str = CONTEXT_UNIT_OPT,
    chars_before: Optional[int] = BEFORE_OPT,
    chars_after: Optional[int] = AFTER_OPT,
    no_context: bool = typer.Option(
        False, "--no-context",
        help="Skip reading the document body. Faster; loses context_before/after.",
    ),
    no_threads: bool = typer.Option(
        False, "--no-threads", help="Do not carry the thread on every record."
    ),
    compress: Optional[str] = COMPRESS_OPT,
    overrides: Optional[Path] = OVERRIDES_OPT,
    anonymize: Optional[str] = POLICY_OPT,
    mask_value: Optional[List[str]] = MASK_VALUE_OPT,
    mask_pattern: Optional[List[str]] = MASK_PATTERN_OPT,
    vault: Optional[Path] = typer.Option(
        None, "--vault",
        help="Write the pseudonym mapping here (mode 0600). Keep it separate "
             "from the export.",
    ),
    author: Optional[str] = AUTHOR_OPT,
    contains: Optional[str] = CONTAINS_OPT,
    resolved: bool = RESOLVED_OPT,
    unresolved_only: bool = UNRESOLVED_OPT,
    threads_only: bool = THREADS_OPT,
    indent: int = typer.Option(2, "--indent", help="JSON indent; 0 for compact."),
) -> None:
    """Export comments as model-ready records.

    Each record carries the comment, the document text either side of its
    anchor, its thread, and rule-based category / intent / priority tags:

        docx-comments llm spec.docx --anonymize strict -o review.jsonl

        docx-comments llm spec.docx --context-unit sentences --before 2 --after 1 --compress balanced

    Without --anonymize the command scans the export and warns on standard
    error about anything private it found. It never sends anything anywhere.
    """
    if granularity not in {"thread", "comment"}:
        raise typer.BadParameter("--granularity must be 'thread' or 'comment'.")
    policy = _build_policy(anonymize, mask_value, mask_pattern)
    if embeddings and context_unit == "chars":
        # Retrieval documents are embedded whole; a shorter default window keeps
        # each one inside an embedding model's input limit.
        chars_before = 300 if chars_before is None else min(chars_before, 300)
        chars_after = 300 if chars_after is None else min(chars_after, 300)
    spec = _context_option(context_unit, chars_before, chars_after)
    compression = _compress_option(compress)
    manual = _overrides_option(overrides)

    selected = filter_comments(
        _load(file),
        author=author,
        contains=contains,
        resolved=_resolved_flag(resolved, unresolved_only),
        threads_only=threads_only,
    )
    _warn_if_private(selected, anonymized=policy is not None)

    from .llm.dataset import build_dataset, to_embeddings_input
    from .privacy import Anonymizer

    engine = (
        Anonymizer(policy, names=[comment.author for comment in selected])
        if policy is not None
        else None
    )

    records = None
    if embeddings:
        payload: Any = to_embeddings_input(
            selected,
            granularity=granularity,
            context=spec,
            include_context=not no_context,
            anonymizer=engine,
            overrides=manual,
            compress=compression,
        )
    else:
        dataset = build_dataset(
            selected,
            context=spec,
            include_thread=not no_threads,
            include_context=not no_context,
            anonymizer=engine,
            compress=compression,
        )
        dataset = _apply_overrides_or_exit(dataset, manual)
        payload = dataset.to_dicts()
        records = dataset

    use_jsonl = as_jsonl or (output is not None and output.suffix.lower() == ".jsonl")

    if output is None:
        # print(), not console.print(): Rich would re-wrap and highlight the
        # payload, which breaks piping into jq.
        if use_jsonl:
            print("\n".join(json.dumps(row, ensure_ascii=False) for row in payload))
        else:
            print(json.dumps(payload, ensure_ascii=False, indent=indent or None))
    else:
        output.parent.mkdir(parents=True, exist_ok=True)
        if use_jsonl:
            with output.open("w", encoding="utf-8", newline="\n") as handle:
                for row in payload:
                    handle.write(json.dumps(row, ensure_ascii=False))
                    handle.write("\n")
        else:
            output.write_text(
                json.dumps(payload, ensure_ascii=False, indent=indent or None),
                encoding="utf-8",
            )
        console.print(
            f"[green]{GLYPH_OK}[/] Wrote {len(payload)} record(s) to [bold]{output}[/]"
        )

    if records is not None and records.compression is not None:
        err_console.print(f"[cyan]{records.compression.summary()}[/]")

    if engine is not None and vault is not None:
        engine.vault.export(vault)
        err_console.print(
            f"[yellow]Vault:[/] {len(engine.vault)} mapping(s) written to "
            f"[bold]{vault}[/]. This file re-identifies the export — store it "
            f"separately."
        )

    if records is not None and records.anonymization is not None:
        report = records.anonymization
        if not report.clean:
            err_console.print(
                f"[bold red]Warning[/] {len(report.residual)} value(s) still "
                f"detectable after redaction."
            )


@app.command()
def prompt(
    file: Path = typer.Argument(
        ..., exists=True, dir_okay=False, readable=True,
        help="Path to the .docx file.",
    ),
    kind: str = typer.Option(
        "action-items", "--kind", "-k",
        help="summary, action-items, resolution, triage or risk.",
    ),
    output: Optional[Path] = typer.Option(
        None, "--output", "-o", help="Write here. Defaults to standard output."
    ),
    budget: int = typer.Option(
        60_000, "--budget", help="Token budget for the comments inside the prompt."
    ),
    as_json: bool = typer.Option(
        False, "--json", help="Emit {system, user, included, omitted} as JSON."
    ),
    context_unit: str = CONTEXT_UNIT_OPT,
    chars_before: Optional[int] = BEFORE_OPT,
    chars_after: Optional[int] = AFTER_OPT,
    compress: Optional[str] = COMPRESS_OPT,
    overrides: Optional[Path] = OVERRIDES_OPT,
    anonymize: Optional[str] = POLICY_OPT,
    mask_value: Optional[List[str]] = MASK_VALUE_OPT,
    mask_pattern: Optional[List[str]] = MASK_PATTERN_OPT,
    vault: Optional[Path] = typer.Option(
        None, "--vault",
        help="Write the pseudonym mapping here (mode 0600), so 'check-response "
             "--vault' can put the real names back into the model's answer.",
    ),
    author: Optional[str] = AUTHOR_OPT,
    contains: Optional[str] = CONTAINS_OPT,
    resolved: bool = RESOLVED_OPT,
    unresolved_only: bool = UNRESOLVED_OPT,
) -> None:
    """Build a ready-to-send prompt about this review.

    Packs as many comments as the budget allows, says what did not fit, and
    requires every claim in the answer to cite a comment id:

        docx-comments prompt spec.docx --kind risk --anonymize strict --compress balanced

    Prints the prompt. Sending it is your business — this command has no
    network access and no API key. Check the answer with 'check-response'.
    """
    from .llm import prompts as prompt_builders
    from .llm.dataset import build_dataset

    # Annotated because the builders differ in their optional keywords; without
    # it mypy joins them to a type it will not let us call.
    builders: Dict[str, Callable[..., Any]] = {
        "summary": prompt_builders.create_review_summary_prompt,
        "action-items": prompt_builders.create_action_items_prompt,
        "resolution": prompt_builders.create_resolution_prompt,
        "triage": prompt_builders.create_triage_prompt,
        "risk": prompt_builders.create_risk_prompt,
    }
    builder = builders.get(kind.replace("_", "-"))
    if builder is None:
        raise typer.BadParameter(
            f"unknown prompt kind {kind!r}. Choose one of: "
            + ", ".join(sorted(builders))
        )

    policy = _build_policy(anonymize, mask_value, mask_pattern)
    spec = _context_option(context_unit, chars_before, chars_after)
    compression = _compress_option(compress)
    manual = _overrides_option(overrides)
    selected = filter_comments(
        _load(file),
        author=author,
        contains=contains,
        resolved=_resolved_flag(resolved, unresolved_only),
    )
    _warn_if_private(selected, anonymized=policy is not None)

    dataset = build_dataset(selected, anonymize=policy, context=spec, compress=compression)
    dataset = _apply_overrides_or_exit(dataset, manual)
    built = builder(dataset.records, budget=budget)

    text = (
        json.dumps(built.to_dict(), ensure_ascii=False, indent=2)
        if as_json
        else f"{built.system}\n\n{'-' * 70}\n\n{built.user}"
    )

    if output is None:
        print(text)
    else:
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(text, encoding="utf-8")
        console.print(
            f"[green]{GLYPH_OK}[/] Wrote a {built.kind} prompt "
            f"(~{built.tokens} tokens, {len(built.included)} comment(s)) "
            f"to [bold]{output}[/]"
        )

    if built.omitted:
        err_console.print(
            f"[yellow]{built.omitted} comment(s) did not fit the budget "
            f"and are not in the prompt.[/]"
        )
    if built.flagged:
        err_console.print(
            "[yellow]Prompt injection:[/] comment(s) "
            + ", ".join(f"#{identifier}" for identifier in built.flagged)
            + " read like instructions to a model. The prompt tells the model "
            "to treat them as data; review them before sending."
        )
    if dataset.compression is not None:
        err_console.print(f"[cyan]{dataset.compression.summary()}[/]")
    if vault is not None:
        if dataset.anonymizer is None:
            err_console.print(
                "[yellow]--vault ignored:[/] nothing was anonymised, so there is "
                "no mapping to write. Add --anonymize."
            )
        else:
            dataset.anonymizer.vault.export(vault)
            err_console.print(
                f"[yellow]Vault:[/] {len(dataset.anonymizer.vault)} mapping(s) written "
                f"to [bold]{vault}[/]. This file re-identifies the prompt — store it "
                f"separately."
            )


@app.command()
def classify(
    file: Path = typer.Argument(
        ..., exists=True, dir_okay=False, readable=True,
        help="Path to the .docx file.",
    ),
    category: Optional[str] = typer.Option(
        None, "--category", help="Show only this category (case-insensitive)."
    ),
    priority: Optional[str] = typer.Option(
        None, "--priority", help="Show only this priority: blocker, high, medium, low."
    ),
    actions_only: bool = typer.Option(
        False, "--actions-only", help="Show only comments that ask for something."
    ),
    overrides: Optional[Path] = OVERRIDES_OPT,
    export_labels: Optional[Path] = typer.Option(
        None, "--export-labels",
        help="Write an editable labels CSV. Change any category / priority / "
             "intent / action_required cell and pass the file back with --overrides.",
    ),
    limit: Optional[int] = LIMIT_OPT,
    author: Optional[str] = AUTHOR_OPT,
    contains: Optional[str] = CONTAINS_OPT,
    resolved: bool = RESOLVED_OPT,
    unresolved_only: bool = UNRESOLVED_OPT,
) -> None:
    """Categorise comments by subject, intent and priority.

    Rule-based, local, and instant — no model involved. Correct it by hand:

        docx-comments classify spec.docx --export-labels labels.csv

        docx-comments classify spec.docx --overrides labels.csv --priority blocker

    Manually set tags are marked with * in the table.
    """
    from .llm.dataset import build_dataset

    manual = _overrides_option(overrides)
    selected = filter_comments(
        _load(file),
        author=author,
        contains=contains,
        resolved=_resolved_flag(resolved, unresolved_only),
    )
    # Records rather than bare classifications: they carry the tags *and* the
    # manual corrections, through the same path the LLM export uses.
    dataset = build_dataset(selected, include_context=False, include_thread=False)
    dataset = _apply_overrides_or_exit(dataset, manual)
    tagged = list(dataset.records)

    if export_labels is not None:
        dataset.export_labels(export_labels)
        console.print(
            f"[green]{GLYPH_OK}[/] Wrote {len(tagged)} label row(s) to "
            f"[bold]{export_labels}[/]. Edit the tags and pass it back with --overrides."
        )

    if category:
        needle = category.casefold()
        tagged = [record for record in tagged if record.category.casefold() == needle]
    if priority:
        needle = priority.casefold()
        tagged = [record for record in tagged if record.priority == needle]
    if actions_only:
        tagged = [record for record in tagged if record.action_required]

    if not tagged:
        console.print("[yellow]No comments matched.[/]")
        return

    counts: Dict[str, int] = {}
    for record in tagged:
        counts[record.category] = counts.get(record.category, 0) + 1

    breakdown = Table(title="Categories", header_style="bold cyan")
    breakdown.add_column("Category")
    breakdown.add_column("Comments", justify="right")
    for name, count in sorted(counts.items(), key=lambda item: (-item[1], item[0])):
        breakdown.add_row(name, str(count))
    console.print(breakdown)

    shown = tagged[:limit] if limit else tagged
    table = Table(title="Classified comments", header_style="bold cyan", expand=True)
    table.add_column("ID", justify="right", style="dim", no_wrap=True)
    table.add_column("Author", style="green", no_wrap=True)
    table.add_column("Category", no_wrap=True)
    table.add_column("Priority", no_wrap=True)
    table.add_column("Intent", no_wrap=True)
    table.add_column("Comment", overflow="fold")

    priority_colour = {
        "blocker": "bold red", "high": "red", "medium": "yellow", "low": "dim"
    }

    def mark(record: Any, field: str, value: str) -> str:
        return f"{value}*" if field in record.overridden else value

    for record in shown:
        table.add_row(
            str(record.comment_id),
            record.author or GLYPH_EMPTY,
            mark(record, "category", record.category),
            f"[{priority_colour.get(record.priority, '')}]"
            f"{mark(record, 'priority', record.priority)}[/]",
            mark(record, "intent", record.intent.replace("_", " ")),
            _truncate(record.comment, 70),
        )
    console.print(table)

    if limit and len(tagged) > limit:
        console.print(f"[dim]Showing {limit} of {len(tagged)} comment(s).[/]")
    if any(record.overridden for record in shown):
        console.print("[dim]* set by hand (--overrides).[/]")


@app.command()
def anonymize(
    file: Path = typer.Argument(
        ..., exists=True, dir_okay=False, readable=True,
        help="Path to the .docx file.",
    ),
    policy: str = typer.Option(
        "balanced", "--policy", "-p",
        help="balanced, strict, names-only, secrets-only or gdpr.",
    ),
    mask_value: Optional[List[str]] = MASK_VALUE_OPT,
    mask_pattern: Optional[List[str]] = MASK_PATTERN_OPT,
    output: Optional[Path] = typer.Option(
        None, "--output", "-o",
        help="Write the redacted comments here (.json or .csv). "
             "Omit to only report what would change.",
    ),
    vault: Optional[Path] = typer.Option(
        None, "--vault",
        help="Write the pseudonym mapping here (mode 0600). Store it separately.",
    ),
    scan_only: bool = typer.Option(
        False, "--scan", help="Only report what is there; change nothing."
    ),
    fail_on_secret: bool = typer.Option(
        False, "--fail-on-secret",
        help="Exit non-zero if a credential is found. For use in CI.",
    ),
) -> None:
    """Mask names and other private information in a document's comments.

        docx-comments anonymize contract.docx -p strict -o clean.json

        docx-comments anonymize contract.docx --mask-value "Project Falcon" --mask-pattern "TCK-\\d{6}"

    With --scan it reports what is there and changes nothing, which is the
    check to put in front of anything that leaves the machine.
    """
    from .privacy import Anonymizer, anonymize_comments, scan_comments
    from .privacy.policy import resolve_policy

    built = _build_policy(policy, mask_value, mask_pattern)
    resolved_policy = resolve_policy(built if built is not None else True)

    comments = _load(file)

    if scan_only:
        scan = scan_comments(comments, policy=resolved_policy)
        console.print(Panel(scan.summary(), title="Privacy scan", expand=False))
        if fail_on_secret and scan.has_secrets:
            raise typer.Exit(code=1)
        return

    engine = Anonymizer(
        resolved_policy, names=[comment.author for comment in comments]
    )
    cleaned, report = anonymize_comments(comments, anonymizer=engine)
    console.print(Panel(report.summary(), title="Anonymisation", expand=False))

    if output is not None:
        if output.suffix.lower() == ".csv":
            csv_exporter.export_csv(cleaned, output)
        else:
            json_exporter.export_json(cleaned, output)
        console.print(
            f"[green]{GLYPH_OK}[/] Wrote {len(cleaned)} redacted comment(s) "
            f"to [bold]{output}[/]"
        )

    if vault is not None:
        engine.vault.export(vault)
        err_console.print(
            f"[yellow]Vault:[/] {len(engine.vault)} mapping(s) written to "
            f"[bold]{vault}[/]. Anyone with this file can undo the redaction."
        )

    if not report.clean:
        err_console.print(
            f"[bold red]Warning[/] {len(report.residual)} value(s) still "
            f"detectable after redaction."
        )
        raise typer.Exit(code=1)

    if fail_on_secret:
        scan = scan_comments(comments)
        if scan.has_secrets:
            raise typer.Exit(code=1)


@app.command("check-pattern")
def check_pattern_command(
    patterns: List[str] = typer.Argument(..., help="One or more regular expressions."),
) -> None:
    """Check custom masking patterns before using them.

        docx-comments check-pattern "TCK-\\d{6}" "(unclosed"

    Reports, for each pattern, whether it compiles, whether it can match empty
    text, whether it would mask ordinary words, and whether it risks very slow
    matching. Exits 1 when any pattern would be refused.
    """
    from .privacy.validation import check_pattern

    refused = False
    for index, pattern in enumerate(patterns, start=1):
        issues = check_pattern(pattern, f"pattern-{index}")
        if not issues:
            console.print(f"[green]{GLYPH_OK}[/] {pattern}", highlight=False)
            continue
        for issue in issues:
            refused = refused or issue.is_error
            style = "bold red" if issue.is_error else "yellow"
            label = "invalid" if issue.is_error else "warning"
            console.print(f"[{style}]{label}[/] {issue.describe()}", highlight=False)
    if refused:
        raise typer.Exit(code=1)


@app.command("check-response")
def check_response_command(
    prompt_file: Path = typer.Argument(
        ..., exists=True, dir_okay=False, readable=True,
        help="The prompt as written by 'prompt --json'.",
    ),
    response_file: Path = typer.Argument(
        ..., exists=True, dir_okay=False, readable=True,
        help="The model's answer, as text.",
    ),
    vault: Optional[Path] = typer.Option(
        None, "--vault", help="Restore real names from this vault file."
    ),
    output: Optional[Path] = typer.Option(
        None, "--output", "-o", help="Write the parsed (and restored) JSON here."
    ),
) -> None:
    """Check a model's answer against the prompt that asked for it.

        docx-comments check-response prompt.json answer.txt --vault names.json -o result.json

    Extracts the JSON from the answer, verifies every cited comment id was in
    the prompt, checks the required keys, and restores real names locally.
    Exits 1 when the answer cites comments that were not in the prompt or
    omits required keys.
    """
    from .llm.responses import ResponseParseError, parse_response
    from .privacy import Vault

    try:
        prompt_data = json.loads(prompt_file.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise typer.BadParameter(
            f"{prompt_file} is not JSON ({exc}). Write it with 'prompt --json'.",
            param_hint="PROMPT_FILE",
        )
    restore = Vault.load(vault) if vault is not None else None
    try:
        parsed = parse_response(
            prompt_data, response_file.read_text(encoding="utf-8"), anonymizer=restore
        )
    except ResponseParseError as exc:
        err_console.print(f"[bold red]Unreadable answer:[/] {exc}")
        raise typer.Exit(code=1)

    lines = [
        f"Kind              {parsed.kind or GLYPH_EMPTY}",
        f"Comments cited    {len(parsed.cited)}",
    ]
    lines.extend(f"Problem           {problem}" for problem in parsed.problems())
    if parsed.valid:
        lines.append("Verified          every citation is in the prompt")
    console.print(Panel("\n".join(lines), title="Response check", expand=False))

    if output is not None:
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(
            json.dumps(parsed.data, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        console.print(f"[green]{GLYPH_OK}[/] Wrote the parsed answer to [bold]{output}[/]")
    if not parsed.valid:
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
