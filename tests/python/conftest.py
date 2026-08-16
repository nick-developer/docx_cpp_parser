"""Shared fixtures: a real .docx builder.

The C++ suite builds its fixture ZIP by hand; here ``zipfile`` does the work, so
the generator can be parameterised.  Everything these tests exercise runs
against genuine OOXML packages written to disk and opened by the real parser —
there is no mocking of the C++ layer anywhere in this suite.
"""

from __future__ import annotations

import random
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Sequence
from xml.sax.saxutils import escape, quoteattr

import pytest

# ─────────────────────────────────────────────────────────────────────────────
#  Fixture specification
# ─────────────────────────────────────────────────────────────────────────────


@dataclass
class CommentSpec:
    """One comment to write into a generated document."""

    id: int
    author: str = "Alice Tester"
    date: str = "2024-03-01T10:00:00Z"
    initials: str = "AT"
    text: str = "A comment."
    parent: Optional[int] = None
    done: bool = False
    style: str = "CommentText"
    anchor: str = ""
    """Document text this comment is anchored to. Empty means no anchor."""


def _para_id(comment_id: int) -> str:
    """Deterministic 8-hex-digit paraId, as Word writes them."""
    return f"{comment_id + 1:08X}"


# ─────────────────────────────────────────────────────────────────────────────
#  OOXML part templates
# ─────────────────────────────────────────────────────────────────────────────

CONTENT_TYPES = """<?xml version="1.0" encoding="UTF-8"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
  <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
  <Default Extension="xml"  ContentType="application/xml"/>
  <Override PartName="/word/document.xml"
    ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>
  <Override PartName="/word/comments.xml"
    ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.comments+xml"/>
</Types>"""

ROOT_RELS = """<?xml version="1.0" encoding="UTF-8"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument"
    Target="word/document.xml"/>
</Relationships>"""

WORD_RELS = """<?xml version="1.0" encoding="UTF-8"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1"
    Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/comments"
    Target="comments.xml"/>
  <Relationship Id="rId2"
    Type="http://schemas.microsoft.com/office/2011/relationships/commentsExtended"
    Target="commentsExtended.xml"/>
</Relationships>"""


def _comments_xml(specs: Sequence[CommentSpec]) -> str:
    parts = [
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>',
        '<w:comments xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"'
        ' xmlns:w14="http://schemas.microsoft.com/office/word/2010/wordml">',
    ]
    for spec in specs:
        style = (
            f"<w:pPr><w:pStyle w:val={quoteattr(spec.style)}/></w:pPr>"
            if spec.style
            else ""
        )
        parts.append(
            f'  <w:comment w:id="{spec.id}" w:author={quoteattr(spec.author)}'
            f" w:date={quoteattr(spec.date)} w:initials={quoteattr(spec.initials)}>"
            f'<w:p w14:paraId="{_para_id(spec.id)}">{style}'
            f"<w:r><w:t>{escape(spec.text)}</w:t></w:r></w:p></w:comment>"
        )
    parts.append("</w:comments>")
    return "\n".join(parts)


def _comments_extended_xml(specs: Sequence[CommentSpec]) -> str:
    parts = [
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>',
        '<w16cex:commentsEx xmlns:w16cex="http://schemas.microsoft.com/office/word/2018/wordml/cex">',
    ]
    for spec in specs:
        parent = (
            f' w16cex:paraIdParent="{_para_id(spec.parent)}"'
            if spec.parent is not None
            else ""
        )
        parts.append(
            f'  <w16cex:commentEx w16cex:paraId="{_para_id(spec.id)}"{parent}'
            f' w16cex:done="{1 if spec.done else 0}"/>'
        )
    parts.append("</w16cex:commentsEx>")
    return "\n".join(parts)


def _document_xml(specs: Sequence[CommentSpec], body_text: str) -> str:
    parts = [
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>',
        '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">',
        "  <w:body>",
    ]
    if body_text:
        parts.append(f"    <w:p><w:r><w:t>{escape(body_text)}</w:t></w:r></w:p>")

    for spec in specs:
        if not spec.anchor:
            continue
        parts.append(
            f'    <w:p><w:commentRangeStart w:id="{spec.id}"/>'
            f'<w:r><w:t xml:space="preserve">{escape(spec.anchor)}</w:t></w:r>'
            f'<w:commentRangeEnd w:id="{spec.id}"/>'
            f'<w:r><w:commentReference w:id="{spec.id}"/></w:r></w:p>'
        )

    parts.append("  </w:body>")
    parts.append("</w:document>")
    return "\n".join(parts)


# ─────────────────────────────────────────────────────────────────────────────
#  Builder
# ─────────────────────────────────────────────────────────────────────────────


def build_docx(
    path: Path,
    specs: Sequence[CommentSpec],
    body_text: str = "Document body.",
    include_extended: bool = True,
) -> Path:
    """Write a valid .docx containing ``specs`` and return its path.

    Parameters
    ----------
    include_extended:
        Set False to omit commentsExtended.xml, which is how documents from
        pre-2016 Word (and from many non-Microsoft writers) look: no threading
        and no resolved flags.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", CONTENT_TYPES)
        archive.writestr("_rels/.rels", ROOT_RELS)
        archive.writestr("word/_rels/document.xml.rels", WORD_RELS)
        archive.writestr("word/document.xml", _document_xml(specs, body_text))
        archive.writestr("word/comments.xml", _comments_xml(specs))
        if include_extended:
            archive.writestr(
                "word/commentsExtended.xml", _comments_extended_xml(specs)
            )
    return path


def make_specs(
    count: int,
    authors: Sequence[str] = ("Alice Tester", "Bob Reviewer", "Carol Editor"),
    reply_ratio: float = 0.4,
    resolved_ratio: float = 0.3,
    seed: int = 1234,
) -> List[CommentSpec]:
    """Generate ``count`` comment specs with a realistic mix of threads.

    Deterministic for a given seed, so performance numbers and assertions are
    reproducible run to run.
    """
    rng = random.Random(seed)
    specs: List[CommentSpec] = []
    roots: List[int] = []

    for index in range(count):
        parent: Optional[int] = None
        if roots and rng.random() < reply_ratio:
            parent = rng.choice(roots)

        day = index % 28 + 1
        specs.append(
            CommentSpec(
                id=index,
                author=authors[index % len(authors)],
                date=f"2024-03-{day:02d}T{index % 24:02d}:15:00Z",
                initials="".join(w[0] for w in authors[index % len(authors)].split()),
                text=f"Comment number {index} about the security of this section.",
                parent=parent,
                done=rng.random() < resolved_ratio,
                anchor=f"Anchored text for comment {index}.",
            )
        )
        if parent is None:
            roots.append(index)

    return specs


# ─────────────────────────────────────────────────────────────────────────────
#  Fixtures
# ─────────────────────────────────────────────────────────────────────────────

#: The canonical 3-comment document: one root, two replies, one of them resolved.
#: Mirrors the fixture used by the C++ suite so both suites assert on the same data.
BASIC_SPECS: List[CommentSpec] = [
    CommentSpec(
        id=0,
        author="Alice Tester",
        date="2024-03-01T10:00:00Z",
        initials="AT",
        text="This sentence needs revision.",
        style="CommentText",
        anchor="The quick brown fox jumps over the lazy dog.",
    ),
    CommentSpec(
        id=1,
        author="Bob Reviewer",
        date="2024-03-02T14:30:00Z",
        initials="BR",
        text="Agreed, let me fix this.",
        parent=0,
        style="",
    ),
    CommentSpec(
        id=2,
        author="Alice Tester",
        date="2024-03-03T09:00:00Z",
        initials="AT",
        text="Thanks for the quick fix!",
        parent=0,
        done=True,
        style="",
    ),
]


@pytest.fixture(scope="session")
def basic_docx(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A 3-comment document with one thread and one resolved reply."""
    directory = tmp_path_factory.mktemp("basic")
    return build_docx(directory / "basic.docx", BASIC_SPECS)


@pytest.fixture(scope="session")
def empty_docx(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A valid document containing no comments at all."""
    directory = tmp_path_factory.mktemp("empty")
    return build_docx(directory / "empty.docx", [])


@pytest.fixture(scope="session")
def legacy_docx(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A document with no commentsExtended.xml — no threading, no done flags."""
    directory = tmp_path_factory.mktemp("legacy")
    specs = [
        CommentSpec(id=0, author="Old Word", text="Legacy comment.", anchor="Body."),
        CommentSpec(id=1, author="Old Word", text="Another legacy comment."),
    ]
    return build_docx(directory / "legacy.docx", specs, include_extended=False)


@pytest.fixture(scope="session")
def unicode_docx(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Non-ASCII authors and text, including characters outside the BMP."""
    directory = tmp_path_factory.mktemp("unicode")
    specs = [
        CommentSpec(
            id=0,
            author="Νίκος Ηλιόπουλος",
            initials="ΝΗ",
            text="Χρειάζεται διόρθωση — ελληνικά, ünlaut, 日本語, emoji 🚀.",
            anchor="Κείμενο αναφοράς",
        ),
        CommentSpec(
            id=1,
            author="Zoë Müller",
            initials="ZM",
            text='Quotes "and" <angle> & ampersands, comma, and\na newline.',
            parent=0,
        ),
    ]
    return build_docx(directory / "unicode.docx", specs)


@pytest.fixture(scope="session")
def batch_dir(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A directory of several documents plus one corrupt file."""
    directory = tmp_path_factory.mktemp("batch")
    for index in range(3):
        build_docx(
            directory / f"doc{index}.docx",
            make_specs(5 + index * 3, seed=index),
        )
    # A file that is not a ZIP at all — the batch parser must record it in
    # errors() rather than aborting the run.
    (directory / "corrupt.docx").write_bytes(b"this is not a zip archive")
    return directory


@pytest.fixture(scope="session")
def large_specs() -> List[CommentSpec]:
    """10,000 comment specs, per the roadmap's scale acceptance criterion."""
    return make_specs(10_000, seed=99)


@pytest.fixture(scope="session")
def large_docx(
    tmp_path_factory: pytest.TempPathFactory, large_specs: List[CommentSpec]
) -> Path:
    """A single document with 10,000 comments."""
    directory = tmp_path_factory.mktemp("large")
    return build_docx(directory / "large.docx", large_specs)
