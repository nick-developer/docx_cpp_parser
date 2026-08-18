# docx_comment_parser

A C++17 shared library that extracts every piece of comment metadata from `.docx` files — text, authors, dates, reply threads, anchor text, and resolution status — with full Python bindings via pybind11.

Since v1.2 it also turns those comments into **spreadsheets, DataFrames and JSON**, and ships a **command-line tool** so you can use it without writing any Python.

New in v1.3: turn a document into a **shareable review report** — one self-contained HTML file with charts, search and thread-by-thread reading, that opens with no internet connection.

[![Tests](https://img.shields.io/badge/tests-323%20passing-brightgreen)](#testing)
[![C++17](https://img.shields.io/badge/C%2B%2B-17-blue)](#building-the-shared-library-with-cmake)
[![Python ≥ 3.9](https://img.shields.io/badge/python-%E2%89%A53.9-blue)](#quick-start--python)
[![License: MIT](https://img.shields.io/badge/license-MIT-green)](#license)

---

## Table of Contents

1. [What it does](#what-it-does)
2. [What's new in v1.3](#whats-new-in-v13)
3. [What's new in v1.2](#whats-new-in-v12)
4. [Quick start — Python](#quick-start--python)
5. [Quick start — command line](#quick-start--command-line)
6. [Quick start — C++](#quick-start--c)
7. [Installation](#installation)
8. [Exporting comments](#exporting-comments)
9. [Review reports](#review-reports)
10. [Command-line guide](#command-line-guide)
11. [Python API reference](#python-api-reference)
12. [C++ API reference](#c-api-reference)
13. [Architecture](#architecture)
14. [Performance](#performance)
15. [Testing](#testing)
16. [Changelog](#changelog)
17. [License](#license)

---

## What it does

A `.docx` file is a ZIP archive containing XML parts defined by the OOXML standard. Comments are spread across up to four of those parts, each requiring a different parsing strategy:

| Part | Content | Parse method |
|---|---|---|
| `word/comments.xml` | Core comment data (id, author, date, text) | DOM — always small |
| `word/commentsExtended.xml` | Reply threading, `done` flag (OOXML 2016+) | SAX streaming |
| `word/commentsIds.xml` | Para-ID cross-reference (fallback) | SAX streaming |
| `word/document.xml` | Anchor text via `commentRangeStart/End` | SAX streaming — can be very large |

`docx_comment_parser` opens the ZIP without decompressing it fully, inflates each part on demand, parses it, and discards the raw bytes. The result is a fully resolved `CommentMetadata` object for every comment in the document, with reply chains linked by id and anchor text extracted from the document body.

**What you get per comment:**

- Identity: `id`, `author`, `initials`, `date` (ISO-8601 string)
- Content: `text` (full plain-text body, XML entities decoded), `paragraph_style`
- Anchoring: `referenced_text` — the exact document text the comment is attached to
- Threading: `is_reply`, `parent_id`, `replies` list, `thread_ids` chain
- Resolution: `done` flag from `commentsExtended.xml`

---

## What's new in v1.3

Everything from v1.2 still works exactly as before. v1.3 adds one thing: **you can now hand your review to someone else.**

Until now the library gave you data — rows, JSON, a DataFrame. Useful if you write code. Useless if the person who needs to see the comments is a manager, a client, or a lawyer.

```python
parser.export_html_report("review.html")
```

That writes **one HTML file**. Double-click it and you get a page with:

- the headline numbers — how many comments, how many resolved, how many still open, who reviewed
- a per-reviewer table showing who is keeping up and who is not
- a chart of comment activity per day and per week
- every conversation, expandable, in reading order
- a search box and filters for author, status, keyword and date

**It is one file.** No folder of assets, no web server, no internet. Email it, put it on a USB stick, open it on a plane — it works, because the charts, the styling and the comments are all inside the file itself.

If you prefer text you can paste into a pull request or a ticket:

```python
parser.export_markdown_report("review.md")
```

There is a terminal command too:

```bash
docx-comments report contract.docx -o review.html
```

**Nothing got heavier.** The base install still has zero dependencies, and importing the library does not load the reporting code at all — you only pay for a report when you ask for one. The parser is untouched and just as fast; see [Performance](#performance).

---

## What's new in v1.2

Everything from v1.1 still works exactly as before. v1.2 adds two things on top.

**1. You can get your comments as a table.**

Before, you had to loop over comment objects and build your own rows. Now one method call gives you a spreadsheet, a DataFrame, or JSON:

```python
parser.to_dataframe()          # pandas
parser.to_polars()             # polars
parser.export_csv("out.csv")   # spreadsheet — no extra packages needed
parser.export_json("out.json") # JSON — no extra packages needed
```

**2. You can use it from a terminal, without writing Python.**

```bash
docx-comments parse report.docx          # see the comments
docx-comments stats report.docx          # who commented, how much is done
docx-comments unresolved report.docx     # what's still open
docx-comments export report.docx --csv -o comments.csv
docx-comments batch ./documents          # a whole folder at once
```

**Nothing got heavier.** Installing the package still pulls in zero dependencies. pandas, polars and the CLI tools are optional extras you opt into. The parser itself is unchanged and just as fast — see [Performance](#performance).

**Two long-standing bugs were fixed** along the way; both are described in the [Changelog](#changelog).

### One small internal change

The compiled C++ module moved from being the whole package to sitting *inside* it, at `docx_comment_parser._core`. This is invisible in normal use — `import docx_comment_parser as dcp` and `dcp.DocxParser()` behave identically. The only code affected is anything that imported the private extension file by path, which was never a supported thing to do.

---

## Quick start — Python

```python
import docx_comment_parser as dcp

parser = dcp.DocxParser()
parser.parse("report.docx")

# Print every comment
for c in parser.comments():
    prefix = "  ↳ [reply]" if c.is_reply else f"[{c.id}]"
    print(f"{prefix} {c.author} ({c.date[:10]}): {c.text[:80]}")
    if c.referenced_text:
        print(f"       anchored to: \"{c.referenced_text[:60]}\"")
```

```
[0] Alice (2026-01-15): This sentence needs rephrasing for clarity and conciseness.
       anchored to: "The methodology employed in this study is fundamentally flaw"
  ↳ [reply] Bob (2026-01-16): Agreed. Suggest: "This sentence requires revision."
[2] Alice (2026-01-17): Please verify the statistical analysis in section 3 & 4.
       anchored to: "Results in section 3 and 4 show p < 0.05."
```

### …or skip the loop and get a table

The same parser can hand you the whole document as rows:

```python
import docx_comment_parser as dcp

parser = dcp.DocxParser()
parser.parse("report.docx")

# A spreadsheet you can open in Excel — needs nothing extra installed.
parser.export_csv("comments.csv")

# A pandas DataFrame — needs `pip install docx-comment-parser[pandas]`.
df = parser.to_dataframe()
print(df[["author", "text", "resolved"]].head())
```

```
        author                                       text  resolved
0        Alice  This sentence needs rephrasing for clari…     False
1          Bob  Agreed. Suggest: "This sentence requires…      True
2        Alice  Please verify the statistical analysis i…     False
```

Because it is a real DataFrame, ordinary pandas works on it:

```python
# Who has the most open comments?
open_by_author = df[~df["resolved"]].groupby("author").size()

# How many comments mention security?
security = df[df["text"].str.contains("security", case=False)]
```

---

## Quick start — command line

Install the CLI extra once:

```bash
pip install "docx-comment-parser[cli]"
```

Then look at a document without writing any code:

```bash
docx-comments parse report.docx
```

```
                              Comments — report.docx
  ID   Author   Date              St   Comment                            Anchored to
  ────────────────────────────────────────────────────────────────────────────────────
   0   Alice    2026-01-15 09:12  ○    This sentence needs rephrasing…    The methodology…
   1   Bob      2026-01-16 11:03  ✓      ↳ Agreed. Suggest: "This sen…
   2   Alice    2026-01-17 14:40  ○    Please verify the statistical…     Results in sec…

3 comment(s)  1 resolved  2 open
```

`○` means open, `✓` means resolved, and `↳` marks a reply.

On a terminal that cannot display those characters — a stock Windows console, for instance — the same table prints with plain ASCII (`open` / `done` / `>`) instead. Nothing is lost and nothing crashes; the tool checks what your terminal can handle and adapts.

A few more things you can do:

```bash
# Only Alice's comments
docx-comments parse report.docx --author alice

# Only comments that mention "security", anywhere in the comment or the text it points at
docx-comments parse report.docx --contains security

# Turn a folder of documents into one spreadsheet
docx-comments batch ./reviews -o all_comments.csv
```

The full command reference is in the [Command-line guide](#command-line-guide).

---

## Quick start — C++

```cpp
#include "docx_comment_parser.h"
#include <iostream>

int main() {
    docx::DocxParser parser;
    parser.parse("report.docx");

    for (const auto& c : parser.comments()) {
        std::cout << "[" << c.id << "] "
                  << c.author << ": "
                  << c.text.substr(0, 80) << "\n";
        if (!c.referenced_text.empty())
            std::cout << "  anchored to: \"" << c.referenced_text << "\"\n";
    }

    const auto& s = parser.stats();
    std::cout << "\n" << s.total_comments << " comment(s), "
              << s.unique_authors.size() << " author(s)\n";
}
```

---

## Installation

### Choosing what to install

The base package has **no dependencies at all**. Optional features live behind extras, so you only install what you use:

```bash
pip install docx-comment-parser              # parser + CSV/JSON/Markdown. Zero dependencies.
pip install "docx-comment-parser[pandas]"    # + to_dataframe()
pip install "docx-comment-parser[polars]"    # + to_polars()
pip install "docx-comment-parser[cli]"       # + the docx-comments command
pip install "docx-comment-parser[report]"    # + export_html_report()
pip install "docx-comment-parser[all]"       # everything above
```

| Extra | Adds | Gives you |
|---|---|---|
| *(none)* | — | `DocxParser`, `BatchParser`, `export_csv()`, `export_json()`, `to_dict()`, `to_json()`, `export_markdown_report()` |
| `pandas` | pandas ≥ 2.0 | `to_dataframe()` |
| `polars` | polars ≥ 1.0 | `to_polars()` |
| `cli` | typer, rich | the `docx-comments` terminal command |
| `report` | jinja2 ≥ 3.0 | `export_html_report()` |
| `all` | all of the above | everything |

The **Markdown** report deliberately needs no extra, exactly like CSV and JSON. Only the interactive HTML report needs `[report]`.

If you call a method whose extra is missing, you get a message telling you exactly what to install rather than an obscure `ImportError`:

```
ImportError: pandas is required for this export but is not installed.
Install it with:  pip install docx-comment-parser[pandas]
```

### Linux / macOS

```bash
# 1. Install system dependencies
sudo apt install build-essential g++ cmake zlib1g-dev   # Debian/Ubuntu
brew install cmake zlib                                  # macOS

# 2. Install the Python build dependency
pip install pybind11

# 3a. Build the Python extension in-place (for development)
python setup.py build_ext --inplace

# 3b. OR install permanently into the current environment
pip install .
```

Verify:

```bash
python -c "import docx_comment_parser; print('OK')"
```

### Windows — MSVC (no vcpkg required)

`docx_comment_parser` bundles a self-contained DEFLATE inflate implementation (`vendor/zlib/zlib.h`). No external zlib install is needed on MSVC — pybind11 is the only dependency.

```powershell
# 1. Open "Developer Command Prompt for VS 2022" (or run vcvarsall.bat x64)
# 2. Install the only required Python dependency
pip install pybind11

# 3. Build
python setup.py build_ext --inplace
```

Verify:

```powershell
python -c "import docx_comment_parser; print('OK')"
```

The compiler invocation will include `-Ivendor` and no `/link zlib.lib`:

```
cl.exe /c /nologo /O2 /std:c++17 /DDOCX_BUILDING_DLL
    -Iinclude -Ivendor -I<pybind11\include> ...
    /Tpsrc/zip_reader.cpp ...
link.exe ... /OUT:docx_comment_parser.cp314-win_amd64.pyd
```

### Windows — MinGW-w64 (MSYS2)

```bash
# Inside an MSYS2 MINGW64 shell
pacman -S mingw-w64-x86_64-gcc mingw-w64-x86_64-cmake \
          mingw-w64-x86_64-zlib mingw-w64-x86_64-python \
          mingw-w64-x86_64-python-pip
pip install pybind11
python setup.py build_ext --inplace
```

### Building the shared library with CMake

If you need the C++ `.so`/`.dll` without Python bindings:

```bash
cmake -B build -DCMAKE_BUILD_TYPE=Release
cmake --build build -j$(nproc)
```

CMake build options:

| Option | Default | Effect |
|---|---|---|
| `BUILD_PYTHON_BINDINGS` | `ON` | Compile the pybind11 extension |
| `BUILD_TESTS` | `ON` | Build and register the test suite with CTest |
| `CMAKE_BUILD_TYPE` | `Release` | `Debug` / `Release` / `RelWithDebInfo` |

---

## Exporting comments

### The idea

`parser.comments()` gives you comment *objects* shaped like the OOXML file format. That is the right shape for reading one comment at a time, but the wrong shape for a spreadsheet: reply links use `-1` to mean "no parent", "resolved" is called `done`, dates are raw text, and nothing records which file a comment came from.

The export layer flattens all of that into plain rows. **One comment = one row.** Same columns every time.

### The five export methods

Every method works on any parsed document:

```python
parser = dcp.DocxParser()
parser.parse("report.docx")

rows  = parser.to_comments()          # list of Comment objects
df    = parser.to_dataframe()         # pandas DataFrame       [pandas]
pf    = parser.to_polars()            # polars DataFrame       [polars]
dicts = parser.to_dict()              # list of plain dicts
text  = parser.to_json()              # JSON string

parser.export_csv("comments.csv")     # write a CSV file
parser.export_json("comments.json")   # write a JSON file
```

`export_csv` and `export_json` return the path they wrote, and create missing folders for you:

```python
path = parser.export_csv("reports/2026/q1/comments.csv")   # folders created
print(f"Wrote {path}")
```

### The columns

| Column | Type | What it is |
|---|---|---|
| `comment_id` | int | The comment's id in the document |
| `parent_id` | int or empty | The comment this one replies to. Empty for a top-level comment |
| `author` | str | Who wrote it |
| `initials` | str | Their initials, as Word recorded them |
| `date` | str | The timestamp exactly as stored in the file |
| `date_parsed` | datetime | The same timestamp as a real date you can sort and filter on |
| `text` | str | The comment itself |
| `referenced_text` | str | The document text the comment points at |
| `paragraph_style` | str | Word style of the comment's first paragraph |
| `resolved` | bool | Whether it has been marked resolved |
| `is_reply` | bool | Whether it is a reply to another comment |
| `thread_depth` | int | 0 for a top-level comment, 1 for a reply, 2 for a reply to a reply… |
| `document_name` | str | Which file it came from |
| `root_id` | int | The id of the first comment in this conversation |
| `reply_count` | int | How many direct replies it has |
| `para_id`, `para_id_parent` | str | Word's internal paragraph ids |
| `range_start_para_id`, `range_end_para_id` | str | Ids marking where the comment is anchored |
| `paragraph_index` | int | Which paragraph in the document it is attached to (`-1` if unknown) |
| `run_index` | int | Which run inside that paragraph (`-1` if unknown) |

The first thirteen are what most people use. The rest carry the low-level anchoring detail through, so exporting never loses information compared with reading `parser.comments()` directly.

#### Two columns for dates, on purpose

`date` is the untouched string from the file. `date_parsed` is that string turned into a real datetime. You get both because they fail differently: if Word wrote something unusual, `date_parsed` becomes empty but `date` still shows you exactly what was in the document. No data is ever silently lost, and a single odd timestamp cannot break a 10,000-comment export.

```python
df["date_parsed"].dt.month              # works like any datetime column
df[df["date_parsed"] > "2026-01-01"]    # filter by date
```

### Filtering before you export

`filter_comments` applies the same rules the CLI uses. Every argument is optional and they combine with AND:

```python
from docx_comment_parser import DocxParser
from docx_comment_parser.filters import filter_comments
from docx_comment_parser.exporters import export_csv

parser = DocxParser()
parser.parse("report.docx")

open_security_notes = filter_comments(
    parser.to_comments(),
    contains="security",   # in the comment OR the text it points at
    resolved=False,        # only unresolved
)

export_csv(open_security_notes, "security_todo.csv")
```

| Argument | Effect |
|---|---|
| `author="alice"` | Author contains "alice", ignoring case. Matches "Alice Smith" |
| `contains="security"` | The word appears in the comment text or in the text it points at |
| `resolved=True` / `False` / `None` | Only resolved / only open / both |
| `threads_only=True` | Only comments that are part of a conversation, dropping standalone notes |

### Several documents at once

`BatchParser` parses files in parallel and exports them as one combined table. The `document_name` column tells you which file each row came from:

```python
import glob
import docx_comment_parser as dcp

batch = dcp.BatchParser(max_threads=0)      # 0 = use every CPU core
batch.parse_all(glob.glob("reviews/*.docx"))

df = batch.to_dataframe()
print(df.groupby("document_name").size())   # comments per file

batch.export_csv("all_reviews.csv")
```

Files that fail to parse do not stop the run. They are reported separately and skipped by the export:

```python
for path, message in batch.errors().items():
    print(f"Could not read {path}: {message}")

print(batch.parsed_files())    # only the files that worked
```

### Exporters as plain functions

The methods above are thin wrappers. If you have built your own list of comments, the underlying functions take it directly:

```python
from docx_comment_parser.exporters import (
    to_dataframe, to_polars, to_dict, to_json, export_csv, export_json,
)

mine = [c for c in parser.to_comments() if c.author == "Alice"]
to_dataframe(mine)
export_csv(mine, "alice.csv")
```

### Encoding notes

`export_csv` writes UTF-8. If you plan to open the file by double-clicking it in Excel on Windows, ask for the byte-order mark so accented names survive:

```python
parser.export_csv("comments.csv", encoding="utf-8-sig")
parser.export_csv("comments.csv", delimiter=";")   # for locales where Excel expects ;
```

`to_json` always produces valid JSON with dates as ISO-8601 strings, so it can be posted to an API or read back with `json.loads` without a custom decoder.

---

## Review reports

Exports give you **data**. Reports give you something a person can read.

### The one-liner

```python
import docx_comment_parser as dcp

parser = dcp.DocxParser()
parser.parse("contract.docx")

parser.export_html_report("review.html")      # needs [report]
parser.export_markdown_report("review.md")    # needs nothing
```

Both return the path they wrote and create missing folders for you.

### What the HTML report contains

Open `review.html` in any browser and you get five things, top to bottom:

| Section | What it answers |
|---|---|
| **Overview** | How many comments are there, how many are resolved, how many are still open, how many people reviewed, and over what period |
| **Reviewers** | Who wrote how many comments, and what share of each person's comments got resolved |
| **Timeline** | When the reviewing actually happened — a bar per day, or per week |
| **Comments → Threads** | Every conversation, collapsed to one line, expandable to read the whole exchange |
| **Comments → Table** | The same comments as flat rows, when you want to scan rather than read |

Above the comments is a filter bar. Type in the search box and the page filters as you type, matching the comment text, the document text it points at, and the author name. The dropdowns filter by reviewer and by status; the two date boxes narrow to a period. They combine, so "everything Alice left open in March" is three clicks.

Every chart bar shows exact numbers when you hover it, and under each chart there is a **Show the numbers behind this chart** link that reveals the same data as a plain table — useful for copying figures out, and for anyone who cannot read the chart.

There is a light/dark button in the top right, and the page follows your system setting until you touch it.

### Why it is one file

The report has **no external references at all**. The styling, the interactive code, the charts and the comments are all written inside the `.html` file.

That matters more than it sounds:

- it opens with no internet connection
- it still works in five years, when whatever CDN it might have used is gone
- it survives being emailed as an attachment
- nothing is sent anywhere when someone opens it — there is no server involved, so a confidential document stays confidential

The charts are plain SVG drawn when the file is written, not a JavaScript charting library. That is why a 5,000-comment report is under a megabyte and appears instantly instead of animating into place.

**Want a PDF?** Open the report and print it (`Ctrl+P` → *Save as PDF*). The page has a print stylesheet that hides the buttons and filters, expands every thread, and keeps sections from splitting across pages. That is why this package does not depend on a PDF library — the browser already does it well.

### Reporting on part of a document

Reports take a list of comments, so anything you can filter, you can report on:

```python
from docx_comment_parser.filters import filter_comments
from docx_comment_parser.reporting import export_html_report

still_open = filter_comments(parser.to_comments(), resolved=False)
export_html_report(still_open, "open_items.html", title="Outstanding issues")
```

`title` replaces the heading; it defaults to the document's file name.

### Several documents in one report

`BatchParser` reports on everything it parsed, and the report gains a **Document** column so you can tell the files apart:

```python
import glob

batch = dcp.BatchParser(max_threads=0)
batch.parse_all(glob.glob("reviews/*.docx"))

batch.export_html_report("all_reviews.html", title="Q1 review round")
```

### The Markdown report

Same numbers, plain text, no extras required. It is built to be pasted somewhere:

```python
print(parser.to_markdown_report())
```

```markdown
# Comment review — contract.docx

## Overview

| Metric | Value |
| --- | --- |
| Total comments | 42 |
| Resolved | 31 (74%) |
| Still open | 11 |
| Reviewers | 4 |

## Open items (11)

- **[#3] Alice** (2026-01-15): Clause 4.2 needs legal sign-off.
  - 2 replies, 1 still open
```

It leads with **Open items** — the things somebody still has to do — because that is what a reviewer opens the file for. A full transcript of every conversation follows; pass `include_threads=False` for just the summary.

```python
parser.export_markdown_report("digest.md", include_threads=False)
```

Because it is ordinary Markdown, it renders as-is in a GitHub pull request, a Jira ticket, or a Confluence page — and it is a good format to hand to an LLM.

### Just the numbers

If you want the statistics without any rendering, the analytics layer is public and needs nothing installed:

```python
from docx_comment_parser import build_report_data

data = build_report_data(parser.to_comments())

print(data.overview.unresolved, "still open")
print(data.overview.resolution_rate)              # 0.0 – 1.0

for author in data.authors:                       # busiest reviewer first
    print(author.author, author.total, f"{author.resolution_percent:.0f}%")

for bucket in data.weekly:
    print(bucket.label, bucket.total, bucket.resolved)

for thread in data.threads:
    print(thread.root.text, "-", thread.size, "comments")
```

A `thread` is counted as resolved only when **every** comment in it is resolved — one open reply keeps the whole conversation open, which is how a person reads it.

### Using your own template

If you want the report to match a house style, pass your own Jinja2 template:

```python
parser.export_html_report("review.html", template="my_template.html.j2")
```

It receives `data` (everything above), plus `payload`, `css`, `js`, `daily_chart`, `weekly_chart`, `default_scale` and `version`. Your template's own folder is searched first, so you can `{% extends "report.html.j2" %}` and override just one block.

### Reproducible output

Reports stamp the time they were generated, so two runs differ. Pass a fixed timestamp and the output is byte-for-byte identical — handy for checking a report into version control and diffing it:

```python
from datetime import datetime, timezone

parser.export_html_report(
    "review.html",
    generated_at=datetime(2026, 3, 1, tzinfo=timezone.utc),
)
```

---

## Command-line guide

Install with `pip install "docx-comment-parser[cli]"`, then run `docx-comments --help`. Every command has its own `--help` too.

The command exists even without the extra installed — it just tells you how to install it instead of crashing.

### `parse` — look at the comments

```bash
docx-comments parse report.docx
docx-comments parse report.docx --author alice --unresolved
docx-comments parse report.docx --limit 20
```

### `stats` — a summary and a per-author breakdown

```bash
docx-comments stats report.docx
```

```
╭─ report.docx ─────────────────╮
│ Total comments     42         │
│ Root comments      18         │
│ Replies            24         │
│ Resolved           31         │
│ Unresolved         11         │
│ Unique authors      4         │
│ Earliest comment   2026-01-15 │
│ Latest comment     2026-02-02 │
╰───────────────────────────────╯

                    By author
  Author    Comments   Resolved   Open   Resolution rate
  ──────────────────────────────────────────────────────
  Alice           19         15      4               79%
  Bob             12          9      3               75%
  Carol           11          7      4               64%
```

### `unresolved` — what is still open

Prints the open comments and **exits with status 1** if there are any. That makes it usable as a gate in a script or CI job:

```bash
docx-comments unresolved spec.docx || echo "Review is not finished yet"
```

Exit code `0` means nothing is left open.

### `export` — write JSON or CSV

```bash
docx-comments export report.docx --csv -o comments.csv
docx-comments export report.docx --json -o comments.json
```

With no `-o`, the data goes to standard output so it can be piped:

```bash
docx-comments export report.docx --json | jq '.[] | select(.resolved == false) | .author'
```

If you give `-o` a filename, the format is inferred from the extension, so `--csv` / `--json` are optional:

```bash
docx-comments export report.docx -o comments.csv     # CSV, inferred
```

### `report` — a shareable review report

```bash
docx-comments report contract.docx                       # writes contract_report.html
docx-comments report contract.docx -o review.html
docx-comments report contract.docx -o review.md          # Markdown, inferred
docx-comments report contract.docx --markdown            # Markdown, explicit
```

With no `-o` it writes `<name>_report.html` next to the document. The format follows the extension you give, so `--html` / `--markdown` are usually unnecessary.

Filters work here too, which is how you produce a report of just the open items:

```bash
docx-comments report spec.docx --unresolved -o todo.html
docx-comments report spec.docx --author alice --title "Alice's notes" -o alice.html
```

| Flag | Meaning |
|---|---|
| `--output PATH`, `-o` | Where to write it. Defaults to `<name>_report.html` |
| `--html` / `--markdown` | Force the format instead of inferring it from the extension |
| `--title TEXT` | Heading for the report. Defaults to the file name |
| `--template PATH` | Your own Jinja2 template for the HTML report |

The HTML report needs the `[report]` extra. Without it the command prints the one-line install instruction and exits `1` rather than showing a traceback. Markdown always works.

### `batch` — a whole folder

```bash
docx-comments batch ./reviews
docx-comments batch ./reviews --recursive --threads 8
docx-comments batch ./reviews -o all_comments.csv
docx-comments batch ./reviews -o all_reviews.html      # one report for every file
```

Prints one row per file, then a total. Word's `~$name.docx` lock files are ignored. Unreadable files are listed at the end and the command exits `1`, but every readable file is still processed and exported.

`-o` accepts `.csv`, `.json`, `.html` and `.md`, and picks the writer from the extension.

### Filters

`--author`, `--contains`, `--resolved`, `--unresolved` and `--threads-only` work the same way on `parse`, `export` and `batch`:

| Flag | Meaning |
|---|---|
| `--author NAME`, `-a` | Author contains NAME, ignoring case |
| `--contains TEXT`, `-c` | TEXT appears in the comment or the text it points at |
| `--resolved` | Only resolved comments |
| `--unresolved` | Only open comments |
| `--threads-only` | Only comments that are part of a conversation |
| `--limit N`, `-n` | Show at most N comments (`parse`, `unresolved`) |

`--resolved` and `--unresolved` together is an error, since nothing could match.

### Exit codes

| Code | Meaning |
|---|---|
| `0` | Success |
| `1` | The file could not be read, or `unresolved` found open comments, or `batch` hit an unreadable file |
| `2` | The command line itself was wrong |

---

## Python API reference

```python
import docx_comment_parser as dcp
```

---

### `DocxParser`

Single-file parser. Non-copyable, movable. Can be reused across multiple calls to `parse()`.

#### `parse(file_path: str) -> None`

Parses a `.docx` file and populates all results. Replaces any previous results from an earlier call.

```python
parser = dcp.DocxParser()
parser.parse("report.docx")
```

Raises `DocxFileError` if the file cannot be opened or is not a valid ZIP archive.  
Raises `DocxFormatError` if the OOXML structure is malformed.  
Files without any comments parse successfully and return an empty list from `comments()`.

#### `comments() -> list[CommentMetadata]`

Returns all comments sorted ascending by `id`.

```python
for c in parser.comments():
    print(f"#{c.id:3d}  {c.author:20s}  {c.text[:60]}")
```

#### `find_by_id(id: int) -> CommentMetadata | None`

Looks up a single comment by its `w:id`. Returns `None` if not found.

```python
c = parser.find_by_id(3)
if c is not None:
    print(c.author, "—", c.text)
```

#### `by_author(author: str) -> list[CommentMetadata]`

Returns all comments whose `author` field exactly matches the given string (case-sensitive). The author string is taken directly from the `w:author` XML attribute.

```python
for c in parser.by_author("Alice"):
    status = "✓" if c.done else "○"
    print(f"  {status} [{c.date[:10]}] {c.text[:70]}")
```

#### `root_comments() -> list[CommentMetadata]`

Returns only the top-level (non-reply) comments in document order.

```python
for root in parser.root_comments():
    n = len(root.replies)
    print(f"Thread #{root.id}: {n} repl{'y' if n == 1 else 'ies'}")
```

#### `thread(root_id: int) -> list[CommentMetadata]`

Returns the full reply chain for a given root comment, starting with the root itself, in chronological order.

```python
for c in parser.thread(0):
    indent = "    " if c.is_reply else ""
    print(f"{indent}[{c.id}] {c.author}: {c.text}")
```

```
[0] Alice: This sentence needs rephrasing for clarity and conciseness.
    [1] Bob: Agreed. Suggest: "This sentence requires revision."
```

#### `stats() -> DocumentCommentStats`

Returns aggregate statistics computed during the last `parse()` call.

```python
s = parser.stats()
print(f"File      : {s.file_path}")
print(f"Comments  : {s.total_comments} total "
      f"({s.total_root_comments} root, {s.total_replies} replies)")
print(f"Resolved  : {s.total_resolved}")
print(f"Authors   : {', '.join(s.unique_authors)}")
print(f"Date range: {s.earliest_date[:10]} → {s.latest_date[:10]}")
```

```
File      : report.docx
Comments  : 3 total (2 root, 1 replies)
Resolved  : 1
Authors   : Alice, Bob
Date range: 2026-01-15 → 2026-01-17
```

#### Export methods

Added in v1.2. All of them operate on the currently parsed document. See [Exporting comments](#exporting-comments) for the full column list and examples.

| Method | Returns | Needs |
|---|---|---|
| `to_comments()` | `list[Comment]` | — |
| `to_dict()` | `list[dict]` | — |
| `to_json(indent=2)` | `str` | — |
| `export_json(path, indent=2)` | `Path` written | — |
| `export_csv(path, encoding="utf-8", delimiter=",")` | `Path` written | — |
| `to_dataframe()` | `pandas.DataFrame` | `[pandas]` extra |
| `to_polars()` | `polars.DataFrame` | `[polars]` extra |

```python
parser.parse("report.docx")
parser.to_dataframe()                 # a table
parser.export_csv("comments.csv")     # a spreadsheet
```

#### Report methods

Added in v1.3. See [Review reports](#review-reports) for what the reports contain.

| Method | Returns | Needs |
|---|---|---|
| `export_html_report(path, title=None, generated_at=None, template=None)` | `Path` written | `[report]` extra |
| `to_html_report(...)` | `str` | `[report]` extra |
| `export_markdown_report(path, title=None, generated_at=None, include_threads=True)` | `Path` written | — |
| `to_markdown_report(...)` | `str` | — |

```python
parser.parse("contract.docx")
parser.export_html_report("review.html")       # one shareable file
parser.export_markdown_report("review.md")     # text to paste anywhere
```

`BatchParser` has the same four methods. They combine every parsed file into one report and take an extra optional `file_paths` argument to restrict it.

---

### `BatchParser`

Processes many files in parallel using a thread pool. The Python GIL is released during `parse_all`, so CPU-bound threads are not blocked.

```python
bp = dcp.BatchParser(max_threads=0)   # 0 = one thread per CPU core
```

#### `parse_all(file_paths: list[str]) -> None`

Parses all files. Files that raise errors are captured in `errors()` rather than propagating as exceptions, so one bad file does not abort the batch.

#### `comments(file_path: str) -> list[CommentMetadata]`

Returns the parsed comments for a specific file.

#### `stats(file_path: str) -> DocumentCommentStats`

Returns statistics for a specific file.

#### `errors() -> dict[str, str]`

Returns `{file_path: error_message}` for every file that failed.

```python
for path, msg in bp.errors().items():
    print(f"FAILED {path}: {msg}")
```

#### `release(file_path: str) -> None`

Frees the in-memory results for one file. Call this as soon as you have finished processing a file to keep peak memory low when working with large batches.

#### `release_all() -> None`

Frees results for all files.

**Complete batch example:**

```python
import docx_comment_parser as dcp
import glob, json

files = glob.glob("/documents/**/*.docx", recursive=True)

bp = dcp.BatchParser(max_threads=0)
bp.parse_all(files)

summary = []
for path in files:
    if path in bp.errors():
        print(f"SKIP {path}: {bp.errors()[path]}")
        continue

    s = bp.stats(path)
    summary.append({
        "file":     path,
        "comments": s.total_comments,
        "authors":  s.unique_authors,
        "resolved": s.total_resolved,
    })
    bp.release(path)   # free this file's memory immediately

print(json.dumps(summary, indent=2))
```

#### `parsed_files() -> list[str]`

Added in v1.2. The files that parsed successfully and still hold results, sorted. Files that failed and files you have already `release()`d are not listed.

```python
bp.parse_all(["a.docx", "b.docx", "broken.docx"])
bp.parsed_files()      # ['a.docx', 'b.docx']
```

#### Export methods

Added in v1.2. Same methods as `DocxParser`, but they combine **every parsed file into one table**, with the `document_name` column identifying the source. Each takes an optional `file_paths` argument to restrict the export; the default is every successfully parsed file.

```python
bp.parse_all(glob.glob("reviews/*.docx"))

bp.to_dataframe()                          # all files, one table
bp.to_dataframe(file_paths=["a.docx"])     # just one
bp.export_csv("all_reviews.csv")
```

---

### `Comment` fields (export rows)

Added in v1.2. `Comment` is the flat, tabular version of `CommentMetadata` returned by `to_comments()` and used as the row type by every exporter. The full column table is in [Exporting comments](#exporting-comments).

The differences from `CommentMetadata` are deliberate, and they are what make it table-friendly:

| `CommentMetadata` | `Comment` | Why |
|---|---|---|
| `id` | `comment_id` | Unambiguous as a column heading |
| `parent_id == -1` | `parent_id is None` | A missing value, not a magic number |
| `done` | `resolved` | Says what it means |
| `date` (string only) | `date` **and** `date_parsed` | Keeps the original, adds a usable datetime |
| — | `thread_depth`, `root_id`, `reply_count` | Conversation position, computed for you |
| — | `document_name` | Which file the row came from |

```python
from docx_comment_parser import Comment, FIELD_NAMES

FIELD_NAMES        # the canonical column order, shared by every exporter
comment.to_dict()  # one row as a plain dict
```

---

### `CommentMetadata` fields

All fields are read-only. Available in both Python and C++.

| Field | Type | Description |
|---|---|---|
| `id` | `int` | `w:id` attribute. Unique within the document. |
| `author` | `str` | `w:author` — display name as set in Word. |
| `date` | `str` | `w:date` — ISO-8601 string exactly as stored in XML, e.g. `"2026-01-15T09:00:00Z"`. Not parsed into a date object. |
| `initials` | `str` | `w:initials` — author abbreviation shown in the comment balloon. |
| `text` | `str` | Full plain-text body of the comment. XML character entities are decoded: `&amp;` → `&`, `&lt;` → `<`, `&gt;` → `>`, `&quot;` → `"`, `&apos;` → `'`, numeric references → UTF-8. |
| `paragraph_style` | `str` | Style name of the first paragraph inside the comment (e.g. `"CommentText"`). Empty string if not set. |
| `referenced_text` | `str` | The document text that the comment is anchored to, extracted from the `commentRangeStart` / `commentRangeEnd` region in `word/document.xml`. Truncated to 240 bytes at a UTF-8 boundary. Empty if the range spans no text runs or the file has no `word/document.xml`. |
| `is_reply` | `bool` | `True` if this comment is a threaded reply. Requires `word/commentsExtended.xml` to be present. |
| `parent_id` | `int` | `id` of the parent comment. `-1` for root (non-reply) comments. |
| `replies` | `list[CommentRef]` | Direct child replies, populated on the parent comment. Empty on reply comments. |
| `thread_ids` | `list[int]` | Ordered list of all `id`s in the full reply chain. Populated only on root comments. Use `parser.thread(root_id)` to retrieve the full objects. |
| `done` | `bool` | `True` if the comment has been marked resolved in Word. Sourced from `commentsExtended.xml`. `False` when that file is absent. |
| `para_id` | `str` | OOXML 2016+ paragraph ID (`w14:paraId`). Used internally for thread resolution. |
| `para_id_parent` | `str` | Parent paragraph ID string before numeric `id` resolution. |
| `paragraph_index` | `int` | 0-based paragraph position in the document body. `-1` if not determined. |
| `run_index` | `int` | 0-based run position within the paragraph. `-1` if not determined. |

#### `CommentRef` fields (elements of `replies`)

| Field | Type | Description |
|---|---|---|
| `id` | `int` | `id` of the reply comment. |
| `author` | `str` | Author of the reply. |
| `date` | `str` | ISO-8601 date of the reply. |
| `text_snippet` | `str` | First 120 characters of the reply text. |

#### `to_dict()` — JSON serialisation

Both `CommentMetadata` and `DocumentCommentStats` expose a `to_dict()` method that returns all fields as a plain Python `dict`.

```python
import json

data = [c.to_dict() for c in parser.comments()]
print(json.dumps(data, indent=2, ensure_ascii=False))
```

---

### `DocumentCommentStats` fields

| Field | Type | Description |
|---|---|---|
| `file_path` | `str` | Path passed to `parse()`. |
| `total_comments` | `int` | Total comments including replies. |
| `total_root_comments` | `int` | Top-level (non-reply) comments. |
| `total_replies` | `int` | Reply comments. Equal to `total_comments - total_root_comments`. |
| `total_resolved` | `int` | Comments with `done=True`. |
| `unique_authors` | `list[str]` | Sorted list of distinct author names. |
| `earliest_date` | `str` | ISO-8601 date string of the oldest comment. |
| `latest_date` | `str` | ISO-8601 date string of the most recent comment. |

---

### Exceptions

| Exception | Inherits from | Raised when |
|---|---|---|
| `dcp.DocxFileError` | `DocxParserError`, `OSError` | File not found, permission denied, or not a valid ZIP archive. |
| `dcp.DocxFormatError` | `DocxParserError`, `ValueError` | Valid ZIP but required OOXML parts are missing or structurally invalid. |
| `dcp.DocxParserError` | `RuntimeError` | Base class — catches both of the above with a single handler. |

```python
try:
    parser.parse("report.docx")
except dcp.DocxFileError as e:
    print(f"Cannot open file: {e}")
except dcp.DocxFormatError as e:
    print(f"Not a valid .docx: {e}")
```

Each exception is catchable by its own type, by `DocxParserError`, and by the matching builtin — so all four of these work:

```python
except dcp.DocxFileError:   ...   # the specific error
except dcp.DocxParserError: ...   # anything this library raises
except OSError:             ...   # any file problem, from any library
except RuntimeError:        ...   # the broadest base
```

> **Fixed in v1.2.** Before v1.2 the specific types were unreachable: every failure arrived as `DocxParserError`, so `except dcp.DocxFileError` silently never matched. Code that catches `DocxParserError`, `OSError` or `ValueError` is unaffected and keeps working.

`BatchParser.parse_all()` never raises. Failures go into `errors()` instead:

```python
bp.parse_all(["good.docx", "corrupt.docx", "missing.docx"])
print(bp.errors())
# {'corrupt.docx': 'inflate failed...', 'missing.docx': 'Cannot open file...'}
```

---

## C++ API reference

Include the single public header:

```cpp
#include "docx_comment_parser.h"
```

Link against the shared library:

```cmake
target_link_libraries(my_app PRIVATE docx_comment_parser)
```

### `docx::DocxParser`

```cpp
docx::DocxParser parser;

// Parse a file — throws on error
parser.parse("report.docx");

// Iterate all comments (sorted by id)
for (const auto& c : parser.comments()) {
    std::cout << "[" << c.id << "] "
              << c.author << ": " << c.text << "\n";
}

// Look up by id — returns nullptr if not found
const docx::CommentMetadata* c = parser.find_by_id(2);
if (c) std::cout << c->text << "\n";

// Filter by author
for (const auto* c : parser.by_author("Alice"))
    std::cout << c->text << "\n";

// Top-level comments only
for (const auto* root : parser.root_comments())
    std::cout << root->id << " has " << root->replies.size() << " replies\n";

// Full reply thread
for (const auto* c : parser.thread(0)) {
    std::string indent = c->is_reply ? "  " : "";
    std::cout << indent << c->author << ": " << c->text << "\n";
}

// Aggregate statistics
const auto& s = parser.stats();
std::cout << s.total_comments << " comments by "
          << s.unique_authors.size() << " authors\n"
          << "Date range: " << s.earliest_date
          << " – "          << s.latest_date << "\n";
```

### `docx::BatchParser`

```cpp
// 0 = use std::thread::hardware_concurrency()
docx::BatchParser bp(/*max_threads=*/0);

bp.parse_all({"a.docx", "b.docx", "c.docx"});

// Check for failures
for (const auto& [path, msg] : bp.errors())
    std::cerr << "Failed: " << path << ": " << msg << "\n";

// Access results per file
for (const auto& c : bp.comments("a.docx"))
    std::cout << c.author << ": " << c.text << "\n";

std::cout << bp.stats("a.docx").total_comments << "\n";

// Free memory as you go
bp.release("a.docx");
bp.release_all();
```

### Exception hierarchy

```cpp
try {
    parser.parse("report.docx");
} catch (const docx::DocxFileError& e) {
    // file not found, not a ZIP
} catch (const docx::DocxFormatError& e) {
    // valid ZIP, bad OOXML
} catch (const docx::DocxParserError& e) {
    // base class — catches both
}
```

---

## Architecture

```
docx_comment_parser/
├── include/
│   ├── docx_comment_parser.h   ← public API (the only header consumers include)
│   ├── zip_reader.h            ← ZIP/DEFLATE reader interface
│   └── xml_parser.h            ← SAX + minimal DOM interface
├── src/
│   ├── docx_parser.cpp         ← orchestrates all four OOXML parts → CommentMetadata
│   ├── batch_parser.cpp        ← std::thread pool + result map
│   ├── zip_reader.cpp          ← memory-mapped ZIP + on-demand inflate
│   └── xml_parser.cpp          ← self-contained SAX + DOM, no libxml2
├── vendor/
│   └── zlib/
│       └── zlib.h              ← vendored DEFLATE + CRC-32 (used on MSVC only)
├── python/
│   └── python_bindings.cpp     ← pybind11 module (GIL released during batch)
├── src/docx_comment_parser/    ← pure-Python layer
│   ├── models.py               ← Comment dataclass (flat, tabular projection)
│   ├── exporters/              ← CSV, JSON, pandas, polars
│   ├── filters.py              ← shared filter predicates
│   ├── reporting/              ← v1.3 review reports
│   │   ├── analytics.py        ← aggregates shared by every report format
│   │   ├── charts.py           ← inline SVG columns, no chart library
│   │   ├── report_builder.py   ← Jinja2 → one self-contained HTML file
│   │   ├── markdown_report.py  ← stdlib-only Markdown digest
│   │   ├── templates/          ← report.html.j2
│   │   └── assets/             ← report.css, report.js (inlined at render time)
│   └── _cli_app.py             ← Typer + Rich command line
├── tests/
│   ├── CMakeLists.txt
│   └── test_docx_parser.cpp    ← 38 assertions, builds its own .docx in memory
├── CMakeLists.txt
└── setup.py
```

### Parse pipeline

```
.docx file (ZIP)
    │
    ▼
ZipReader — memory-mapped — inflate one entry at a time
    │
    ├──▶ word/comments.xml       → dom_parse()  → CommentMetadata[]
    │                                              id, author, date, initials, text
    │
    ├──▶ word/commentsExtended   → sax_parse()  → fill is_reply, done, para_id_parent
    │
    ├──▶ word/commentsIds.xml    → sax_parse()  → fill missing para_ids (fallback)
    │
    ├──▶ resolve_threads()       →               link parent_id, replies[], thread_ids[]
    │
    └──▶ word/document.xml       → sax_parse()  → fill referenced_text per comment
```

### Memory model

**ZIP extraction:** the file is memory-mapped (`mmap` / `MapViewOfFile`). Each ZIP entry is inflated into a temporary heap buffer, parsed, and the buffer is freed. No two entries' raw bytes are live at the same time.

**XML parsing:** `comments.xml` is parsed into a minimal DOM tree (always small — typically < 100 KB). The three other parts are streamed with SAX callbacks; only the data the callbacks accumulate is held in memory, not the raw XML text.

**BatchParser:** one `DocxParser` instance per worker thread. Results are stored in a `std::unordered_map` protected by a mutex. Calling `release(path)` immediately after consuming a file's results keeps peak memory proportional to `max_threads`, not to the total batch size.

### Zero external dependencies

| Capability | Implementation |
|---|---|
| ZIP parsing | Custom memory-mapped reader (no libzip, no minizip) |
| DEFLATE inflate | System zlib on Linux / macOS / MinGW; `vendor/zlib/zlib.h` on MSVC |
| XML parsing | Custom SAX + minimal DOM (no libxml2, no expat) |
| Threading | `std::thread` + `std::mutex` — C++17 standard library only |
| Python bindings | pybind11 — header-only, build-time dependency only |

---

## Performance

Parsing speed is the point of this library, so every release is measured against the last one to confirm the new features cost nothing.

### Parser throughput — v1.2.0 vs v1.3.0

Runs interleaved A/B/A/B so background load hits both versions equally; each figure is the best of three rounds of seven.

| Comments | v1.2.0 | v1.3.0 | Change |
|---|---|---|---|
| 100 | 1.346 ms | 1.352 ms | +0.4% |
| 1,000 | 13.647 ms | 13.608 ms | −0.3% |
| 10,000 | 136.412 ms | 144.988 ms | +6.3% |

Every figure here is noise, including the last one — and that can be shown rather than assumed. Both columns ran **the same compiled `_core` extension**: v1.3 changed no C++ at all. `parse()` is therefore byte-identical machine code in both runs, so its measured spread is by definition this machine's measurement error, which puts the noise floor at roughly ±6%. Nothing in the table exceeds it.

### Import cost

v1.3 adds a reporting layer, but importing the library does not load it:

| | v1.2.0 | v1.3.0 |
|---|---|---|
| `import docx_comment_parser` | 57.6 ms | 57.1 ms |

The reporting modules resolve on first use, the same way pandas already did. A program that only parses documents never pays for code it does not call — `import docx_comment_parser; "reporting" in sys.modules` is `False`, and a test asserts it stays that way.

### Report generation

| Comments | `parse()` | analytics | `export_html_report()` | `export_markdown_report()` | HTML size |
|---|---|---|---|---|---|
| 100 | 1.2 ms | 0.5 ms | 19.2 ms | 2.4 ms | 68 KB |
| 1,000 | 12.0 ms | 3.8 ms | 38.8 ms | 20.1 ms | 206 KB |
| 5,000 | 58.2 ms | 20.0 ms | 140.4 ms | 77.8 ms | 835 KB |
| 10,000 | 122.7 ms | 38.7 ms | 264.3 ms | 155.9 ms | 1.6 MB |

Report figures are end-to-end from a parsed document to a finished file. About 17 ms of the HTML column is fixed start-up cost (reading the template and starting Jinja2) which is why the small cases look disproportionate; beyond that, cost grows with the number of comments rather than faster.

A 5,000-comment report — the roadmap's stated target — takes **140 ms** and produces an **835 KB** file that still opens instantly. Two decisions keep it that size: comments are embedded once as compact JSON with author and document names de-duplicated, rather than as pre-rendered rows; and the charts are generated SVG rather than a ~200 KB bundled chart library. The page then renders one screen of results at a time, so the browser never lays out thousands of rows.

### Parser throughput — v1.1.2 vs v1.2.0

Same machine, same documents, runs interleaved so background load affects both equally. Each figure is the best median of five alternating rounds.

| Comments | v1.1.2 | v1.2.0 | Change |
|---|---|---|---|
| 100 | 1.204 ms | 1.166 ms | −3.2% |
| 1,000 | 11.593 ms | 11.594 ms | ±0.0% |
| 10,000 | 125.652 ms | 121.390 ms | −3.4% |

Roughly **80,000–86,000 comments per second**, unchanged. The differences are measurement noise, not real gains.

This is the expected result: the parser's C++ code was not touched apart from resetting a stats struct once per `parse()` call. The export layer is pure Python that runs only when you ask for it, so a program that never calls `to_dataframe()` pays nothing for its existence.

### Export throughput

Measured on the same documents, best of seven runs:

| Comments | `parse()` | `to_comments()` | `to_dataframe()` | `to_polars()` | `to_json()` | `export_csv()` |
|---|---|---|---|---|---|---|
| 100 | 1.3 ms | 1.0 ms | 4.3 ms | 1.8 ms | 1.9 ms | 2.7 ms |
| 1,000 | 10.7 ms | 10.1 ms | 17.7 ms | 13.7 ms | 19.4 ms | 22.6 ms |
| 10,000 | 120.6 ms | 117.4 ms | 161.0 ms | 147.4 ms | 209.6 ms | 228.5 ms |

Every export column includes the `to_comments()` conversion, so the numbers are end-to-end from a parsed document to the finished output.

A 10,000-comment DataFrame takes **161 ms**, comfortably inside the 1-second design budget, and cost grows linearly with the number of comments rather than faster. Memory stays proportional too: CSV writing streams row by row, so exporting a large document does not build the whole file in memory first.

These properties are asserted by the test suite, not just measured once — see the `perf` tests below.

---

## Testing

There are two suites: the original C++ one and a Python one added in v1.2. Together they run **323 checks**.

### Python suite

```bash
pip install "docx-comment-parser[dev]"
pytest                          # everything
pytest -m "not perf"            # skip the slower performance tests
pytest --cov=docx_comment_parser --cov-report=term-missing
```

257 tests, **98% statement coverage** — above the 90% project target.

Like the C++ suite, it invents its own fixtures: `tests/python/conftest.py` builds genuine `.docx` packages with `zipfile` and hands them to the real parser. Nothing is mocked, and no sample documents need to exist on disk.

| File | Covers |
|---|---|
| `test_core_regression.py` | That the v1.1 API still behaves identically — every class, method, field, `to_dict()` key and exception |
| `test_models.py` | Field mapping, date parsing, thread depth, malformed input |
| `test_exporters.py` | pandas, polars, JSON and CSV output, including dtypes, Unicode and empty documents |
| `test_filters.py` | Filtering rules |
| `test_cli.py` | Every command, flag, and exit code, through Typer's test runner |
| `test_reporting.py` | Analytics arithmetic, SVG charts, HTML and Markdown output, and the security properties below |
| `test_performance.py` | Scale and timing budgets (marked `perf`) |

Three of the reporting tests are worth calling out, because they check promises rather than behaviour:

- **It really is self-contained.** The generated HTML is scanned for any `src`/`href` pointing outside the file; the assertion is that there are none. It runs against both a 3-comment document and a 5,000-comment one.
- **Document content cannot break the page.** A comment whose text is `</script><script>…` is written into a report, and the test asserts the file still contains exactly the two script tags the template opened — the comment's own text is escaped into inert JSON, and survives intact when decoded.
- **The library stays lazy.** A subprocess imports the package and asserts that neither the reporting layer nor pandas appears in `sys.modules`.

The regression file is the important one: it exists specifically to prove that moving the compiled module into a package changed nothing a user can see. If it passes, upgrading is safe.

Type checking is enforced too:

```bash
mypy            # strict mode, clean
```

### C++ suite

The test suite creates a synthetic `.docx` file entirely in memory using a minimal ZIP builder and pre-compressed XML fixtures. No sample files need to be present on disk.

```bash
# Build and run via CTest
cmake -B build -DBUILD_TESTS=ON -DCMAKE_BUILD_TYPE=Debug
cmake --build build -j$(nproc)
ctest --test-dir build --output-on-failure

# Or run the binary directly for line-by-line output
./build/tests/test_docx_parser
```

Expected output:

```
Test fixture: /tmp/test_docx_parser_fixture.docx

=== test_basic_parsing ===

=== test_threading ===

=== test_done_flag ===

=== test_anchor_text ===

=== test_by_author ===

=== test_stats ===

=== test_root_comments ===

=== test_batch_parser ===

=== test_missing_file ===

=== test_encoding_utf8_bom ===

=== test_encoding_utf16le ===

=== test_encoding_utf16be ===

=== test_encoding_utf32le ===

=== test_encoding_windows1252 ===

=== test_encoding_iso8859_1 ===

=== test_encoding_numeric_entities ===

──────────────────────────────
Results: 66 passed, 0 failed
```

The test binary exits with code `0` on full pass, `1` on any failure.

---

## Changelog

### v1.3.0 — Shareable review reports

**Public API: backward compatible.** Existing code needs no changes; `test_core_regression.py` proves it, and the parser's C++ sources were not touched at all.

#### New — `export_html_report()`

- One self-contained HTML file: overview tiles, a per-reviewer table, per-day and per-week activity charts, an expandable thread explorer, and a filterable comment table.
- Client-side search and filters (author, status, keyword, date range) with no backend.
- **No external references of any kind** — it opens offline, and opening it sends nothing anywhere. Asserted by a test, at 3 comments and at 5,000.
- Charts are generated inline SVG rather than a bundled charting library, which keeps a 5,000-comment report at 835 KB instead of megabytes and makes it render instantly.
- A print stylesheet, so the browser's *Save as PDF* produces a clean document — which is why there is no PDF dependency.
- Custom Jinja2 templates via `template=`, receiving the same context as the built-in one.
- Available on `DocxParser` and `BatchParser`; the batch version merges every parsed file and adds a `Document` column.

#### New — `export_markdown_report()`

Same figures, plain text, **no extra required** — like CSV and JSON. Leads with open items, then the full transcript (`include_threads=False` for a summary only). Pastes into a pull request, a ticket, or an LLM prompt.

#### New — a public analytics layer

`build_report_data()` returns the aggregates both report formats share — `Overview`, `AuthorStat`, `TimelineBucket` and `Thread` — as plain frozen dataclasses, with no rendering and no dependencies. This is what stops the two formats from ever disagreeing about how many comments are open, and it is useful on its own.

A thread counts as resolved only when every comment in it is resolved: one open reply keeps the conversation open.

#### New — the `report` command

```bash
docx-comments report contract.docx -o review.html
docx-comments report spec.docx --unresolved -o todo.html
```

Format inferred from the extension, all the usual filters, plus `--title` and `--template`. `batch -o` now also accepts `.html` and `.md`.

#### Performance — no regression, and no new import cost

- Parser throughput unchanged: v1.3 ships the same compiled extension, so the A/B spread *is* the measurement noise floor (see [Performance](#performance)).
- The reporting layer is imported on first use, not at `import docx_comment_parser`. Import time is unchanged at ~57 ms, and a test asserts the modules stay out of `sys.modules`.

#### Packaging

- New `report` extra (`jinja2>=3.0`), added to `all` and `dev`. The base install still has **zero dependencies**.
- Templates and assets ship in the wheel and the sdist.
- 257 Python tests at 98% coverage, alongside the 66 C++ checks; `mypy --strict` still passes.

### v1.2.0 — Structured export and a command-line tool

**Public API: backward compatible.** Existing code needs no changes. The `test_core_regression.py` suite exists to prove it.

#### New — export comments as data

- `to_dataframe()` (pandas), `to_polars()` (polars), `to_dict()`, `to_json()`, `export_csv()`, `export_json()` and `to_comments()` on both `DocxParser` and `BatchParser`.
- A new `Comment` dataclass: the flat, one-row-per-comment view. Uses `__slots__`, so 10,000 comments stay cheap.
- Computed columns the parser did not previously expose: `thread_depth`, `root_id`, `reply_count`, `document_name`, and `date_parsed` (a real datetime alongside the untouched original string).
- `filter_comments()` for author / keyword / resolved / thread filtering, shared with the CLI.
- CSV export streams to disk; DataFrame export builds column-first, keeping a 10,000-comment export at ~161 ms.

#### New — the `docx-comments` command

- `parse`, `stats`, `export`, `unresolved` and `batch`, built with Typer and Rich.
- Filters on every relevant command: `--author`, `--contains`, `--resolved`, `--unresolved`, `--threads-only`, `--limit`.
- `unresolved` exits `1` when open comments remain, so it works as a CI gate.
- `export` writes to stdout by default, so it pipes into `jq`.

#### New — `BatchParser.parsed_files()`

Returns the sorted list of files that parsed successfully and still hold results. This is what lets the batch exporters work without being handed the paths again.

#### Fixed — `DocxFileError` and `DocxFormatError` were unreachable

`py::register_exception` was called with the base class last, and pybind11 tries translators in reverse registration order — so `DocxParserError` caught every derived type first. Every failure surfaced as `DocxParserError`, and `except dcp.DocxFileError` silently never matched, despite being documented.

The three types are now created with `PyErr_NewException` and a tuple of bases, and dispatched by a single translator with most-derived-first clauses. `DocxFileError` is now both a `DocxParserError` and an `OSError`; `DocxFormatError` is both a `DocxParserError` and a `ValueError`. Code catching any of the old types keeps working; catching the specific types now works too.

#### Fixed — stale statistics after parsing a comment-free document

`DocxParser::Impl::parse` returned early when a document had no `comments.xml`, or an empty one, before reaching `compute_stats()`. Re-using a parser therefore left the *previous* document's totals and `file_path` visible:

```python
parser.parse("has_comments.docx")
parser.parse("no_comments.docx")
parser.stats().file_path        # v1.1.2: "has_comments.docx"  ← wrong
                                # v1.2.0: "no_comments.docx"
```

Stats are now reset at the start of every `parse()`.

#### Packaging

- The compiled extension moved from the top level to `docx_comment_parser._core`, inside a new pure-Python package. `import docx_comment_parser as dcp` is unchanged.
- Optional extras: `[pandas]`, `[polars]`, `[cli]`, `[all]`, `[dev]`. The base install still has **zero dependencies**.
- Ships `py.typed` and a `_core.pyi` stub; `mypy --strict` passes.

#### Testing

- 188 Python tests at 97% coverage, alongside the existing 66 C++ checks.
- Parser throughput verified against v1.1.2 with interleaved A/B runs: no regression (see [Performance](#performance)).

### v1.1.2 — Added multiple enconding support

Included multiple text enconding support for a wide range of encondings. Updated unit tests for the new text enconding functionality.

#### `src/xml_parser.cpp` — Added a complete encoding transcoding layer before the XML parser:

`extract_xml_encoding_decl()` — scans the XML prolog for encoding="..."

`detect_encoding()` — BOM detection (UTF-8/16/32 LE/BE) takes precedence, falls back to the XML declaration

`utf16_to_utf8()` / `utf32_to_utf8()` — built-in converters (no platform dependency) with correct surrogate-pair handling

**Windows path:** `win_mbcs_to_utf8()` via `MultiByteToWideChar` + `WideCharToMultiByte`; maps 60+ encoding names to Windows codepage numbers (all Windows-125x, ISO-8859-1..16, Asian, Cyrillic, Thai, OEM codepages)

**Linux/macOS path:** `iconv_convert()` via `iconv(3)` with the same name alias table; handles `E2BIG`/`EILSEQ`/`EINVAL` gracefully
`transcode_to_utf8()` — public entry point, called at the start of `sax_parse()` so all parsing paths (DOM and SAX) go through it automatically

#### `include/xml_parser.h` — Exposed transcode_to_utf8() as a public API with full docstring.

#### `CMakeLists.txt` — Added `find_package(Iconv QUIET)` for non-Windows targets; links `Iconv::Iconv` only when it's a separate library (not built into libc).

#### `tests/test_docx_parser.cpp` — Added 7 encoding tests (66 total, all green):

`test_encoding_utf8_bom` — UTF-8 BOM is silently stripped

`test_encoding_utf16le` / `test_encoding_utf16be` — BOM-detected UTF-16

`test_encoding_utf32le` — BOM-detected UTF-32

`test_encoding_windows1252` — `encoding="windows-1252"` with ç, é, ä in content

`test_encoding_iso8859_1` — `encoding="ISO-8859-1"` with é, ñ

`test_encoding_numeric_entities` — `&#x4E2D;` (Chinese) and `&#233;` (é) references

### v1.1.0 — Inflate fix and zero-dependency MSVC support

**Public API:** unchanged. Existing code does not need modification.

#### `vendor/zlib/zlib.h` — two critical inflate bugs fixed

**Bug 1 — `huff_build`: out-of-bounds write in the Huffman symbol table.**

The original implementation used canonical code-start values as array indices into `syms[]`. For the RFC 1951 fixed literal tree, `next[9] = 400`, so all 112 nine-bit symbols (bytes 144–255, present in any real XML document) were written to `syms[400]`…`syms[511]` — well past the 288-element array. This caused silent heap corruption on every inflate call that decoded actual XML text. Synthetic test data with only ASCII symbols (code values < 144, all 8-bit) happened to stay in bounds by coincidence.

Fixed by filling `syms[]` cumulatively: for each bit-length `b` in ascending order, all symbols with `lens[i] == b` are appended in symbol-value order. This exactly matches how `huff_decode`'s `index` variable navigates the table.

**Bug 2 — `inflateInit2`: wiped the caller's I/O fields.**

`inflateInit2` called `memset(strm, 0, sizeof(*strm))`. The real zlib API contract — and the usage in `zip_reader.cpp` — requires the caller to set `next_in`, `avail_in`, `next_out`, and `avail_out` *before* calling `inflateInit2`. The `memset` zeroed all four, so every `inflate()` call received null pointers and zero lengths, returning `Z_DATA_ERROR (-3)` immediately on the first bit read.

Fixed by only zeroing the fields `inflateInit2` actually owns: `total_in`, `total_out`, `msg`, and `state`.

#### `src/xml_parser.cpp` — processing instruction terminator

The PI handler (`<?...?>`) scanned for the first bare `>`. A PI whose content contained `>` would terminate parsing prematurely. Fixed to scan for the correct `?>` closing sequence.

#### Windows MSVC — zero-dependency build

`vendor/zlib/zlib.h` is now a self-contained, header-only DEFLATE decompressor + CRC-32 implementing the exact zlib API surface used by the library. When compiled with MSVC (`#ifdef _MSC_VER`), `zip_reader.cpp` defines `VENDOR_ZLIB_IMPLEMENTATION` and includes this header instead of the system `<zlib.h>`. On all other platforms the system zlib is used as before.

The result: building the Python extension on Windows now requires only `pip install pybind11`. No vcpkg, no pre-installed zlib, no additional configuration.

---

## License

MIT — see `LICENSE` for the full text.

`vendor/zlib/zlib.h` is released under MIT-0 (no attribution required).
