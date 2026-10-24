"""Spending fewer tokens on the same review.

Context is what makes a record useful and also what makes it expensive: five
hundred characters either side of every anchor is most of a record's tokens, and
most of those characters are not about the comment.  This module removes what a
model does not need, cheapest and safest first, entirely locally and with no
dependency:

**Whitespace** (lossless).  Runs of spaces, tabs, non-breaking and zero-width
characters, and blank lines cost tokens and carry nothing.  Collapsing them
changes no word.

**Thread de-duplication** (lossless for a corpus).  Every record carries its
whole thread so a retrieval system can pull back one record and still see the
conversation.  When the corpus goes to a model whole, that is the same thread
once per reply; keeping it on the thread's root only is the single largest
saving on a heavily discussed review.

**Query-focused extraction** (lossy, bounded).  The context either side of an
anchor is split into sentences, each sentence is scored by how many of the
comment's and the anchor's content words it shares — weighted by how rare the
word is within the window — plus a bonus for being close to the anchor, and the
best-scoring sentences are kept, in document order, until the side's token
budget is spent.  The sentence next to the anchor is always kept.  Gaps are
marked with ``…`` so the model knows text was skipped rather than reading two
distant sentences as adjacent.  This is the extractive, selective-context family
of prompt compression, chosen over learned compressors (LLMLingua and kin)
because those need a local language model, which this library will not impose.

**Filler removal** (lossy, opt-in).  Articles, copulas and similar function
words are dropped from *context only* — never from a comment or its anchor, and
never a negation, a modal or a quantifier: "must not" and "all" carry the
meaning of a requirement, and a compressor that dropped them would invert it.

Levels bundle these: ``light`` is whitespace only; ``balanced`` adds extraction
to a 120-token budget per side; ``aggressive`` tightens that to 60, de-duplicates
threads, clips quoted replies and drops filler.  Every run reports the token
count before and after, measured with the same counter the chunker uses.
"""

from __future__ import annotations

import math
from functools import lru_cache
import re
from dataclasses import dataclass, replace
from typing import (
    TYPE_CHECKING,
    Any,
    Callable,
    Dict,
    FrozenSet,
    List,
    Mapping,
    Optional,
    Sequence,
    Set,
    Tuple,
    Union,
)

from .chunking import approximate_tokens, estimate_tokens
from .context import split_sentences

if TYPE_CHECKING:  # pragma: no cover - typing only
    from .dataset import LLMRecord

__all__ = [
    "CompressionConfig",
    "CompressionStats",
    "CompressionLike",
    "COMPRESSION_LEVELS",
    "resolve_compression",
    "normalize_text",
    "drop_filler",
    "compress_context",
    "compress_records",
]

TokenCounter = Callable[[str], int]

_ZERO_WIDTH = re.compile(r"[​-‍⁠﻿­]")
_INLINE_SPACE = re.compile(r"[ \t\f\v   -   　]+")
_LINE_BREAKS = re.compile(r"[ \t]*(?:\r\n|\r|\n)(?:[ \t]*(?:\r\n|\r|\n))*[ \t]*")
_SPACE_BEFORE_PUNCT = re.compile(r" +([,.;:!?)\]])")
_TERM = re.compile(r"\w+", re.UNICODE)

#: Function words dropped by ``drop_stopwords``. Deliberately excludes
#: negations (not, no, never, without), modals (must, shall, should, may, can,
#: will, would), quantifiers (all, any, every, each, only, some) and
#: conjunctions (and, or, but): each of those can reverse a requirement.
_FILLER = (
    "the an of in on at by for with from as into onto upon that which this "
    "these those is are was were be been being it its there very really just "
    "also then such quite rather simply"
).split()
_FILLER_PATTERN = re.compile(
    r"(?<![\w-])(?:" + "|".join(_FILLER) + r"|a)(?![\w-]) ?",
)
_FILLER_PATTERN_TITLE = re.compile(
    # Sentence-initial capitals ("The", "This"), but never a bare "A", which is
    # as likely to be a label ("Annex A", "Option A") as an article.
    r"(?<![\w-])(?:" + "|".join(word.capitalize() for word in _FILLER) + r")(?![\w-]) ?",
)

#: Words too common to make a sentence relevant just by sharing them.
_STOPWORDS = frozenset(
    _FILLER
    + """
    a and or but to not no do does did has have had will would can could should
    shall may might must we you they he she i me my our your their them his her
    what when where who why how if so than too please here there about over
    under more most some any all each other only own same such one two
    """.split()
)

_ELLIPSIS = "…"


@dataclass(frozen=True)
class CompressionConfig:
    """Which reductions to apply. See the module docstring for each one."""

    normalize_whitespace: bool = True
    max_context_tokens: Optional[int] = None
    """Token budget for *each side* of the context. ``None`` keeps it all."""

    relevance: bool = False
    """Choose which context sentences to keep by relevance to the comment,
    rather than simply keeping the ones nearest the anchor."""

    dedupe_threads: bool = False
    drop_stopwords: bool = False
    max_thread_entry_chars: Optional[int] = None
    """Clip each *other* comment quoted in a record's thread to this many
    characters. The record's own comment is never clipped."""

    name: str = "custom"

    def __post_init__(self) -> None:
        for label in ("max_context_tokens", "max_thread_entry_chars"):
            value = getattr(self, label)
            if value is not None and (isinstance(value, bool) or not isinstance(value, int) or value <= 0):
                raise ValueError(f"{label} must be a positive integer or None, got {value!r}")


#: Named levels, lightest first.
COMPRESSION_LEVELS: Mapping[str, CompressionConfig] = {
    "light": CompressionConfig(name="light"),
    "balanced": CompressionConfig(max_context_tokens=120, relevance=True, name="balanced"),
    "aggressive": CompressionConfig(
        max_context_tokens=60,
        relevance=True,
        dedupe_threads=True,
        drop_stopwords=True,
        max_thread_entry_chars=160,
        name="aggressive",
    ),
}

#: What every ``compress=`` parameter accepts.
CompressionLike = Union[None, bool, str, CompressionConfig]


def resolve_compression(value: CompressionLike) -> Optional[CompressionConfig]:
    """``None``/``False``/``"none"`` → no compression; ``True`` → balanced."""
    if value is None or value is False:
        return None
    if value is True:
        return COMPRESSION_LEVELS["balanced"]
    if isinstance(value, CompressionConfig):
        return value
    if isinstance(value, str):
        name = value.strip().lower()
        if name in {"none", "off", ""}:
            return None
        if name not in COMPRESSION_LEVELS:
            valid = ", ".join(["none", *COMPRESSION_LEVELS])
            raise ValueError(f"unknown compression level {value!r}; choose one of: {valid}")
        return COMPRESSION_LEVELS[name]
    raise TypeError(f"cannot interpret {value!r} as a compression setting")


@dataclass(frozen=True)
class CompressionStats:
    """What compression saved, measured with the package's token counter."""

    records: int
    tokens_before: int
    tokens_after: int
    level: str

    @property
    def saved(self) -> int:
        return self.tokens_before - self.tokens_after

    @property
    def ratio(self) -> float:
        """Tokens after as a share of tokens before; lower is smaller."""
        return self.tokens_after / self.tokens_before if self.tokens_before else 1.0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "records": self.records,
            "tokens_before": self.tokens_before,
            "tokens_after": self.tokens_after,
            "saved": self.saved,
            "ratio": round(self.ratio, 3),
            "level": self.level,
        }

    def summary(self) -> str:
        return (
            f"Compression ({self.level}): {self.tokens_before:,} -> "
            f"{self.tokens_after:,} tokens across {self.records} record(s), "
            f"{1 - self.ratio:.0%} saved"
        )


# ─────────────────────────────────────────────────────────────────────────────
#  Text-level operations
# ─────────────────────────────────────────────────────────────────────────────


def normalize_text(text: str) -> str:
    """Collapse whitespace runs and remove invisible characters. Changes no word."""
    if not text:
        return text
    cleaned = _ZERO_WIDTH.sub("", text)
    cleaned = _LINE_BREAKS.sub("\n", cleaned)
    cleaned = _INLINE_SPACE.sub(" ", cleaned)
    return cleaned.strip()


def drop_filler(text: str) -> str:
    """Remove filler function words, keeping negations, modals and quantifiers."""
    if not text:
        return text
    reduced = _FILLER_PATTERN.sub("", text)
    reduced = _FILLER_PATTERN_TITLE.sub("", reduced)
    reduced = _SPACE_BEFORE_PUNCT.sub(r"\1", reduced)
    return _INLINE_SPACE.sub(" ", reduced).strip()


@lru_cache(maxsize=65536)
def _stem(word: str) -> str:
    """A deliberately small suffix stripper, so "retained"/"retain" meet.

    Cached: a review's vocabulary is a few thousand words seen hundreds of
    thousands of times.
    """
    for suffix in ("ations", "ation", "ings", "ing", "edly", "ies", "ed", "es", "ly", "s"):
        if word.endswith(suffix) and len(word) - len(suffix) >= 4:
            return word[: -len(suffix)]
    return word


@lru_cache(maxsize=32768)
def _terms(text: str) -> FrozenSet[str]:
    """Content-word stems of ``text``. Cached because overlapping windows of
    neighbouring comments share most of their sentences."""
    return frozenset(
        _stem(word)
        for word in (match.group().casefold() for match in _TERM.finditer(text))
        if len(word) > 2 and word not in _STOPWORDS
    )


def _memoized(counter: TokenCounter) -> TokenCounter:
    """Wrap a token counter with a per-run memo.

    The same sentences and thread entries are counted over and over — every
    record carries its whole thread, and neighbouring windows overlap — and an
    exact tokenizer is the most expensive call in the module.  The memo lives
    for one :func:`compress_records` call, so memory is bounded by the export.
    """
    memo: Dict[str, int] = {}

    def count(text: str) -> int:
        cached = memo.get(text)
        if cached is None:
            cached = memo[text] = counter(text)
        return cached

    return count


def _clip_tokens(text: str, budget: int, keep_end: bool, counter: TokenCounter) -> str:
    """Cut ``text`` to roughly ``budget`` tokens on a word boundary.

    ``keep_end`` keeps the tail — the part of a *before* context that touches
    the anchor.  Starts from a character estimate and tightens, so a long text
    costs a handful of counter calls rather than one per word.
    """
    if counter(text) <= budget:
        return text
    words = text.split(" ")
    keep = max(1, min(len(words), int(budget * 0.75)))
    while keep > 1:
        piece = " ".join(words[-keep:] if keep_end else words[:keep])
        if counter(piece) <= budget:
            break
        keep = max(1, int(keep * 0.85))
    piece = " ".join(words[-keep:] if keep_end else words[:keep])
    return f"{_ELLIPSIS} {piece}" if keep_end else f"{piece} {_ELLIPSIS}"


def compress_context(
    text: str,
    *,
    side: str,
    query: str = "",
    max_tokens: Optional[int] = None,
    relevance: bool = True,
    token_counter: Optional[TokenCounter] = None,
) -> str:
    """Reduce one side of a context window to ``max_tokens``.

    ``side`` is ``"before"`` or ``"after"`` the anchor: it decides which end is
    nearest the comment, and therefore which sentence is always kept and where
    a truncation is marked.  ``query`` is the text the context should be
    relevant to — normally the comment plus its anchor.
    """
    if side not in {"before", "after"}:
        raise ValueError(f"side must be 'before' or 'after', got {side!r}")
    if not text or max_tokens is None:
        return text
    counter = token_counter or estimate_tokens
    if counter(text) <= max_tokens:
        return text

    sentences = [piece.strip() for piece in split_sentences(text)]
    sentences = [piece for piece in sentences if piece]
    if not sentences:
        return text
    count = len(sentences)
    nearest = count - 1 if side == "before" else 0

    costs = [counter(sentence) for sentence in sentences]
    if costs[nearest] >= max_tokens:
        clipped = _clip_tokens(sentences[nearest], max_tokens, side == "before", counter)
        return clipped

    distance = [abs(index - nearest) for index in range(count)]
    if relevance:
        wanted = _terms(query)
        per_sentence = [_terms(sentence) for sentence in sentences]
        frequency: Dict[str, int] = {}
        for terms in per_sentence:
            for term in terms:
                frequency[term] = frequency.get(term, 0) + 1
        scores = [
            sum(math.log(1 + count / frequency[term]) for term in terms & wanted)
            / math.sqrt(len(terms) + 1)
            + 0.5 / (1 + distance[index])
            for index, terms in enumerate(per_sentence)
        ]
    else:
        scores = [-float(d) for d in distance]

    kept: Set[int] = {nearest}
    used = costs[nearest]
    for index in sorted(range(count), key=lambda i: (-scores[i], distance[i])):
        if index in kept:
            continue
        if used + costs[index] + 1 <= max_tokens:
            kept.add(index)
            used += costs[index] + 1

    pieces: List[str] = []
    previous: Optional[int] = None
    for index in sorted(kept):
        if previous is None and index > 0:
            pieces.append(_ELLIPSIS)
        elif previous is not None and index > previous + 1:
            pieces.append(_ELLIPSIS)
        pieces.append(sentences[index])
        previous = index
    if previous is not None and previous < count - 1:
        pieces.append(_ELLIPSIS)
    return " ".join(pieces)


# ─────────────────────────────────────────────────────────────────────────────
#  Record-level operations
# ─────────────────────────────────────────────────────────────────────────────


def _record_tokens(record: "LLMRecord", counter: TokenCounter) -> int:
    """Tokens in a record's text fields — what compression can change."""
    total = 0
    for value in (
        record.context_before, record.referenced_text, record.context_after, record.comment,
    ):
        if value:
            total += counter(value)
    for entry in record.thread:
        if entry["comment_id"] != record.comment_id:
            total += counter(f"{entry['author']}: {entry['text']}")
    return total


def _clip_chars(text: str, limit: int) -> str:
    collapsed = " ".join(text.split())
    if len(collapsed) <= limit:
        return collapsed
    cut = collapsed.rfind(" ", 0, limit)
    return collapsed[: cut if cut > limit * 0.6 else limit].rstrip() + _ELLIPSIS


def _compress_one(
    record: "LLMRecord",
    config: CompressionConfig,
    counter: TokenCounter,
    keep_thread: bool,
) -> "LLMRecord":
    before, anchor, after, comment = (
        record.context_before, record.referenced_text, record.context_after, record.comment,
    )
    if config.normalize_whitespace:
        before, anchor, after, comment = (
            normalize_text(before), normalize_text(anchor),
            normalize_text(after), normalize_text(comment),
        )

    if config.max_context_tokens is not None:
        query = f"{comment} {anchor}"
        before = compress_context(
            before, side="before", query=query, max_tokens=config.max_context_tokens,
            relevance=config.relevance, token_counter=counter,
        )
        after = compress_context(
            after, side="after", query=query, max_tokens=config.max_context_tokens,
            relevance=config.relevance, token_counter=counter,
        )

    if config.drop_stopwords:
        before, after = drop_filler(before), drop_filler(after)

    thread: Tuple[Dict[str, Any], ...] = record.thread if keep_thread else ()
    if thread and (config.normalize_whitespace or config.max_thread_entry_chars):
        rewritten = []
        for entry in thread:
            text = str(entry["text"])
            if entry["comment_id"] == record.comment_id:
                text = comment
            else:
                if config.normalize_whitespace:
                    text = normalize_text(text)
                if config.max_thread_entry_chars:
                    text = _clip_chars(text, config.max_thread_entry_chars)
            rewritten.append({**entry, "text": text})
        thread = tuple(rewritten)

    return replace(
        record,
        context_before=before,
        referenced_text=anchor,
        context_after=after,
        comment=comment,
        thread=thread,
    )


def compress_records(
    records: Sequence["LLMRecord"],
    config: CompressionLike = "balanced",
    *,
    token_counter: Optional[TokenCounter] = None,
) -> Tuple[Tuple["LLMRecord", ...], Optional[CompressionStats]]:
    """Compress every record; return the new records and what it saved.

    The input records are not modified.  ``config`` may be a level name, a
    :class:`CompressionConfig`, ``True`` (balanced) or ``None`` (a no-op that
    returns ``(records, None)``).
    """
    resolved = resolve_compression(config)
    if resolved is None:
        return tuple(records), None

    # Two counters: the caller's tokenizer (or the exact one, when tiktoken is
    # installed) measures what was saved, while choosing sentences uses the
    # fast estimate, which errs high -- so what it lets through fits under an
    # exact count as well. A caller who passes token_counter gets it for both.
    measure = _memoized(token_counter or estimate_tokens)
    select = measure if token_counter is not None else _memoized(approximate_tokens)
    keep_thread_on: Optional[Set[int]] = None
    if resolved.dedupe_threads:
        # One carrier per thread: its root when present, else the first record
        # of it, so a thread whose root was filtered out still travels once.
        carriers: Dict[Tuple[str, int], int] = {}
        for index, record in enumerate(records):
            key = (record.document, record.thread_id)
            if key not in carriers or record.comment_id == record.thread_id:
                if key not in carriers or records[carriers[key]].comment_id != record.thread_id:
                    carriers[key] = index
        keep_thread_on = set(carriers.values())

    before_tokens = 0
    after_tokens = 0
    compressed: List["LLMRecord"] = []
    for index, record in enumerate(records):
        before_tokens += _record_tokens(record, measure)
        keep = keep_thread_on is None or index in keep_thread_on
        result = _compress_one(record, resolved, select, keep)
        after_tokens += _record_tokens(result, measure)
        compressed.append(result)

    stats = CompressionStats(
        records=len(compressed),
        tokens_before=before_tokens,
        tokens_after=after_tokens,
        level=resolved.name,
    )
    return tuple(compressed), stats
