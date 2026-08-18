# docx-comment-parser Roadmap (AI Agent Implementation Plan)

## Objective
Transform `docx-comment-parser` from a high-performance extraction library into a complete document review and comment intelligence platform.

Guiding principles:
- Preserve parser performance as the primary differentiator.
- Maintain backward compatibility.
- Keep core package lightweight.
- Place optional dependencies behind extras.
- Design all outputs to be LLM-ready.

---

# Release Strategy

## Phase 1 (v1.2)
1. Pandas/DataFrame Export
2. CLI Utility

## Phase 2 (v1.3)
3. HTML Review Reports

## Phase 3 (v1.4)
4. Document Comment Diff Engine

## Phase 4 (v1.5)
5. LLM Export & Review Intelligence

---

# Priority 1: DataFrame and Structured Export Layer

## Goals
Provide first-class interoperability with:
- Pandas
- Polars
- CSV workflows
- BI tools
- LLM pipelines

## New Module

```text
src/docx_comment_parser/exporters/
    dataframe.py
    json_exporter.py
    csv_exporter.py
```

## New APIs

```python
parser.to_dataframe()
```

```python
parser.to_polars()
```

```python
parser.to_dict()
```

```python
parser.to_json()
```

```python
parser.export_csv(path)
```

## Target Schema

```python
{
    "comment_id": int,
    "parent_id": int | None,
    "author": str,
    "initials": str,
    "date": datetime,
    "text": str,
    "referenced_text": str,
    "paragraph_style": str,
    "resolved": bool,
    "is_reply": bool,
    "thread_depth": int,
    "document_name": str
}
```

## Technical Tasks

### Add dataclass model

```python
@dataclass(slots=True)
class Comment:
    ...
```

### Type hints coverage

Requirement:

```text
mypy --strict passes
```

### Optional dependencies

```toml
[project.optional-dependencies]
pandas = ["pandas>=2.0"]
polars = ["polars>=1.0"]
```

## Acceptance Criteria

- Export 10,000+ comments without memory issues.
- DataFrame export under 1 second for medium datasets.
- JSON output directly serializable.

---

# Priority 2: Professional CLI

## Goals
Allow non-Python users to access parser capabilities.

## Command

```bash
docx-comments
```

## Technology

Use:

```python
Typer
```

Benefits:
- Auto-completion
- Help generation
- Type validation

## CLI Structure

```bash
docx-comments parse file.docx
```

```bash
docx-comments stats file.docx
```

```bash
docx-comments export file.docx --json
```

```bash
docx-comments export file.docx --csv
```

```bash
docx-comments unresolved file.docx
```

```bash
docx-comments batch ./documents
```

## Additional Features

### Author filtering

```bash
--author Alice
```

### Thread filtering

```bash
--threads-only
```

### Resolved filtering

```bash
--resolved
```

```bash
--unresolved
```

### Search

```bash
--contains security
```

## Rich Terminal Output

Use:

```python
rich
```

For:
- tables
- progress bars
- colored status indicators

## Acceptance Criteria

- Single executable install.
- Cross-platform support.
- Useful help for every command.

---

# Priority 3: Interactive HTML Review Reports

## Goals
Generate shareable review reports without requiring Python.

## API

```python
parser.export_html_report(
    output_path="report.html"
)
```

## Architecture

```text
reporting/
    templates/
    assets/
    report_builder.py
```

## Technology

Template engine:

```python
jinja2
```

## Report Sections

### Overview

- total comments
- resolved comments
- unresolved comments
- unique reviewers

### Author Analytics

- comments by reviewer
- resolution rates

### Timeline Visualization

Charts:

- comments per day
- comments per week

Use:

```javascript
Chart.js
```

### Thread Explorer

Interactive expand/collapse.

### Full Comment Table

Filters:

- author
- status
- keyword
- date

## Search UX

Requirements:

- client-side search
- no backend
- works offline

## Optional Outputs

```python
parser.export_markdown_report()
```

```python
parser.export_pdf_report()
```

## Acceptance Criteria

- Self-contained HTML.
- Opens without internet.
- Supports 5,000+ comments.

---

# Priority 4: Document Comment Diff Engine

## Goals
Compare review cycles.

Example:

```python
compare_comments(
    "review_v1.docx",
    "review_v2.docx"
)
```

## New Module

```text
comparison/
    matcher.py
    diff_engine.py
    similarity.py
```

## Core Use Cases

### Detect Added Comments

```python
result.added
```

### Detect Removed Comments

```python
result.removed
```

### Detect Newly Resolved

```python
result.resolved
```

### Detect Reopened

```python
result.reopened
```

## Matching Strategy

### Level 1

Exact ID matching.

### Level 2

Anchor matching.

Match on:

- referenced text
- author
- location

### Level 3

Fuzzy matching.

Use:

```python
rapidfuzz
```

Similarity threshold:

```python
0.85
```

## Outputs

```python
result.summary()
```

```python
result.to_dataframe()
```

```python
result.export_html()
```

## Enterprise Features

### Review Velocity

Measure:

- comments resolved per version
- time-to-resolution

### Risk Hotspots

Flag:

- persistent unresolved threads
- recurring review issues

## Acceptance Criteria

- High matching accuracy.
- Works across edited documents.

---

# Priority 5: LLM Export and Comment Intelligence

## Goals
Make the library the best DOCX review preprocessing layer for AI systems.

## New Module

```text
llm/
    exporters.py
    chunking.py
    prompts.py
```

## Primary API

```python
parser.to_llm_dataset()
```

## Output Structure

```python
{
    "comment_id": 5,
    "author": "Alice",
    "context_before": "...",
    "referenced_text": "...",
    "context_after": "...",
    "comment": "Needs clarification",
    "thread": [...]
}
```

## Context Windows

```python
parser.to_llm_dataset(
    chars_before=500,
    chars_after=500
)
```

## Prompt Builders

```python
create_review_summary_prompt()
```

```python
create_action_items_prompt()
```

```python
create_resolution_prompt()
```

## Classification Layer

Initial rule-based system.

Categories:

- Grammar
- Editorial
- Technical
- Design
- Compliance
- Legal
- Security
- Requirements

## Future AI Extensions

Provider adapters:

```python
OpenAI
Azure OpenAI
Anthropic
Gemini
```

Implemented as optional integrations.

## Vector Search Preparation

Export:

```python
parser.to_embeddings_input()
```

Each comment becomes a retrieval-ready document.

## Acceptance Criteria

- Zero AI dependencies in core package.
- Clean JSON schema.
- Compatible with RAG pipelines.

---

# Cross-Cutting Engineering Work

## Testing

Coverage target:

```text
>= 90%
```

Test categories:

- parser
- exporters
- CLI
- reporting
- diffing
- LLM exports

## Performance Benchmarking

Datasets:

- 100 comments
- 1,000 comments
- 10,000 comments

Track:

- memory
- parse time
- export time

## CI/CD

GitHub Actions:

```text
lint
unit tests
integration tests
benchmarks
build wheels
publish release
```

## Packaging

Build:

```text
Linux
Windows
macOS
```

Ship:

```text
x86_64
arm64
```

## Documentation

Required:

- quick start
- CLI guide
- DataFrame guide
- reporting guide
- diff guide
- LLM integration guide

---

# Success Metrics

## Adoption

- CLI becomes default entry point for non-developers.
- DataFrame export becomes primary integration method.

## Performance

- No measurable regression in parser throughput.
- Export operations remain near-linear complexity.

## Ecosystem Positioning

Target positioning:

"Fastest DOCX comment extraction and review intelligence toolkit for Python."
