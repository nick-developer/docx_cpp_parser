# docx_comment_parser

A C++17 shared library that extracts every piece of comment metadata from `.docx` files — text, authors, dates, reply threads, anchor text, and resolution status — with full Python bindings via pybind11.

Since v1.2 it also turns those comments into **spreadsheets, DataFrames and JSON**, and ships a **command-line tool** so you can use it without writing any Python.

v1.3 added **shareable review reports** — one self-contained HTML file with charts, search and thread-by-thread reading, that opens with no internet connection.

New in v1.4: **compare two review cycles.** Hand it the version you read last week and the one that came back today, and it tells you what is new, what got closed, and what came back open.

[![Tests](https://img.shields.io/badge/tests-463%20passing-brightgreen)](#testing)
[![C++17](https://img.shields.io/badge/C%2B%2B-17-blue)](#building-the-shared-library-with-cmake)
[![Python ≥ 3.9](https://img.shields.io/badge/python-%E2%89%A53.9-blue)](#quick-start--python)
[![License: MIT](https://img.shields.io/badge/license-MIT-green)](#license)

---

## Table of Contents

1. [What it does](#what-it-does)
2. [What's new in v1.4](#whats-new-in-v14)
3. [What's new in v1.3](#whats-new-in-v13)
4. [What's new in v1.2](#whats-new-in-v12)
5. [Quick start — Python](#quick-start--python)
6. [Quick start — command line](#quick-start--command-line)
7. [Quick start — C++](#quick-start--c)
8. [Installation](#installation)
9. [Exporting comments](#exporting-comments)
10. [Review reports](#review-reports)
11. [Comparing review cycles](#comparing-review-cycles)
12. [Command-line guide](#command-line-guide)
13. [Python API reference](#python-api-reference)
14. [C++ API reference](#c-api-reference)
15. [Architecture](#architecture)
16. [Performance](#performance)
17. [Testing](#testing)
18. [Changelog](#changelog)
19. [License](#license)

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

## What's new in v1.4

Everything from v1.3 still works exactly as before. v1.4 adds one thing: **it can tell you what changed since last time.**

Every previous version answered questions about *a* document. But a review is not one document — it is the same document coming back, again and again, and the question after the second round is never "what comments are in this file". It is "what happened".

```python
from docx_comment_parser import compare_comments

result = compare_comments("review_v1.docx", "review_v2.docx")
print(result.summary())
```

```
Comment diff — review_v1.docx → review_v2.docx

  Comments          42 → 47      (+5)
  Still open        18 → 15      (-3)
  Resolution       57% → 68%     (+11 pts)

  Added              5
  Removed            0
  Resolved           9
  Re-opened          1
  Edited             3
  Unchanged         30

  Net progress      +8 resolved   (converging)

  Matched by  38 id, 3 anchor, 1 fuzzy  (threshold 0.85, rapidfuzz)

  Risk        4 thread(s) still open
    #12 Alice: Clause 4.2 still needs legal sign-off. — re-opened after being resolved
```

The four lists the roadmap asked for are right there:

```python
result.added        # comments that are new
result.removed      # comments that are gone
result.resolved     # were open, now closed
result.reopened     # were closed, now open again
```

**The hard part is not the arithmetic — it is knowing which comment is which.** Word reuses comment ids, renumbers them when earlier comments are deleted, and rewrites paragraph ids on anything it touches. So the engine tries three things in order: the id (corroborated by a signal that survives editing), then the anchor (same person, same passage, or the same words in a new place), then text similarity. Nothing is paired on an id alone.

It also answers the two questions a review lead actually has:

```python
result.velocity     # is this converging, and how fast?
result.hotspots     # which threads should worry me?
```

And it renders, in every format the rest of the library already speaks:

```python
result.export_html("what_changed.html")   # one self-contained page
result.export_markdown("what_changed.md") # paste into a PR or a ticket
result.to_dataframe()                     # pandas
result.export_csv("changes.csv")
```

From a terminal:

```bash
docx-comments diff spec_v1.docx spec_v2.docx -o changed.html
```

**Nothing got heavier.** The base install still has zero dependencies: fuzzy matching uses `rapidfuzz` when you install it and the standard library's `difflib` when you do not, and both give the same answer. Importing the library does not load the comparison code at all. The parser is untouched and just as fast; see [Performance](#performance).

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
pip install docx-comment-parser              # parser + CSV/JSON/Markdown + diffing. Zero dependencies.
pip install "docx-comment-parser[pandas]"    # + to_dataframe()
pip install "docx-comment-parser[polars]"    # + to_polars()
pip install "docx-comment-parser[cli]"       # + the docx-comments command
pip install "docx-comment-parser[report]"    # + export_html_report()
pip install "docx-comment-parser[diff]"      # + faster compare_comments()
pip install "docx-comment-parser[all]"       # everything above
```

| Extra | Adds | Gives you |
|---|---|---|
| *(none)* | — | `DocxParser`, `BatchParser`, `export_csv()`, `export_json()`, `to_dict()`, `to_json()`, `export_markdown_report()`, `compare_comments()` |
| `pandas` | pandas ≥ 2.0 | `to_dataframe()` |
| `polars` | polars ≥ 1.0 | `to_polars()` |
| `cli` | typer, rich | the `docx-comments` terminal command |
| `report` | jinja2 ≥ 3.0 | `export_html_report()`, `DiffResult.export_html()` |
| `diff` | rapidfuzz ≥ 3.0 | roughly 3× faster fuzzy matching in `compare_comments()` |
| `all` | all of the above | everything |

The **Markdown** report deliberately needs no extra, exactly like CSV and JSON. Only the interactive HTML report needs `[report]`.

`[diff]` is the one extra that is purely about speed. `compare_comments()` works without it — it falls back to `difflib` from the standard library and reaches the same decisions, just slower on the comments that need fuzzy matching. `result.similarity_backend` tells you which engine ran.

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

## Comparing review cycles

Reports tell you where a review *is*. A diff tells you where it **went**.

### The one-liner

```python
from docx_comment_parser import compare_comments

result = compare_comments("review_v1.docx", "review_v2.docx")
print(result.summary())
```

Both arguments accept either a path or a list of comments you already have, so you can compare a filtered subset just as easily:

```python
from docx_comment_parser.filters import filter_comments

result = compare_comments(
    filter_comments(v1.to_comments(), author="alice"),
    filter_comments(v2.to_comments(), author="alice"),
)
```

### What you get

```python
result.added        # in the new version only
result.removed      # in the old version only
result.resolved     # was open, now marked done
result.reopened     # was done, now open again
result.edited       # matched, but the comment text changed
result.reanchored   # matched, but the document text it points at changed
result.unchanged    # matched, nothing moved
```

Each item is a `CommentChange` carrying both sides:

```python
for change in result.edited:
    print(f"#{change.comment_id} {change.author}")
    print(f"  was: {change.before.text}")
    print(f"  now: {change.after.text}")
```

**A change can be several things at once, and it is listed as all of them.** A comment that was reworded *and* resolved appears in `resolved` and in `edited`, because a diff that had to pick one label would be lying about the other. `change.kinds` is the full set; `change.kind` is the headline for a narrow column. `unchanged` is the one bucket that is exclusive — it means nothing moved at all.

### How comments are matched

This is the part that decides whether everything else is true. Word reuses `w:id` values, renumbers them when comments are deleted, and rewrites paragraph ids on anything it touches, so "same id" is a strong hint and not a fact. Three levels are tried in order, each only looking at what the level above could not account for:

| Level | Matches on | Catches |
|---|---|---|
| **1 — identity** | Same `comment_id`, plus a second signal: an unchanged timestamp, the same anchored text, or recognisably similar text by the same author | The ordinary case — a document that came back edited |
| **2 — anchor** | Same author *and* either the same words or the same anchored passage | A renumbered document, or a comment reworded in place |
| **3 — fuzzy** | Text similarity at or above the threshold | Everything else — a typo fix in a document that was also renumbered |

**An id match with no corroboration is rejected**, and this matters more than it sounds. If Word renumbered the document, id 5 in the new version may belong to a completely different person's comment. Pairing them would report one heavily edited comment instead of one addition and one removal, and every column downstream — velocity, hotspots, the lot — would be quietly wrong. Requiring a second signal costs one string comparison and removes the failure mode.

At level 3 the anchored text can only ever *raise* a score, never lower one. Revising a document rewrites the passages its comments point at — that is what a revision is — so weighting a changed anchor against a pair would reject exactly the comments this feature exists to find.

```python
result = compare_comments(a, b, threshold=0.9)   # demand closer text
result = compare_comments(a, b, fuzzy=False)     # ids and anchors only
```

`result.similarity_backend` records whether `rapidfuzz` or `difflib` did the scoring, and every change carries `change.level` and `change.score` so a surprising pairing can be traced.

### Review velocity

```python
v = result.velocity

v.net_resolved            # comments closed minus comments re-opened
v.is_converging           # closing faster than opening, and the backlog shrank
v.resolution_rate_delta   # change in the resolved share, -1.0 to 1.0
v.open_before, v.open_after
v.median_time_to_resolution
v.oldest_open_age
```

**A caveat that is stated wherever these appear.** OOXML records when a comment was *written*, and whether it is now marked done — but never *when* it was marked done. There is no resolution timestamp to read. So the timing figures measure a newly resolved comment's age against the newest activity in the later document: the closest thing the file format can support, and a lower bound on the real number.

### Risk hotspots

```python
for spot in result.hotspots[:5]:
    print(f"#{spot.root_id} {spot.author}: {spot.text}")
    print(f"   {spot.open_comments} open · {', '.join(spot.reasons)}")
```

```
#12 Alice: Clause 4.2 still needs legal sign-off.
   2 open · re-opened after being resolved, open across both versions
#7 Carol: The methodology section needs a rewrite.
   3 open · still attracting new comments, long-running
```

Only threads with something still open are ranked — a finished conversation, however contentious it was, is finished business. The weighting is explicit: a thread that came back open is the loudest signal a review can produce, one nobody has closed across two cycles is next, and size and age only break ties between threads that already qualify.

### Reports and exports

Every format the rest of the library speaks:

```python
result.summary()                          # plain text, for a terminal or a log
result.export_html("changed.html")        # one self-contained page   [report]
result.export_markdown("changed.md")      # paste into a PR or ticket
result.to_dataframe()                     # pandas                    [pandas]
result.to_polars()                        # polars                    [polars]
result.export_csv("changes.csv")
result.export_json("changes.json")
result.to_rows()                          # list of plain dicts
```

The HTML diff report is the same kind of file as the review report — no external references, opens offline, prints to PDF — with the headline numbers, a velocity table, the hotspot list, and every change searchable and filterable by kind and reviewer. Edited comments show the old and new text side by side.

The tabular exports put **before and after in separate columns** rather than one column plus a marker: a diff is read by comparing the two, and a shape that makes the reader pair up rows themselves is a worse table however compact it looks. Where a comment does not exist on one side, that side's `resolved_*` and `comment_id_*` columns are null, not `False` and `-1`.

```python
df = result.to_dataframe()
df[df["change"] == "reopened"][["comment_id_after", "author", "text_after"]]
```

### Reproducible output

Like the reports, a diff stamps the time it was made. Pass a fixed timestamp and the output is byte-for-byte identical:

```python
from datetime import datetime, timezone

result = compare_comments(a, b, generated_at=datetime(2026, 3, 1, tzinfo=timezone.utc))
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

### `diff` — what changed since last time

```bash
docx-comments diff spec_v1.docx spec_v2.docx
```

```
┌────── spec_v1.docx → spec_v2.docx ──────┐
│ New comments          5                 │
│ Removed               0                 │
│ Newly resolved        9                 │
│ Re-opened             1                 │
│ Edited                3                 │
│ Unchanged            30                 │
│                                         │
│ Comments        42 → 47   +5            │
│ Still open      18 → 15   -3            │
│ Resolution rate 57% → 68% +11 pts       │
└──────────────── converging ─────────────┘
```

Followed by the changes themselves and a risk-hotspot table. `-o` writes the whole comparison in whichever format the extension names:

```bash
docx-comments diff v1.docx v2.docx -o changed.html    # self-contained report
docx-comments diff v1.docx v2.docx -o changed.md      # needs no extra
docx-comments diff v1.docx v2.docx -o changes.csv
docx-comments diff v1.docx v2.docx -o changes.json
```

| Flag | Meaning |
|---|---|
| `--output PATH`, `-o` | Write the full comparison as `.html`, `.md`, `.csv` or `.json` |
| `--threshold F`, `-t` | Similarity a fuzzy match must reach, 0.0–1.0. Default `0.85` |
| `--no-fuzzy` | Match on ids and anchors only, never on text similarity |
| `--show N`, `-n` | Changes listed in the terminal. `0` for none. Default 10 |
| `--title TEXT` | Heading for the report |
| `--template PATH` | Your own Jinja2 template for the HTML report |
| `--fail-on-open` | Exit `1` if any comment is still unresolved |

`--fail-on-open` makes the command a release gate that reports progress rather than just refusing:

```bash
docx-comments diff last_signed_off.docx current.docx --fail-on-open \
  || echo "Still open items — see the table above"
```

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

`--resolved` and `--unresolved` together is an error, since nothing could match. `diff` takes no filters: a comparison is only meaningful over the whole of both documents.

### Exit codes

| Code | Meaning |
|---|---|
| `0` | Success |
| `1` | The file could not be read, or `unresolved` found open comments, or `batch` hit an unreadable file, or `diff --fail-on-open` found open comments |
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

### `compare_comments` and `DiffResult`

Added in v1.4. See [Comparing review cycles](#comparing-review-cycles) for what the comparison does and how comments are matched.

```python
from docx_comment_parser import compare_comments

result = compare_comments(before, after, threshold=0.85, fuzzy=True, generated_at=None)
```

| Argument | Type | Meaning |
|---|---|---|
| `before`, `after` | path or `list[Comment]` | The two versions. A path is parsed for you |
| `threshold` | float | Similarity a level 3 match must reach. Default `0.85` |
| `fuzzy` | bool | Set `False` to stop after level 2 |
| `generated_at` | datetime | Fix the timestamp for reproducible output |

#### `DiffResult` — the facet views

Every one returns a tuple of `CommentChange`, in reading order.

| Property | Contains |
|---|---|
| `added` | Comments present only in the newer document |
| `removed` | Comments present only in the older document |
| `resolved` | Open before, resolved after |
| `reopened` | Resolved before, open after |
| `edited` | Matched, comment text changed |
| `reanchored` | Matched, anchored document text changed |
| `unchanged` | Matched, nothing moved |
| `matched` | Every change with a comment on both sides |
| `changes` | All of the above, each comment exactly once |
| `of_kind(kind)` | Any single `ChangeKind` |
| `has_changes` | `False` when the two documents' comments are identical |

#### `DiffResult` — the rest

| Member | Type | What it is |
|---|---|---|
| `velocity` | `ReviewVelocity` | Counts, rates and timing for the cycle |
| `hotspots` | `tuple[Hotspot, ...]` | Threads still carrying risk, most pressing first |
| `before_name`, `after_name` | str | The two documents |
| `threshold` | float | The threshold this comparison used |
| `similarity_backend` | str | `rapidfuzz` or `difflib` |
| `generated_at` | datetime | When the comparison was made |

#### `DiffResult` — outputs

| Method | Returns | Needs |
|---|---|---|
| `summary()` | `str` | — |
| `to_rows()` | `list[dict]` | — |
| `to_dict()` | `dict` | — |
| `to_json(indent=2)` | `str` | — |
| `export_json(path, indent=2)` | `Path` written | — |
| `export_csv(path, encoding="utf-8", delimiter=",")` | `Path` written | — |
| `to_markdown(title=None, include_details=True)` | `str` | — |
| `export_markdown(path, ...)` | `Path` written | — |
| `to_dataframe()` | `pandas.DataFrame` | `[pandas]` |
| `to_polars()` | `polars.DataFrame` | `[polars]` |
| `to_html(title=None, template=None)` | `str` | `[report]` |
| `export_html(path, title=None, template=None)` | `Path` written | `[report]` |

#### `CommentChange` fields

| Field | Type | Description |
|---|---|---|
| `before` | `Comment` or `None` | The older version. `None` for an addition |
| `after` | `Comment` or `None` | The newer version. `None` for a removal |
| `kinds` | `tuple[ChangeKind, ...]` | Every facet that applies. Never empty |
| `kind` | `ChangeKind` | The headline facet, for a narrow column |
| `level` | `MatchLevel` or `None` | `id` / `anchor` / `fuzzy`. `None` when unmatched |
| `score` | float | Similarity of the two comment bodies, 0.0–1.0 |
| `comment` | `Comment` | The newer version where there is one, else the older |
| `author`, `comment_id`, `resolved` | | Shortcuts onto `comment` |
| `has(kind)` | bool | Whether a facet applies |

#### `ReviewVelocity` fields

| Field | Type | Description |
|---|---|---|
| `comments_before`, `comments_after` | int | Totals on each side |
| `added`, `removed`, `resolved`, `reopened`, `edited`, `unchanged` | int | Facet counts |
| `open_before`, `open_after` | int | Unresolved comments on each side |
| `resolution_rate_before`, `resolution_rate_after` | float | 0.0–1.0 |
| `net_resolved` | int | `resolved - reopened` — the cycle's real progress |
| `net_change` | int | Growth in the number of comments |
| `resolution_rate_delta` | float | Change in the resolved share |
| `is_converging` | bool | Closing faster than opening, and the backlog did not grow |
| `mean_time_to_resolution`, `median_time_to_resolution` | `timedelta` or `None` | Age of newly resolved comments (a lower bound — see above) |
| `mean_open_age`, `oldest_open_age` | `timedelta` or `None` | Age of what is still open |

#### `Hotspot` fields

| Field | Type | Description |
|---|---|---|
| `root_id`, `document` | int, str | Which thread, in which file |
| `author`, `text`, `referenced_text` | str | The root comment, clipped |
| `size`, `open_comments` | int | Comments in the thread, and how many are open |
| `persistent_open` | int | Comments open in *both* versions |
| `reopened`, `added` | int | Comments re-opened, and comments new this round |
| `age_days` | float or `None` | First comment to newest activity in the document |
| `reasons` | `tuple[str, ...]` | Why it is on the list |
| `score` | float | Ranking weight; comparable within one diff |

#### Lower-level access

The matching layer is public if you want the pairs without the classification:

```python
from docx_comment_parser.comparison import match_comments, MatchLevel

result = match_comments(before, after, threshold=0.85)
result.matches                       # (Match(before, after, level, score), ...)
result.unmatched_before              # nothing paired with these
result.by_level(MatchLevel.FUZZY)    # only the text-similarity pairs
```

```python
from docx_comment_parser.comparison.similarity import similarity, normalise, backend

backend()                                  # 'rapidfuzz' or 'difflib'
similarity(normalise(a), normalise(b))     # 0.0 – 1.0
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
│   ├── comparison/             ← v1.4 diff engine
│   │   ├── similarity.py       ← rapidfuzz when present, difflib otherwise
│   │   ├── matcher.py          ← three-level matching: id → anchor → fuzzy
│   │   ├── diff_engine.py      ← change facets, review velocity, risk hotspots
│   │   ├── diff_report.py      ← summary, rows, CSV/JSON/DataFrame, MD, HTML
│   │   ├── templates/          ← diff.html.j2
│   │   └── assets/             ← diff.css, diff.js (layered on report.css)
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

### Parser throughput — v1.3.0 vs v1.4.0

Runs interleaved A/B/A/B so background load hits both versions equally; each figure is the best of three rounds of seven.

| Comments | v1.3.0 | v1.4.0 | Change |
|---|---|---|---|
| 100 | 1.194 ms | 1.203 ms | +0.7% |
| 1,000 | 12.035 ms | 11.956 ms | −0.7% |
| 10,000 | 128.748 ms | 129.512 ms | +0.6% |

Both columns ran the **same compiled `_core` extension**: v1.4 changed no C++ at all, so `parse()` is byte-identical machine code in both runs and the spread above *is* this machine's measurement error. Nothing exceeds ±1%.

### Import cost

| | v1.3.0 | v1.4.0 |
|---|---|---|
| `import docx_comment_parser` | 62.7 ms | 62.9 ms |

The comparison layer resolves on first use, the same way the reporting layer and pandas already did — `import docx_comment_parser; "docx_comment_parser.comparison" in sys.modules` is `False`, and a test asserts it stays that way, along with `rapidfuzz` and even `difflib`.

### Diff throughput

Two versions of the same document: one comment in eleven deleted, one in three resolved, one in seven reworded, plus 5% new comments. Best of three, measured from parsed comments to the finished output.

| Comments | `parse()` | `compare_comments()` | `summary()` | `export_markdown()` | `export_html()` | HTML size |
|---|---|---|---|---|---|---|
| 100 | 1.3 ms | 1.2 ms | 0.1 ms | 0.3 ms | 20.3 ms | 68 KB |
| 1,000 | 11.8 ms | 11.2 ms | 0.2 ms | 1.8 ms | 30.7 ms | 298 KB |
| 5,000 | 64.2 ms | 105.9 ms | 1.0 ms | 3.7 ms | 74.5 ms | 1.3 MB |
| 10,000 | 130.7 ms | 238.7 ms | 1.9 ms | 4.7 ms | 129.1 ms | 2.6 MB |

**Comparing 10,000 comments costs about twice what parsing them does.** That ratio holds because the vast majority of comments match at level 1 or 2, which are dictionary lookups — the same cost per comment whether there are ten or ten thousand. Only the comments that match at neither reach level 3, and that stage is quadratic in what is left over, which is why the 5,000 and 10,000 rows grow slightly faster than the rows above them: the residue grows with the document, so its square grows faster still.

The case to watch is the pathological one: two large documents with nothing in common, where *every* comment falls through to level 3. There, candidates are blocked by shared words — consulted rarest first — and capped per comment, which turns the cross-product into a bounded scan. 1,500 comments against 1,500 with nothing in common takes **0.40 s** with rapidfuzz and **3.25 s** without, and correctly reports 1,500 additions and 1,500 removals. Without that blocking the same comparison takes over four minutes on the standard-library back-end.

### Diff throughput without `[diff]`

The same measurements with rapidfuzz not installed, so `difflib` from the standard library does the scoring:

| Comments | rapidfuzz | difflib | Ratio |
|---|---|---|---|
| 100 | 1.2 ms | 2.9 ms | 2.4× |
| 1,000 | 11.2 ms | 63.3 ms | 5.7× |
| 5,000 | 105.9 ms | 367.3 ms | 3.5× |
| 10,000 | 238.7 ms | 757.0 ms | 3.2× |

A 10,000-comment diff still finishes in under a second on a bare install with zero dependencies. `[diff]` is worth installing if you compare large documents often; it is not needed for the feature to be usable, which is why it is an extra rather than a dependency. **The two back-ends reach the same decisions** — the test suite runs the whole comparison suite twice, once with rapidfuzz forced off — though their scores differ by a percent or two, since they are different algorithms.

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

There are two suites: the original C++ one and a Python one added in v1.2. Together they run **463 checks**.

### Python suite

```bash
pip install "docx-comment-parser[dev]"
pytest                          # everything
pytest -m "not perf"            # skip the slower performance tests
pytest --cov=docx_comment_parser --cov-report=term-missing
```

397 tests, **98% statement coverage** — above the 90% project target.

Like the C++ suite, it invents its own fixtures: `tests/python/conftest.py` builds genuine `.docx` packages with `zipfile` and hands them to the real parser. Nothing is mocked, and no sample documents need to exist on disk.

| File | Covers |
|---|---|
| `test_core_regression.py` | That the v1.1 API still behaves identically — every class, method, field, `to_dict()` key and exception |
| `test_models.py` | Field mapping, date parsing, thread depth, malformed input |
| `test_exporters.py` | pandas, polars, JSON and CSV output, including dtypes, Unicode and empty documents |
| `test_filters.py` | Filtering rules |
| `test_cli.py` | Every command, flag, and exit code, through Typer's test runner |
| `test_reporting.py` | Analytics arithmetic, SVG charts, HTML and Markdown output, and the security properties below |
| `test_comparison.py` | Similarity, all three matching levels, change classification, velocity, hotspots, and every diff export |
| `test_performance.py` | Scale and timing budgets (marked `perf`) |

The comparison tests are built to defeat the matcher rather than to agree with it. Each level is exercised on a document constructed to be unmatchable by the level above it — renumbered ids to force anchor matching, reworded text *plus* renumbered ids to force fuzzy matching — and the case that matters most has the ids lining up while the comments behind them are strangers, where the required answer is one addition and one removal rather than one heavily edited comment. Two more are worth calling out:

- **A rewritten anchor must not block a match.** A comment whose wording survived a revision that rewrote the passage underneath it has to still be the same comment. This one caught a real bug during development, where a changed anchor dragged an otherwise certain pair below the threshold.
- **Both back-ends reach the same decisions.** The similarity tests run twice, once with rapidfuzz forced unavailable, and the whole suite is run a second time in CI conditions with it uninstalled.

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

### v1.4.0 — Comparing review cycles

**Public API: backward compatible.** Existing code needs no changes; `test_core_regression.py` proves it, and the parser's C++ sources were not touched at all.

#### New — `compare_comments()`

```python
from docx_comment_parser import compare_comments

result = compare_comments("review_v1.docx", "review_v2.docx")
result.added, result.removed, result.resolved, result.reopened
```

- Takes paths or lists of `Comment`, so a filtered subset compares as easily as a whole document.
- Changes are described as overlapping **facets**, not one label: a comment reworded *and* resolved appears in both lists, because a diff forced to pick one would misreport the other. `edited`, `reanchored` and `unchanged` complete the set.
- `DiffResult` carries both sides of every change, plus how it was matched (`level`) and how alike the two texts are (`score`).

#### New — three-level matching

Word reuses comment ids and renumbers them freely, so "same id" is a hint, not a fact. Matching runs id → anchor → fuzzy, each level looking only at what the previous one could not account for.

**An id match with no corroboration is rejected.** A second signal has to agree — an unchanged timestamp, an identical anchor, or recognisably similar text by the same author. Without that check, a renumbered document pairs comments with strangers and reports a heavily edited comment instead of an addition and a removal, silently corrupting every figure downstream.

At level 3 the anchored text can only ever *raise* a pair's score, never lower one: revising a document rewrites the passages its comments point at, so a changed anchor is the expected case rather than evidence against the pair.

#### New — review velocity and risk hotspots

`result.velocity` gives net progress, resolution-rate movement, and whether the review is converging. `result.hotspots` ranks the threads that still carry risk — re-opened first, then those nobody has closed across two cycles — each with the reasons it is on the list.

The timing figures are honest about their limit: OOXML records when a comment was written and whether it is now done, but never when it was marked done, so ages are measured against the newest activity in the later document and are a lower bound. That caveat is printed in every report that shows them.

#### New — every output format the library already speaks

`summary()`, `to_rows()`, `to_dict()`, `to_json()`, `export_csv()`, `export_json()`, `to_dataframe()`, `to_polars()`, `to_markdown()` / `export_markdown()`, and `to_html()` / `export_html()`.

The HTML diff report is the same kind of artefact as the v1.3 review report — self-contained, offline, printable to PDF — and inherits its design tokens rather than redefining them. Tabular exports keep before and after in separate columns, with nulls (not `False` and `-1`) where a comment does not exist on one side.

#### New — the `diff` command

```bash
docx-comments diff spec_v1.docx spec_v2.docx -o changed.html
docx-comments diff signed_off.docx current.docx --fail-on-open
```

Format inferred from the extension, plus `--threshold`, `--no-fuzzy`, `--show`, `--title`, `--template` and `--fail-on-open` for use as a release gate.

#### Packaging — still zero dependencies

- New `diff` extra (`rapidfuzz>=3.0`), added to `all` and `dev`. It is **purely a speed option**: without it the engine uses `difflib` and reaches the same decisions, taking 757 ms rather than 239 ms on a 10,000-comment comparison. `result.similarity_backend` records which ran.
- The comparison layer is imported on first use. Import time is unchanged at ~63 ms, and a test asserts that neither the layer, nor rapidfuzz, nor even `difflib` appears in `sys.modules` after `import docx_comment_parser`.
- Diff templates and assets ship in the wheel and the sdist.
- 397 Python tests at 98% coverage, alongside the 66 C++ checks; `mypy --strict` still passes.

#### Fixed — `mypy` could not run with rapidfuzz installed

rapidfuzz ships `.pyi` stubs, and `follow_imports = "skip"` does not apply to stub files — so mypy walked into rapidfuzz's stubs, then numpy's, and died on syntax that only exists from Python 3.12. Fixed with `follow_imports_for_stubs` on the third-party override, which also stops the same thing happening through pandas.

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
