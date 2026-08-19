"""The text around a comment, which is the part a model actually needs.

A comment on its own is usually unreadable.  "This is wrong" means nothing; "this
is wrong" attached to *the retention period shall be seven years*, in a paragraph
about data deletion, means something a model can act on.  The parser already
extracts the anchored text — the exact run a reviewer selected — but the
sentences on either side of it are what turn a fragment into a question with an
answer.

Those sentences are not in the comment parts of a .docx; they are in
``word/document.xml``, which the C++ core reads for anchors and then discards.
This module reads it again, in Python, on demand:

* only when a caller asks for context (``chars_before``/``chars_after`` > 0),
* once per document, cached, however many comments are exported,
* streaming, so a 40 MB document does not become a 40 MB DOM.

The result is one plain-text projection of the body plus a character range per
comment id, from which any window is a slice.  Everything is standard library —
``zipfile`` and ``xml.etree`` — so context costs no dependency.

What is deliberately *not* here: fidelity.  This is a reading of the document for
a language model, not a renderer.  Tables flatten to their cell text, drawings
and fields vanish, tracked deletions are excluded (a model should see the text as
it stands, not as it was), and paragraphs are separated by a blank line.
"""

from __future__ import annotations

import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import IO, TYPE_CHECKING, Dict, List, Optional, Tuple, Union

if TYPE_CHECKING:  # pragma: no cover - typing only
    from xml.etree.ElementTree import Element

__all__ = ["DocumentContext", "ContextWindow", "ContextCache"]

PathLike = Union[str, "Path"]

_W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
_BODY_PART = "word/document.xml"

#: Elements whose character data is not body text a reader would see.
#: ``instrText`` is a field code (``PAGEREF _Toc12345 \\h``) and ``delText`` is
#: text a tracked change has already removed.
_SKIPPED_TEXT_PARENTS = frozenset({f"{_W}instrText", f"{_W}delText"})

_PARAGRAPH_SEPARATOR = "\n\n"


@dataclass(frozen=True)
class ContextWindow:
    """The document text immediately around one comment's anchor."""

    before: str
    anchor: str
    after: str

    @property
    def is_empty(self) -> bool:
        return not (self.before or self.anchor or self.after)

    def joined(self) -> str:
        """The three parts as continuous text, for a prompt or an embedding."""
        return f"{self.before}{self.anchor}{self.after}".strip()


class DocumentContext:
    """The body text of one .docx, with a character range per comment.

    Build with :meth:`from_docx`.  Construction is the expensive part and it
    happens once; :meth:`window` is then a pair of slices.
    """

    __slots__ = ("text", "_ranges", "_paragraphs", "source")

    def __init__(
        self,
        text: str = "",
        ranges: Optional[Dict[int, Tuple[int, int]]] = None,
        paragraphs: Tuple[Tuple[int, int], ...] = (),
        source: str = "",
    ) -> None:
        self.text = text
        self._ranges: Dict[int, Tuple[int, int]] = ranges or {}
        self._paragraphs = paragraphs
        self.source = source

    # ── Construction ────────────────────────────────────────────────────────

    @classmethod
    def empty(cls) -> "DocumentContext":
        """A context that knows nothing — every window comes back blank.

        Returned instead of raising whenever the source document is missing or
        unreadable.  An LLM export built from a DataFrame, or from a file that
        has since been moved, must still produce records; it just produces them
        without surrounding text.
        """
        return cls()

    @classmethod
    def from_docx(cls, path: PathLike) -> "DocumentContext":
        """Read one .docx and project its body to text with comment ranges.

        Never raises for a missing, corrupt or comment-free file: those all
        yield :meth:`empty`.  Context is an enrichment, and a failure to enrich
        must not take down an export that would otherwise have succeeded.
        """
        target = Path(path)
        try:
            with zipfile.ZipFile(target) as archive:
                if _BODY_PART not in archive.namelist():
                    return cls.empty()
                with archive.open(_BODY_PART) as handle:
                    text, ranges, paragraphs = _read_body(handle)
        except (OSError, zipfile.BadZipFile, ValueError):
            return cls.empty()

        return cls(text=text, ranges=ranges, paragraphs=paragraphs, source=str(target))

    # ── Queries ─────────────────────────────────────────────────────────────

    def __bool__(self) -> bool:
        return bool(self.text)

    def __len__(self) -> int:
        return len(self._ranges)

    def anchor_range(self, comment_id: int) -> Optional[Tuple[int, int]]:
        """The ``[start, end)`` character range this comment is attached to."""
        return self._ranges.get(comment_id)

    def anchor_text(self, comment_id: int) -> str:
        """The exact document text the comment covers, read from the body.

        Differs from ``Comment.referenced_text`` in one useful way: the parser
        clips that to 240 characters, and this does not.
        """
        found = self._ranges.get(comment_id)
        if found is None:
            return ""
        return self.text[found[0]:found[1]]

    def paragraph_text(self, comment_id: int) -> str:
        """The whole paragraph the comment's anchor starts in."""
        found = self._ranges.get(comment_id)
        if found is None:
            return ""
        for start, end in self._paragraphs:
            if start <= found[0] < end:
                return self.text[start:end]
        return ""

    def window(
        self,
        comment_id: int,
        chars_before: int = 500,
        chars_after: int = 500,
        *,
        whole_words: bool = True,
    ) -> ContextWindow:
        """Text around one comment's anchor, clipped to the requested widths.

        ``whole_words`` trims a partial word off the outer edge of each side, so
        a window never opens mid-token — which reads badly in a prompt and, for
        an embedding, adds a fragment that means nothing.
        """
        found = self._ranges.get(comment_id)
        if found is None:
            return ContextWindow("", "", "")

        start, end = found
        before = self.text[max(0, start - chars_before):start] if chars_before else ""
        after = self.text[end:end + chars_after] if chars_after else ""

        if whole_words:
            if before and start - chars_before > 0:
                cut = before.find(" ")
                # Only trim when a space exists; a 500-character window with no
                # space in it is one long token and clipping it to nothing would
                # be worse than keeping the fragment.
                if 0 <= cut < len(before) - 1:
                    before = before[cut + 1:]
            if after and end + chars_after < len(self.text):
                cut = after.rfind(" ")
                if cut > 0:
                    after = after[:cut]

        return ContextWindow(
            before=before.lstrip("\n"),
            anchor=self.text[start:end],
            after=after.rstrip("\n"),
        )


# ─────────────────────────────────────────────────────────────────────────────
#  Streaming body reader
# ─────────────────────────────────────────────────────────────────────────────


def _read_body(
    handle: IO[bytes],
) -> Tuple[str, Dict[int, Tuple[int, int]], Tuple[Tuple[int, int], ...]]:
    """Stream ``document.xml`` into text, comment ranges and paragraph bounds.

    One pass, appending to a list of fragments and tracking the running length
    so a range can be recorded the moment its marker is seen.  ``iterparse``
    delivers events in document order, which is what makes the offsets correct:
    every ``w:t`` before a ``commentRangeStart`` has already been counted when
    that start is handled.
    """
    from xml.etree import ElementTree

    pieces: List[str] = []
    length = 0

    open_ranges: Dict[int, int] = {}
    ranges: Dict[int, Tuple[int, int]] = {}
    paragraphs: List[Tuple[int, int]] = []
    paragraph_start = 0
    skip_depth = 0

    # ``events=("start", "end")`` because a range marker is an empty element and
    # its position must be read at "start"; text is read at "end", when the
    # element's character data is complete.
    for event, element in ElementTree.iterparse(handle, events=("start", "end")):
        tag = element.tag

        if event == "start":
            if tag in _SKIPPED_TEXT_PARENTS:
                skip_depth += 1
            elif tag == f"{_W}p":
                paragraph_start = length
            elif tag == f"{_W}commentRangeStart":
                identifier = _attr_id(element)
                if identifier is not None:
                    open_ranges[identifier] = length
            elif tag == f"{_W}commentRangeEnd":
                identifier = _attr_id(element)
                if identifier is not None:
                    ranges[identifier] = (open_ranges.pop(identifier, length), length)
            elif tag == f"{_W}commentReference":
                identifier = _attr_id(element)
                # A comment with no range still has a reference mark. Record a
                # zero-width anchor at that point so the surrounding paragraph
                # is still available as context.
                if identifier is not None and identifier not in ranges:
                    ranges.setdefault(identifier, (length, length))
            continue

        # ── end events ──────────────────────────────────────────────────────
        if tag in _SKIPPED_TEXT_PARENTS:
            skip_depth -= 1
        elif tag == f"{_W}t":
            if not skip_depth and element.text:
                pieces.append(element.text)
                length += len(element.text)
        elif tag == f"{_W}tab":
            pieces.append("\t")
            length += 1
        elif tag in (f"{_W}br", f"{_W}cr"):
            pieces.append("\n")
            length += 1
        elif tag == f"{_W}p":
            if length > paragraph_start:
                paragraphs.append((paragraph_start, length))
            pieces.append(_PARAGRAPH_SEPARATOR)
            length += len(_PARAGRAPH_SEPARATOR)
            # Free the paragraph's children now that its text is captured. The
            # emptied element stays in the tree, which costs a few dozen bytes,
            # while its runs — the bulk of a Word document — are collected.
            element.clear()

    # A range whose end marker is missing (a truncated or hand-edited document)
    # would otherwise be silently dropped; close it at the end of the body.
    for identifier, start in open_ranges.items():
        ranges.setdefault(identifier, (start, length))

    return "".join(pieces), ranges, tuple(paragraphs)


def _attr_id(element: "Element") -> Optional[int]:
    """Read ``w:id`` off a marker element, or ``None`` when it is unusable."""
    raw = element.get(f"{_W}id")
    if raw is None:
        return None
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


# ─────────────────────────────────────────────────────────────────────────────
#  Cache
# ─────────────────────────────────────────────────────────────────────────────


class ContextCache:
    """One :class:`DocumentContext` per document path, built on first request.

    A batch export touches the same handful of documents thousands of times;
    without this, each comment would re-open and re-parse its .docx.  Bounded by
    the number of distinct documents in the export, which is small by
    construction.
    """

    __slots__ = ("_contexts", "_enabled")

    def __init__(self, enabled: bool = True) -> None:
        self._contexts: Dict[str, DocumentContext] = {}
        self._enabled = enabled

    def __bool__(self) -> bool:
        """False when this cache will never read a document."""
        return self._enabled

    def get(self, document_name: str) -> DocumentContext:
        if not self._enabled or not document_name:
            return DocumentContext.empty()
        cached = self._contexts.get(document_name)
        if cached is None:
            cached = DocumentContext.from_docx(document_name)
            self._contexts[document_name] = cached
        return cached
