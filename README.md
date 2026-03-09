# docx_comment_parser

A fast, memory-efficient C++17 shared library (DLL/SO) that extracts **all comment metadata** from `.docx` files, with full Python bindings via pybind11.

## Features

| Feature | Details |
|---|---|
| Comment fields | id, author, date, initials, full text, paragraph style |
| Anchoring | referenced document text (via `commentRangeStart/End`) |
| Threading | parent/reply relationships (OOXML 2016+ `commentsExtended.xml`) |
| Resolution | `done` flag, earliest/latest dates, per-author filtering |
| Batch parsing | Thread-pool with configurable parallelism |
| Memory | ZIP entries inflated one-at-a-time; SAX for document body; no full DOM |
| Dependencies | libxml2, zlib (standard on all major platforms) |
| Python | pybind11 extension module, GIL released during batch parsing |

---

## Building

### Prerequisites

**Linux / macOS**
```bash
sudo apt install libxml2-dev zlib1g-dev   # Debian/Ubuntu
brew install libxml2 zlib                  # macOS
pip install pybind11 cmake
```

**Windows**
Install [vcpkg](https://github.com/microsoft/vcpkg) then:
```powershell
vcpkg install libxml2 zlib pybind11
```

### CMake (recommended)

```bash
cmake -B build -DCMAKE_BUILD_TYPE=Release
cmake --build build -j$(nproc)
# Optionally run tests:
cd build && ctest --output-on-failure
```

This produces:
- `build/libdocx_comment_parser.so` (Linux) / `.dylib` (macOS) / `.dll` (Windows)
- `build/_docx_comment_parser*.so` – Python extension

### pip (Python only)

```bash
pip install pybind11
pip install .
```

---

## Python Usage

```python
import docx_comment_parser as dcp

# ── Single file ──────────────────────────────────────────────────────────────
parser = dcp.DocxParser()
parser.parse("report.docx")

for c in parser.comments():
    print(f"[{c.id}] {c.author} ({c.date}): {c.text[:80]}")
    if c.referenced_text:
        print(f"  ↳ anchored to: '{c.referenced_text[:60]}'")
    if c.is_reply:
        print(f"  ↳ reply to comment #{c.parent_id}")

# Filter by author
for c in parser.by_author("Alice"):
    print(c.to_dict())

# Get full thread for a root comment
for c in parser.thread(0):
    indent = "  " if c.is_reply else ""
    print(f"{indent}[{c.id}] {c.author}: {c.text}")

# Stats
s = parser.stats()
print(f"Total: {s.total_comments}, Authors: {s.unique_authors}")
print(f"Date range: {s.earliest_date} → {s.latest_date}")

# ── Batch (parallel) ─────────────────────────────────────────────────────────
import glob

bp = dcp.BatchParser(max_threads=0)   # 0 = auto
files = glob.glob("/documents/**/*.docx", recursive=True)
bp.parse_all(files)

for f in files:
    if f in bp.errors():
        print(f"ERROR {f}: {bp.errors()[f]}")
        continue
    s = bp.stats(f)
    print(f"{f}: {s.total_comments} comments by {len(s.unique_authors)} authors")

bp.release_all()   # free memory
```

---

## C++ Usage

```cpp
#include "docx_comment_parser.h"

// Single file
docx::DocxParser parser;
parser.parse("report.docx");

for (const auto& c : parser.comments()) {
    std::cout << c.id << " | " << c.author << " | " << c.text << "\n";
}

// Batch
docx::BatchParser bp(/*threads=*/4);
bp.parse_all({"a.docx", "b.docx", "c.docx"});
for (const auto& [path, err] : bp.errors())
    std::cerr << "Failed: " << path << ": " << err << "\n";
bp.release_all();
```

---

## CommentMetadata fields

| Field | Type | Source |
|---|---|---|
| `id` | `int` | `w:id` attribute |
| `author` | `str` | `w:author` |
| `date` | `str` | `w:date` (ISO-8601) |
| `initials` | `str` | `w:initials` |
| `text` | `str` | Full plain-text of comment body |
| `paragraph_style` | `str` | Style of first paragraph in comment |
| `referenced_text` | `str` | Document text anchored by this comment |
| `is_reply` | `bool` | True if this is a threaded reply |
| `parent_id` | `int` | id of parent comment (-1 if root) |
| `replies` | `list[CommentRef]` | Direct replies (populated on parent) |
| `para_id` | `str` | OOXML 2016+ paragraph ID |
| `para_id_parent` | `str` | Parent paragraph ID (before id resolution) |
| `done` | `bool` | Resolved/done flag (`commentsExtended.xml`) |
| `thread_ids` | `list[int]` | Ordered ids in this thread (root only) |
| `paragraph_index` | `int` | 0-based paragraph in document body |
| `run_index` | `int` | 0-based run within paragraph |

---

## Architecture

```
docx_comment_parser/
├── include/
│   ├── docx_comment_parser.h   # Public API (CommentMetadata, DocxParser, BatchParser)
│   ├── zip_reader.h            # ZIP reader interface (zlib only, no libzip)
│   └── xml_utils.h             # Lightweight libxml2 helpers
├── src/
│   ├── zip_reader.cpp          # Memory-mapped ZIP + inflate
│   ├── docx_parser.cpp         # Core: comments.xml (DOM) + document.xml (SAX)
│   └── batch_parser.cpp        # Thread-pool batch processing
├── python/
│   └── python_bindings.cpp     # pybind11 module
├── tests/
│   └── test_docx_parser.cpp    # Self-contained test suite
├── CMakeLists.txt
└── setup.py
```

### Memory model

- **ZIP entries** are memory-mapped and inflated one at a time; no entry's data is kept in memory while another is being read.
- **`comments.xml`** is parsed with libxml2 DOM (typically < 100 KB).
- **`document.xml`** (which can be very large) is streamed with libxml2 SAX2; only the anchor text accumulator is kept in memory.
- **BatchParser** runs one `DocxParser` per thread; results can be individually `release()`d to reclaim memory after use.

---

## License

MIT
