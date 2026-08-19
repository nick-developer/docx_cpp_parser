"""The record a model is actually given, and the four ways to hand it over.

``parser.to_llm_dataset()`` is the entry point the roadmap names, and this is
what it builds: one :class:`LLMRecord` per comment, carrying the comment, the
document text around it, the thread it belongs to, and the rule-based tags from
:mod:`.classification`.

Four things had to be decided to make that record useful rather than merely
complete.

**Context is included, not implied.**  A record without the surrounding document
text forces the model to answer questions about a fragment.  :mod:`.context`
reads it back out of the .docx, so ``context_before`` and ``context_after`` are
real document text and not a paraphrase of the anchor.

**The thread travels with every comment in it.**  It is redundant — the same
thread appears on each of its comments — and it is the right trade: a retrieval
system pulls back single records, and a record that cannot see the reply that
resolved it will report an answered question as open.  Set ``include_thread=False``
when the whole corpus goes to the model in one piece and the redundancy is pure
cost.

**Privacy is a parameter, not a separate step.**  ``anonymize=`` runs the export
through :mod:`docx_comment_parser.privacy` *before* the records are built, so
there is no window in which a record object holds real names and no path where
somebody exports first and redacts later.  It is off by default — this is a
library, not a policy — but the CLI scans and warns, and one keyword turns it on.

**Serialisation is boring on purpose.**  Plain JSON-safe types, dates as ISO
strings, no nesting beyond the thread list.  A schema an ingestion pipeline can
consume without a custom decoder is worth more than a clever one.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import (
    TYPE_CHECKING,
    Any,
    Dict,
    Iterable,
    List,
    Optional,
    Sequence,
    Tuple,
    Union,
)

from ..models import Comment
from .classification import Classification, classify_text
from .context import ContextCache

if TYPE_CHECKING:  # pragma: no cover - typing only
    import pandas

    from ..privacy import AnonymizationReport, Anonymizer
    from ..privacy.policy import PolicyLike

__all__ = [
    "LLMRecord",
    "LLMDataset",
    "build_dataset",
    "to_llm_dataset",
    "to_embeddings_input",
    "to_jsonl",
    "export_jsonl",
]

PathLike = Union[str, "Path"]

DEFAULT_CHARS_BEFORE = 500
DEFAULT_CHARS_AFTER = 500


@dataclass(frozen=True)
class LLMRecord:
    """One comment, with everything a model needs to reason about it.

    The field order is the order the roadmap's schema gives, extended with the
    review-intelligence tags and the thread.
    """

    comment_id: int
    author: str
    context_before: str
    referenced_text: str
    context_after: str
    comment: str
    thread: Tuple[Dict[str, Any], ...]
    """Every comment in this one's thread, oldest first, each as
    ``{comment_id, author, text, resolved, is_reply, date}``."""

    # ── Position and status ─────────────────────────────────────────────────
    thread_id: int
    """Root comment id — the stable identity of the conversation."""

    parent_id: Optional[int]
    is_reply: bool
    thread_depth: int
    resolved: bool
    date: str
    """ISO-8601, or the raw value when it could not be parsed. Never ``None``,
    because a JSON consumer with a missing key is a bug waiting to happen."""

    document: str
    paragraph_style: str

    # ── Review intelligence ─────────────────────────────────────────────────
    category: str
    category_confidence: float
    intent: str
    priority: str
    action_required: bool

    def to_dict(self) -> Dict[str, Any]:
        """The record as plain JSON-safe data, in schema order."""
        return {
            "comment_id": self.comment_id,
            "author": self.author,
            "context_before": self.context_before,
            "referenced_text": self.referenced_text,
            "context_after": self.context_after,
            "comment": self.comment,
            "thread": [dict(entry) for entry in self.thread],
            "thread_id": self.thread_id,
            "parent_id": self.parent_id,
            "is_reply": self.is_reply,
            "thread_depth": self.thread_depth,
            "resolved": self.resolved,
            "date": self.date,
            "document": self.document,
            "paragraph_style": self.paragraph_style,
            "category": self.category,
            "category_confidence": self.category_confidence,
            "intent": self.intent,
            "priority": self.priority,
            "action_required": self.action_required,
        }

    def to_text(self) -> str:
        """A compact readable rendering, for pasting into a prompt.

        Empty sections are omitted rather than written as blank labels: a
        model reading twenty records should not have to skip twenty
        ``Context: (none)`` lines to find the one that has context.
        """
        lines = [
            f"[#{self.comment_id}] {self.author}"
            f"{' (reply)' if self.is_reply else ''}"
            f" — {'resolved' if self.resolved else 'open'}"
            f" — {self.category}/{self.priority}"
        ]
        if self.referenced_text:
            lines.append(f'  on: "{_clip(self.referenced_text, 300)}"')
        if self.context_before or self.context_after:
            lines.append(
                f"  context: …{_clip(self.context_before, 200)}"
                f" ⟪anchor⟫ {_clip(self.context_after, 200)}…"
            )
        lines.append(f"  says: {self.comment}")
        if len(self.thread) > 1:
            for entry in self.thread:
                if entry["comment_id"] == self.comment_id:
                    continue
                lines.append(f"  ↳ {entry['author']}: {_clip(str(entry['text']), 200)}")
        return "\n".join(lines)


def _clip(text: str, limit: int) -> str:
    """Shorten to ``limit`` characters on a word boundary where one is near."""
    collapsed = " ".join(text.split())
    if len(collapsed) <= limit:
        return collapsed
    cut = collapsed.rfind(" ", 0, limit)
    return collapsed[: cut if cut > limit * 0.6 else limit].rstrip() + "…"


# ─────────────────────────────────────────────────────────────────────────────
#  Dataset
# ─────────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class LLMDataset:
    """The records, plus what was done to produce them.

    Iterable and indexable, so it can be used wherever a list of records is
    expected, while still carrying the anonymisation report — which a caller
    must be able to see without asking for it separately.
    """

    records: Tuple[LLMRecord, ...]
    anonymization: Optional["AnonymizationReport"] = None
    anonymizer: Optional["Anonymizer"] = None
    """Present when anonymisation ran. Holds the vault that reverses the
    pseudonyms in a model's answer — see ``Anonymizer.deanonymize``."""

    def __iter__(self) -> Any:
        return iter(self.records)

    def __len__(self) -> int:
        return len(self.records)

    def __getitem__(self, index: int) -> LLMRecord:
        return self.records[index]

    @property
    def is_anonymized(self) -> bool:
        return self.anonymization is not None

    def to_dicts(self) -> List[Dict[str, Any]]:
        return [record.to_dict() for record in self.records]

    def to_json(self, indent: int = 2) -> str:
        return json.dumps(self.to_dicts(), ensure_ascii=False, indent=indent)

    def to_jsonl(self) -> str:
        return to_jsonl(self.records)

    def export_jsonl(self, path: PathLike) -> "Path":
        return export_jsonl(self.records, path)

    def to_dataframe(self) -> "pandas.DataFrame":
        """Records as a pandas DataFrame; the thread column holds lists."""
        from ..exporters.dataframe import _require

        pandas = _require("pandas", "pandas")
        return pandas.DataFrame(self.to_dicts())

    def chunks(self, max_tokens: int = 100_000, **kwargs: Any) -> List[Any]:
        """Split into context-window-sized batches. See :mod:`.chunking`."""
        from .chunking import chunk_records

        return chunk_records(self.records, max_tokens, **kwargs)


# ─────────────────────────────────────────────────────────────────────────────
#  Building
# ─────────────────────────────────────────────────────────────────────────────


def _thread_entries(
    thread: Sequence[Comment],
) -> Tuple[Dict[str, Any], ...]:
    """Project a thread to the compact form carried on every record in it."""
    return tuple(
        {
            "comment_id": comment.comment_id,
            "author": comment.author,
            "text": comment.text,
            "resolved": comment.resolved,
            "is_reply": comment.is_reply,
            "date": _iso(comment),
        }
        for comment in thread
    )


def _iso(comment: Comment) -> str:
    """The comment's timestamp as ISO-8601, falling back to the raw XML value."""
    if comment.date_parsed is not None:
        return comment.date_parsed.isoformat()
    return comment.date


def _threads(comments: Sequence[Comment]) -> Dict[Tuple[str, int], List[Comment]]:
    """Group comments into threads, keyed by (document, root id).

    Keyed on the document too because comment ids are only unique within one
    file, and a batch export routinely holds several.  Getting this wrong would
    silently weld two documents' threads together.
    """
    grouped: Dict[Tuple[str, int], List[Comment]] = {}
    for comment in comments:
        grouped.setdefault((comment.document_name, comment.root_id), []).append(comment)

    for thread in grouped.values():
        # Oldest first, ids as the tiebreak: a thread read out of order is the
        # single fastest way to make a model misattribute a decision.
        thread.sort(
            key=lambda comment: (
                comment.date_parsed.timestamp() if comment.date_parsed else 0.0,
                comment.comment_id,
            )
        )
    return grouped


def build_dataset(
    comments: Iterable[Comment],
    *,
    chars_before: int = DEFAULT_CHARS_BEFORE,
    chars_after: int = DEFAULT_CHARS_AFTER,
    include_thread: bool = True,
    include_context: bool = True,
    classify: bool = True,
    anonymize: Optional["PolicyLike"] = None,
    anonymizer: Optional["Anonymizer"] = None,
    documents: Optional[Dict[str, str]] = None,
) -> LLMDataset:
    """Turn parsed comments into model-ready records.

    Parameters
    ----------
    chars_before / chars_after:
        Width of the document context window around each anchor.  Set both to 0,
        or ``include_context=False``, to skip reading the source documents
        entirely — which is what you want when the .docx files are no longer on
        this machine, or when the anchors alone are enough.
    include_thread:
        Carry the whole conversation on every record in it.
    classify:
        Run the rule-based category / intent / priority pass.
    anonymize:
        A policy, a preset name, or ``True``.  Applied to the comments before
        any record is built, so private values never enter a record at all.
    anonymizer:
        Reuse an existing :class:`~docx_comment_parser.privacy.Anonymizer` so
        several exports share one set of stand-ins.
    documents:
        Override where a document's text is read from, as
        ``{document_name: path}``.  For the case where the export was produced
        on one machine and the .docx files live somewhere else on this one.

    Returns
    -------
    :class:`LLMDataset` — a sequence of records that also carries the
    anonymisation report and the vault needed to reverse it.
    """
    materialised = list(comments)

    report = None
    engine = anonymizer
    if anonymize is not None or anonymizer is not None:
        from ..privacy import Anonymizer, anonymize_comments

        # The engine is built here rather than left to anonymize_comments, which
        # would build its own and keep the vault out of reach. The vault is what
        # turns a model's answer back into real names, so it has to come back to
        # the caller on the dataset.
        if engine is None:
            engine = Anonymizer(
                anonymize if anonymize is not None else True,
                names=[comment.author for comment in materialised],
            )
        materialised, report = anonymize_comments(materialised, anonymizer=engine)

    grouped = _threads(materialised)
    cache = ContextCache(enabled=include_context and (chars_before or chars_after) > 0)
    overrides = documents or {}

    records: List[LLMRecord] = []
    for comment in materialised:
        source = overrides.get(comment.document_name, comment.document_name)
        window = cache.get(source).window(comment.comment_id, chars_before, chars_after)

        thread = grouped.get((comment.document_name, comment.root_id), [comment])
        classification = (
            classify_text(comment.text, comment.referenced_text)
            if classify
            else _UNCLASSIFIED
        )

        records.append(
            LLMRecord(
                comment_id=comment.comment_id,
                author=comment.author,
                context_before=window.before,
                referenced_text=comment.referenced_text or window.anchor,
                context_after=window.after,
                comment=comment.text,
                thread=_thread_entries(thread) if include_thread else (),
                thread_id=comment.root_id,
                parent_id=comment.parent_id,
                is_reply=comment.is_reply,
                thread_depth=comment.thread_depth,
                resolved=comment.resolved,
                date=_iso(comment),
                document=comment.document_name,
                paragraph_style=comment.paragraph_style,
                category=classification.category.value,
                category_confidence=round(classification.confidence, 3),
                intent=classification.intent.value,
                priority=classification.priority.value,
                action_required=classification.action_required,
            )
        )

    return LLMDataset(
        records=tuple(records), anonymization=report, anonymizer=engine
    )


#: The tags a record carries when classification is switched off. A record must
#: always have the fields; "not computed" is expressed as ``Other`` with zero
#: confidence, not as a missing key.
_UNCLASSIFIED = classify_text("")


def to_llm_dataset(
    comments: Iterable[Comment],
    *,
    chars_before: int = DEFAULT_CHARS_BEFORE,
    chars_after: int = DEFAULT_CHARS_AFTER,
    **kwargs: Any,
) -> List[Dict[str, Any]]:
    """Build the dataset and return it as plain dicts — the roadmap's shape."""
    dataset = build_dataset(
        comments, chars_before=chars_before, chars_after=chars_after, **kwargs
    )
    return dataset.to_dicts()


# ─────────────────────────────────────────────────────────────────────────────
#  Retrieval-ready export
# ─────────────────────────────────────────────────────────────────────────────


def to_embeddings_input(
    comments: Iterable[Comment],
    *,
    granularity: str = "thread",
    chars_before: int = 300,
    chars_after: int = 300,
    anonymize: Optional["PolicyLike"] = None,
    **kwargs: Any,
) -> List[Dict[str, Any]]:
    """One retrieval document per thread (or per comment), ready to embed.

    Each entry is ``{id, text, metadata}`` — the shape every vector store
    accepts, and the shape none of them require you to reshape.

    ``granularity``
        ``"thread"`` (default) emits one document per conversation.  This is
        almost always the right unit: a reply saying "agreed, fixed" is
        meaningless as a retrieval hit on its own, and a thread is still short
        enough to embed whole.  ``"comment"`` emits one per comment, for a
        corpus of long standalone comments where thread-level chunks would blur
        several distinct issues together.
    """
    if granularity not in {"thread", "comment"}:
        raise ValueError(
            f"granularity must be 'thread' or 'comment', got {granularity!r}"
        )

    dataset = build_dataset(
        comments,
        chars_before=chars_before,
        chars_after=chars_after,
        anonymize=anonymize,
        **kwargs,
    )

    if granularity == "comment":
        return [
            {
                "id": f"{record.document}#{record.comment_id}",
                "text": _embedding_text(record),
                "metadata": _metadata(record),
            }
            for record in dataset.records
        ]

    # Thread granularity: one document per conversation, built from its root
    # record so the anchor and context are the thread's own.
    seen: Dict[Tuple[str, int], LLMRecord] = {}
    for record in dataset.records:
        key = (record.document, record.thread_id)
        # The root is the record whose id *is* the thread id; fall back to the
        # first seen so a thread whose root was filtered out still exports.
        if key not in seen or record.comment_id == record.thread_id:
            seen.setdefault(key, record)
            if record.comment_id == record.thread_id:
                seen[key] = record

    documents: List[Dict[str, Any]] = []
    for (document, thread_id), record in seen.items():
        body = [_embedding_text(record)]
        for entry in record.thread:
            if entry["comment_id"] != record.comment_id:
                body.append(f"{entry['author']}: {entry['text']}")
        metadata = _metadata(record)
        metadata["thread_size"] = max(len(record.thread), 1)
        metadata["thread_resolved"] = all(
            bool(entry["resolved"]) for entry in record.thread
        ) if record.thread else record.resolved
        documents.append(
            {
                "id": f"{document}#thread-{thread_id}",
                "text": "\n".join(body),
                "metadata": metadata,
            }
        )
    return documents


def _embedding_text(record: LLMRecord) -> str:
    """The text that gets embedded: the comment, its anchor, and its context.

    Ordered comment-first because retrieval queries are phrased like comments
    ("what did reviewers say about retention?"), and most embedding models
    weight the opening of a document more heavily than its tail.
    """
    parts = [f"{record.author}: {record.comment}"]
    if record.referenced_text:
        parts.append(f'Anchored to: "{record.referenced_text}"')
    context = " ".join(
        piece for piece in (record.context_before, record.context_after) if piece
    ).strip()
    if context:
        parts.append(f"Document context: {context}")
    return "\n".join(parts)


def _metadata(record: LLMRecord) -> Dict[str, Any]:
    """Filterable metadata — scalars only, as vector stores require."""
    return {
        "comment_id": record.comment_id,
        "thread_id": record.thread_id,
        "author": record.author,
        "document": record.document,
        "date": record.date,
        "resolved": record.resolved,
        "is_reply": record.is_reply,
        "category": record.category,
        "priority": record.priority,
        "intent": record.intent,
        "action_required": record.action_required,
    }


# ─────────────────────────────────────────────────────────────────────────────
#  JSONL
# ─────────────────────────────────────────────────────────────────────────────


def to_jsonl(records: Iterable[Union[LLMRecord, Dict[str, Any]]]) -> str:
    """Serialise records as JSON Lines — one object per line, no wrapper array.

    The format every batch and fine-tuning API takes, and the one that streams:
    a consumer reads a line at a time instead of holding the whole corpus in
    memory to find the closing bracket.
    """
    lines = [
        json.dumps(
            record.to_dict() if isinstance(record, LLMRecord) else record,
            ensure_ascii=False,
            default=str,
        )
        for record in records
    ]
    return "\n".join(lines)


def export_jsonl(
    records: Iterable[Union[LLMRecord, Dict[str, Any]]],
    path: PathLike,
    encoding: str = "utf-8",
) -> "Path":
    """Write records to a ``.jsonl`` file. Returns the path written.

    Written line by line rather than joined first, so a 10,000-comment export
    never materialises its own serialisation in memory alongside the records.
    """
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("w", encoding=encoding, newline="\n") as handle:
        for record in records:
            payload = record.to_dict() if isinstance(record, LLMRecord) else record
            handle.write(json.dumps(payload, ensure_ascii=False, default=str))
            handle.write("\n")
    return target
