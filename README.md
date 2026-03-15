# docx_comment_parser

A C++17 shared library that extracts every piece of comment metadata from `.docx` files — text, authors, dates, reply threads, anchor text, and resolution status — with full Python bindings via pybind11.

[![Tests](https://img.shields.io/badge/tests-38%20passing-brightgreen)](#testing)
[![C++17](https://img.shields.io/badge/C%2B%2B-17-blue)](#building-the-shared-library)
[![Python ≥ 3.8](https://img.shields.io/badge/python-%E2%89%A53.8-blue)](#python-quick-start)
[![License: MIT](https://img.shields.io/badge/license-MIT-green)](#license)

---

## Table of Contents

1. [What it does](#what-it-does)
2. [Quick start — Python](#quick-start--python)
3. [Quick start — C++](#quick-start--c)
4. [Installation](#installation)
5. [Python API reference](#python-api-reference)
6. [C++ API reference](#c-api-reference)
7. [Architecture](#architecture)
8. [Testing](#testing)
9. [Changelog](#changelog)
10. [License](#license)

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

| Exception | Python base | Raised when |
|---|---|---|
| `dcp.DocxFileError` | `IOError` | File not found, permission denied, or not a valid ZIP archive. |
| `dcp.DocxFormatError` | `ValueError` | Valid ZIP but required OOXML parts are missing or structurally invalid. |
| `dcp.DocxParserError` | `RuntimeError` | Base class — catches both of the above with a single handler. |

```python
try:
    parser.parse("report.docx")
except dcp.DocxFileError as e:
    print(f"Cannot open file: {e}")
except dcp.DocxFormatError as e:
    print(f"Not a valid .docx: {e}")
```

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

## Testing

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

──────────────────────────────
Results: 38 passed, 0 failed
```

The test binary exits with code `0` on full pass, `1` on any failure.

---

## Changelog

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
