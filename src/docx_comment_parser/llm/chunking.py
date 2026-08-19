"""Fitting a review into a context window without cutting a thread in half.

Every model has a budget, and a 10,000-comment review does not fit in one.  The
question is not *whether* to split it but *where*, and the answer is not "every
N records".  A thread split across two requests is worse than useless: the model
sees a reply with no comment above it in one call and a question with no answer
in the other, and confidently summarises both wrong.

So chunking here is bin-packing with one hard constraint — a thread stays whole
— and one soft one: chunks come out in document order, because a review reads in
document order and a summary that jumps around is harder to check.

Token counting uses ``tiktoken`` when it is installed and a calibrated heuristic
when it is not, in the same shape as the ``rapidfuzz``/``difflib`` split in the
comparison layer: the accurate back-end is an optional extra, the approximate one
is always there, and neither changes the *structure* of the answer.  The
heuristic deliberately over-counts slightly.  Under-counting produces a request
the provider rejects; over-counting produces one chunk more than strictly
necessary, which nobody notices.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import (
    TYPE_CHECKING,
    Any,
    Callable,
    Dict,
    Iterable,
    List,
    Optional,
    Sequence,
    Tuple,
)

if TYPE_CHECKING:  # pragma: no cover - typing only
    from .dataset import LLMRecord

__all__ = [
    "estimate_tokens",
    "token_backend",
    "Chunk",
    "chunk_records",
    "chunk_texts",
]

#: Average characters per token for English prose in the byte-pair vocabularies
#: every current provider uses. Measured between 3.6 and 4.2 on review text;
#: 3.6 is chosen because erring low here means erring *high* on token count.
_CHARS_PER_TOKEN = 3.6

#: Per-record overhead: the JSON punctuation, keys and separators a record costs
#: once serialised into a prompt, over and above its text.
_RECORD_OVERHEAD_TOKENS = 12

_encoder: Optional[Any] = None
_probed = False


def _tiktoken_encoder() -> Optional[Any]:
    """Return a ``tiktoken`` encoder if the package is installed, else ``None``.

    ``o200k_base`` is the vocabulary current frontier models use; if this
    tiktoken is older than that name, ``cl100k_base`` is close enough for a
    budgeting decision.  Resolved once and memoised — the probe is separate from
    the result so an absent package is not re-imported on every call.
    """
    global _encoder, _probed
    if _probed:
        return _encoder

    _probed = True
    try:
        import tiktoken
    except ImportError:
        _encoder = None
        return None

    for vocabulary in ("o200k_base", "cl100k_base"):
        try:
            _encoder = tiktoken.get_encoding(vocabulary)
            return _encoder
        except (ValueError, KeyError):  # pragma: no cover - depends on version
            continue
    _encoder = None  # pragma: no cover - no usable vocabulary
    return None


def token_backend() -> str:
    """Name the counter in use: ``tiktoken`` or ``estimate``."""
    return "tiktoken" if _tiktoken_encoder() is not None else "estimate"


def estimate_tokens(text: str) -> int:
    """How many tokens ``text`` is worth.

    Exact under ``tiktoken``; otherwise the larger of a characters-per-token
    estimate and a words-times-1.3 estimate.  Taking the max matters for the two
    inputs that break a single formula: dense code or identifiers (many
    characters per word, so the character estimate wins) and heavily punctuated
    prose (many short tokens, so the word estimate wins).
    """
    if not text:
        return 0

    encoder = _tiktoken_encoder()
    if encoder is not None:
        return len(encoder.encode(text, disallowed_special=()))

    by_chars = len(text) / _CHARS_PER_TOKEN
    by_words = text.count(" ") * 1.3 + 1
    return int(max(by_chars, by_words)) + 1


# ─────────────────────────────────────────────────────────────────────────────
#  Chunks
# ─────────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Chunk:
    """One batch of records that fits inside a model's context window."""

    index: int
    """0-based position of this chunk in the sequence."""

    total: int
    """How many chunks the whole export produced — so a prompt can say
    "part 2 of 5", which measurably improves what a model does with a partial
    corpus."""

    records: Tuple["LLMRecord", ...]
    tokens: int
    """Estimated tokens for the records in this chunk, overhead included."""

    threads: Tuple[int, ...]
    """Root ids covered here, for a caller reassembling per-thread answers."""

    @property
    def is_first(self) -> bool:
        return self.index == 0

    @property
    def is_last(self) -> bool:
        return self.index == self.total - 1

    def to_dicts(self) -> List[Dict[str, Any]]:
        """The chunk's records as plain dicts."""
        return [record.to_dict() for record in self.records]

    def to_json(self, indent: Optional[int] = None) -> str:
        """The chunk as a JSON array, ready to paste into a prompt."""
        import json

        return json.dumps(self.to_dicts(), ensure_ascii=False, indent=indent, default=str)

    def to_text(self) -> str:
        """The chunk as compact readable text — cheaper in tokens than JSON.

        Roughly a third fewer tokens than the same records serialised as JSON,
        because every key is written once as a label rather than once per
        record.  Use JSON when the model must echo the structure back, and this
        when it only has to read it.
        """
        return "\n\n".join(record.to_text() for record in self.records)


def _record_tokens(record: "LLMRecord") -> int:
    """Token cost of one record, text plus serialisation overhead."""
    return estimate_tokens(record.to_text()) + _RECORD_OVERHEAD_TOKENS


def chunk_records(
    records: Sequence["LLMRecord"],
    max_tokens: int = 100_000,
    *,
    keep_threads_together: bool = True,
    token_counter: Optional[Callable[[str], int]] = None,
) -> List[Chunk]:
    """Pack records into chunks that each fit ``max_tokens``.

    Parameters
    ----------
    max_tokens:
        Budget per chunk, counting only the records.  Leave room for the prompt
        itself and for the answer: two thirds of the model's window is a
        sensible ceiling, not the whole of it.
    keep_threads_together:
        Never split a comment thread across two chunks.  A thread longer than
        the budget on its own is emitted as one over-budget chunk rather than
        silently cut — a caller can detect that from :attr:`Chunk.tokens` and
        decide, which is better than this function deciding for them.
    token_counter:
        Override the counter, e.g. with a provider's own tokenizer.

    Returns an empty list for no records, so a caller can iterate without a
    guard.
    """
    if not records:
        return []
    if max_tokens <= 0:
        raise ValueError(f"max_tokens must be positive, got {max_tokens}")

    counter = token_counter or (lambda text: estimate_tokens(text))

    def cost(record: "LLMRecord") -> int:
        return counter(record.to_text()) + _RECORD_OVERHEAD_TOKENS

    # Group first, then pack the groups. A group is a thread when threads must
    # stay whole and a single record otherwise, which collapses both modes onto
    # one packing loop instead of two that could drift apart.
    groups: List[List["LLMRecord"]] = []
    if keep_threads_together:
        by_root: Dict[Tuple[str, int], List["LLMRecord"]] = {}
        for record in records:
            by_root.setdefault((record.document, record.thread_id), []).append(record)
        groups = list(by_root.values())
    else:
        groups = [[record] for record in records]

    packed: List[Tuple[List["LLMRecord"], int]] = []
    current: List["LLMRecord"] = []
    current_tokens = 0

    for group in groups:
        group_tokens = sum(cost(record) for record in group)
        if current and current_tokens + group_tokens > max_tokens:
            packed.append((current, current_tokens))
            current, current_tokens = [], 0
        current.extend(group)
        current_tokens += group_tokens

    if current:
        packed.append((current, current_tokens))

    total = len(packed)
    return [
        Chunk(
            index=index,
            total=total,
            records=tuple(batch),
            tokens=tokens,
            threads=tuple(dict.fromkeys(record.thread_id for record in batch)),
        )
        for index, (batch, tokens) in enumerate(packed)
    ]


def chunk_texts(
    texts: Iterable[str],
    max_tokens: int = 100_000,
    *,
    token_counter: Optional[Callable[[str], int]] = None,
) -> List[List[str]]:
    """Pack plain strings into budget-sized batches.

    The same packing, for callers who are not using :class:`LLMRecord` — a list
    of comment bodies, a set of document paragraphs.
    """
    counter = token_counter or (lambda text: estimate_tokens(text))
    batches: List[List[str]] = []
    current: List[str] = []
    current_tokens = 0

    for text in texts:
        tokens = counter(text)
        if current and current_tokens + tokens > max_tokens:
            batches.append(current)
            current, current_tokens = [], 0
        current.append(text)
        current_tokens += tokens

    if current:
        batches.append(current)
    return batches
