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

How much context is a choice, made with :class:`ContextSpec`: a number of
characters, words, sentences or paragraphs on each side of the anchor, set
independently.  Characters are the cheapest and the bluntest; sentences and
paragraphs cost one extra pass over the document — made once and cached — and
never cut a thought in half.

What is deliberately *not* here: fidelity.  This is a reading of the document for
a language model, not a renderer.  Tables flatten to their cell text, drawings
and fields vanish, tracked deletions are excluded (a model should see the text as
it stands, not as it was), and paragraphs are separated by a blank line.
"""

from __future__ import annotations

import re
import zipfile
from bisect import bisect_right
from dataclasses import dataclass
from pathlib import Path
from typing import IO, TYPE_CHECKING, Any, Dict, List, Mapping, Optional, Tuple, Union

if TYPE_CHECKING:  # pragma: no cover - typing only
    from xml.etree.ElementTree import Element

__all__ = [
    "DocumentContext",
    "ContextWindow",
    "ContextCache",
    "ContextSpec",
    "CONTEXT_UNITS",
    "resolve_context",
    "split_sentences",
]

PathLike = Union[str, "Path"]

#: The units a context window can be measured in.
CONTEXT_UNITS: Tuple[str, ...] = ("chars", "words", "sentences", "paragraphs")

#: Unit spellings accepted besides the canonical ones.
_UNIT_ALIASES: Mapping[str, str] = {
    "char": "chars", "characters": "chars", "character": "chars",
    "word": "words", "sentence": "sentences", "paragraph": "paragraphs",
}

#: The default amount for each unit when only the unit is given — roughly the
#: same reading distance as the 500-character default.
DEFAULT_CONTEXT_AMOUNTS: Mapping[str, int] = {
    "chars": 500, "words": 80, "sentences": 2, "paragraphs": 1,
}

#: Ceiling on one side of a window measured in words, sentences or paragraphs.
#: A "sentence" in a document with no punctuation, or a "paragraph" that is a
#: pasted appendix, would otherwise become a whole-document context window.
DEFAULT_MAX_CONTEXT_CHARS = 4000

#: A run of sentence-ending punctuation, optionally closed by a quote or a
#: bracket, followed by whitespace. Where the next sentence begins is decided
#: in ``_sentence_starts``, which also rules out abbreviations.
_SENTENCE_END = re.compile(r"[.!?…]+[\"'”’)\]]*(?=\s)")

#: Words that end in a full stop without ending a sentence.
_ABBREVIATIONS = frozenset(
    """
    mr mrs ms dr prof sr jr st vs etc approx dept est fig figs no nos vol
    eg ie al cf incl inc ltd co corp art sec para ch pp p ref refs resp
    jan feb mar apr jun jul aug sep sept oct nov dec
    """.split()
)

_WORD = re.compile(r"\S+")


@dataclass(frozen=True)
class ContextSpec:
    """How much document text to read around each comment's anchor.

    ``before`` and ``after`` are counts of ``unit`` and are independent — two
    sentences before and none after is a valid request.  Sentence and paragraph
    counts *include* the one the anchor sits in: one sentence before means "the
    start of the anchor's own sentence", which is what a reader means by it.

    ``max_chars`` caps each side whatever the unit, so a paragraph that is
    really a pasted appendix cannot become a 40 KB context window.  ``None``
    applies :data:`DEFAULT_MAX_CONTEXT_CHARS` to the word, sentence and
    paragraph units and no extra cap to characters.
    """

    before: int = 500
    after: int = 500
    unit: str = "chars"
    max_chars: Optional[int] = None

    def __post_init__(self) -> None:
        unit = _UNIT_ALIASES.get(str(self.unit).strip().lower(), str(self.unit).strip().lower())
        if unit not in CONTEXT_UNITS:
            raise ValueError(
                f"context unit must be one of {', '.join(CONTEXT_UNITS)}; got {self.unit!r}"
            )
        object.__setattr__(self, "unit", unit)
        for label in ("before", "after"):
            value = getattr(self, label)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"context {label} must be a non-negative integer, got {value!r}")
        if self.max_chars is not None and (
            isinstance(self.max_chars, bool)
            or not isinstance(self.max_chars, int)
            or self.max_chars <= 0
        ):
            raise ValueError(f"max_chars must be a positive integer, got {self.max_chars!r}")

    @classmethod
    def of(cls, unit: str, before: Optional[int] = None, after: Optional[int] = None,
           max_chars: Optional[int] = None) -> "ContextSpec":
        """A spec for ``unit``, with that unit's default amount for any side not given."""
        probe = cls(0, 0, unit)  # normalises and validates the unit name
        default = DEFAULT_CONTEXT_AMOUNTS[probe.unit]
        return cls(
            default if before is None else before,
            default if after is None else after,
            probe.unit,
            max_chars,
        )

    @property
    def enabled(self) -> bool:
        """False when the spec asks for no context on either side."""
        return bool(self.before or self.after)

    @property
    def cap(self) -> Optional[int]:
        """The per-side character ceiling actually applied."""
        if self.max_chars is not None:
            return self.max_chars
        return None if self.unit == "chars" else DEFAULT_MAX_CONTEXT_CHARS

    def to_dict(self) -> Dict[str, Any]:
        return {
            "before": self.before, "after": self.after,
            "unit": self.unit, "max_chars": self.max_chars,
        }


#: What ``context=`` accepts: a spec, a mapping of its fields, or a unit name.
ContextLike = Union[ContextSpec, Mapping[str, Any], str]


def resolve_context(
    context: Optional[ContextLike],
    chars_before: int = 500,
    chars_after: int = 500,
) -> ContextSpec:
    """Turn any accepted ``context=`` value into a :class:`ContextSpec`.

    ``None`` falls back to the character widths, which is how every v1.5.0 call
    keeps meaning exactly what it meant.
    """
    if context is None:
        return ContextSpec(chars_before, chars_after, "chars")
    if isinstance(context, ContextSpec):
        return context
    if isinstance(context, str):
        return ContextSpec.of(context)
    if isinstance(context, Mapping):
        fields = dict(context)
        unknown = set(fields) - {"before", "after", "unit", "max_chars"}
        if unknown:
            raise ValueError(f"unknown context option(s): {', '.join(sorted(unknown))}")
        return ContextSpec.of(
            fields.get("unit", "chars"), fields.get("before"),
            fields.get("after"), fields.get("max_chars"),
        )
    raise TypeError(f"cannot interpret {context!r} as a context specification")


def _sentence_starts(text: str, paragraph_starts: Tuple[int, ...] = ()) -> List[int]:
    """Offsets at which a sentence begins, sorted, always starting with 0.

    Rule-based, in the spirit of the classifier: a terminator followed by
    whitespace ends a sentence unless the word before it is a known
    abbreviation or a single letter (an initial, or one half of ``e.g.``), or
    the next word starts in lower case.  Every paragraph start is a sentence
    start as well — a heading has no full stop and is still a unit of its own.
    """
    starts = set(paragraph_starts)
    starts.add(0)
    length = len(text)
    for match in _SENTENCE_END.finditer(text):
        if text[match.start()] == ".":
            # The word before the full stop, found by walking back -- a regex
            # search over a look-back slice cost more than the rest of the
            # split put together on a document with many short sentences.
            stop = match.start()
            while stop > 0 and not text[stop - 1].isalnum() and not text[stop - 1].isspace():
                stop -= 1
            first = stop
            while first > 0 and stop - first < 24 and text[first - 1].isalnum():
                first -= 1
            token = text[first:stop]
            if token and (
                (len(token) == 1 and token.isalpha()) or token.casefold() in _ABBREVIATIONS
            ):
                continue
        position = match.end()
        while position < length and text[position].isspace():
            position += 1
        if position >= length:
            continue
        if text[position].islower():
            continue
        starts.add(position)
    return sorted(starts)


def split_sentences(text: str) -> List[str]:
    """Split ``text`` into sentences, keeping every character.

    ``"".join(split_sentences(text)) == text`` always holds, which is what lets
    a compressor drop sentences and still quote the survivors exactly.
    """
    if not text:
        return []
    paragraph_starts = tuple(
        match.end() for match in re.finditer(r"\n\s*\n", text) if match.end() < len(text)
    )
    starts = _sentence_starts(text, paragraph_starts)
    bounds = starts + [len(text)]
    return [text[bounds[i]:bounds[i + 1]] for i in range(len(starts)) if bounds[i] < bounds[i + 1]]


def _clip_begin(text: str, begin: int, start: int, limit: int) -> int:
    """Move ``begin`` so ``text[begin:start]`` is at most ``limit`` characters,
    starting on a word boundary — the side nearest the anchor is what stays."""
    if start - begin <= limit:
        return begin
    clipped = start - limit
    space = text.find(" ", clipped, start)
    if space != -1 and space - clipped < limit - 1:
        clipped = space + 1
    return clipped


def _clip_finish(text: str, end: int, finish: int, limit: int) -> int:
    """Move ``finish`` so ``text[end:finish]`` is at most ``limit`` characters,
    ending on a word boundary."""
    if finish - end <= limit:
        return finish
    clipped = end + limit
    space = text.rfind(" ", end, clipped)
    if space - end > 0:
        clipped = space
    return clipped

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

    __slots__ = (
        "text", "_ranges", "_paragraphs", "source",
        "_sentences", "_paragraph_starts", "_paragraph_starts_raw",
    )

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
        # Built on first use by a sentence or paragraph window, then reused for
        # every other comment in the document.
        self._sentences: Optional[List[int]] = None
        self._paragraph_starts: Optional[List[int]] = None
        self._paragraph_starts_raw: Optional[List[int]] = None

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
        unit: str = "chars",
        max_chars: Optional[int] = None,
    ) -> ContextWindow:
        """Text around one comment's anchor, clipped to the requested widths.

        ``chars_before`` / ``chars_after`` are counts of ``unit`` — characters
        by default, or words, sentences or paragraphs.  See :class:`ContextSpec`
        for what each unit means; :meth:`window_for` takes one directly.

        ``whole_words`` trims a partial word off the outer edge of each side, so
        a window never opens mid-token — which reads badly in a prompt and, for
        an embedding, adds a fragment that means nothing.
        """
        if unit != "chars" or max_chars is not None:
            return self.window_for(
                comment_id, ContextSpec(chars_before, chars_after, unit, max_chars)
            )

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

    def window_for(self, comment_id: int, spec: ContextSpec) -> ContextWindow:
        """Text around one comment's anchor, measured as ``spec`` says."""
        bounds = self.bounds_for(comment_id, spec)
        if bounds is None:
            return ContextWindow("", "", "")
        begin, start, end, finish = bounds
        return ContextWindow(
            before=self.text[begin:start],
            anchor=self.text[start:end],
            after=self.text[end:finish],
        )

    def bounds_for(
        self, comment_id: int, spec: ContextSpec
    ) -> Optional[Tuple[int, int, int, int]]:
        """``(begin, start, end, finish)``: the window as offsets into :attr:`text`.

        ``text[begin:start]`` is the context before, ``text[start:end]`` the
        anchor and ``text[end:finish]`` the context after — exactly what
        :meth:`window_for` returns.  Offsets rather than strings are what let a
        redactor work on the document once and cut every window from the
        result, instead of re-scanning each overlapping window.
        """
        found = self._ranges.get(comment_id)
        if found is None:
            return None
        start, end = found
        text = self.text

        if spec.unit == "chars":
            begin, finish = self._char_bounds(start, end, spec.before, spec.after)
        else:
            if spec.unit == "words":
                begin, finish = self._word_bounds(start, end, spec.before, spec.after)
            else:
                if spec.unit == "sentences":
                    units = self._sentence_bounds()
                else:
                    units = self._paragraph_bounds()
                begin = _unit_start(text, units, start, spec.before)
                finish = _unit_end(text, units, end, spec.after)

        cap = spec.cap
        if cap is not None:
            begin = _clip_begin(text, begin, start, cap)
            finish = _clip_finish(text, end, finish, cap)
        if spec.unit != "chars":
            while begin < start and text[begin].isspace():
                begin += 1
            while finish > end and text[finish - 1].isspace():
                finish -= 1
        return begin, start, end, finish

    def _char_bounds(self, start: int, end: int, before: int, after: int) -> Tuple[int, int]:
        """The offsets :meth:`window` slices, with its whole-word trimming."""
        text = self.text
        begin = max(0, start - before) if before else start
        if before and start - before > 0 and begin < start:
            space = text.find(" ", begin, start)
            # Trim only when a space exists inside the slice; a slice with none
            # is one long token and clipping it to nothing would be worse.
            if space != -1 and space - begin < (start - begin) - 1:
                begin = space + 1
        while begin < start and text[begin] == "\n":
            begin += 1

        finish = min(len(text), end + after) if after else end
        if after and end + after < len(text) and finish > end:
            space = text.rfind(" ", end, finish)
            if space - end > 0:
                finish = space
        while finish > end and text[finish - 1] == "\n":
            finish -= 1
        return begin, finish

    def paragraph_ranges(self, begin: int, finish: int) -> List[Tuple[int, int, int]]:
        """``(index, start, end)`` of every paragraph overlapping ``[begin, finish)``."""
        paragraphs = self._paragraphs
        if not paragraphs or begin >= finish:
            return []
        starts = self._paragraph_index()
        first = max(0, bisect_right(starts, begin) - 1)
        found: List[Tuple[int, int, int]] = []
        for index in range(first, len(paragraphs)):
            p_start, p_end = paragraphs[index]
            if p_start >= finish:
                break
            if p_end > begin:
                found.append((index, p_start, p_end))
        return found

    def _paragraph_index(self) -> List[int]:
        """Starts of the recorded paragraphs, for bisecting; built once."""
        if self._paragraph_starts_raw is None:
            self._paragraph_starts_raw = [paragraph[0] for paragraph in self._paragraphs]
        return self._paragraph_starts_raw

    # ── Unit boundaries ─────────────────────────────────────────────────────

    def _sentence_bounds(self) -> List[int]:
        if self._sentences is None:
            self._sentences = _sentence_starts(
                self.text, tuple(self._paragraph_bounds())
            )
        return self._sentences

    def _paragraph_bounds(self) -> List[int]:
        if self._paragraph_starts is None:
            starts = [paragraph[0] for paragraph in self._paragraphs]
            if not starts or starts[0] != 0:
                starts.insert(0, 0)
            self._paragraph_starts = sorted(set(starts))
        return self._paragraph_starts

    def _word_bounds(self, start: int, end: int, before: int, after: int) -> Tuple[int, int]:
        """Offsets ``before`` words left of ``start`` and ``after`` words right of ``end``.

        Reads a slice sized for the request rather than the whole document, and
        widens it only when the slice turns out to hold too few words.
        """
        text = self.text
        begin = start
        if before:
            span = max(64, before * 12)
            while True:
                lower = max(0, start - span)
                words = [m.start() for m in _WORD.finditer(text, lower, start)]
                # The first word in a slice that does not reach the document
                # start may be a fragment; it only counts when the slice is
                # already the whole prefix.
                usable = words if lower == 0 else words[1:]
                if len(usable) >= before or lower == 0:
                    begin = usable[-before] if len(usable) >= before else (usable[0] if usable else start)
                    break
                span *= 2

        finish = end
        if after:
            span = max(64, after * 12)
            while True:
                upper = min(len(text), end + span)
                words = [m.end() for m in _WORD.finditer(text, end, upper)]
                usable = words if upper == len(text) else words[:-1]
                if len(usable) >= after or upper == len(text):
                    finish = usable[after - 1] if len(usable) >= after else (usable[-1] if usable else end)
                    break
                span *= 2
        return begin, finish


def _unit_start(text: str, starts: List[int], position: int, count: int) -> int:
    """Where ``count`` units (sentences or paragraphs) before ``position`` begin.

    The unit the position sits in counts as the first when any text of it lies
    before the position; when the position is exactly at a unit's start, the
    first counted unit is the previous one.
    """
    if count <= 0:
        return position
    index = bisect_right(starts, position) - 1
    partial = text[starts[index]:position]
    first = index if partial.strip() else index - 1
    return starts[max(0, first - (count - 1))] if first >= 0 else position


def _unit_end(text: str, starts: List[int], position: int, count: int) -> int:
    """Where ``count`` units after ``position`` end — the mirror of :func:`_unit_start`."""
    if count <= 0:
        return position
    index = bisect_right(starts, position) - 1
    following = starts[index + 1] if index + 1 < len(starts) else len(text)
    remainder = text[position:following]
    last = index + (count - 1) if remainder.strip() else index + count
    if last + 1 < len(starts):
        return starts[last + 1]
    return len(text)


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
