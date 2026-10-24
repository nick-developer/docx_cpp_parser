"""Turning a .docx review into something a language model can reason about.

The rest of this library answers "what is in this document".  This package
answers the question after it: *what do I send to a model, and in what shape, so
that the answer is worth reading.*

    records = parser.to_llm_dataset()          # the roadmap's schema, as dicts
    prompt  = create_action_items_prompt(...)  # packed, budgeted, id-cited
    docs    = parser.to_embeddings_input()     # one document per thread, for RAG

The modules, in the order the work happens:

:mod:`.context`
    Reads ``word/document.xml`` back to recover the text either side of each
    comment's anchor — the part that turns "this is wrong" into a question with
    an answer.  Measured in characters, words, sentences or paragraphs (see
    :class:`~.context.ContextSpec`).  Standard library, on demand, cached per
    document.

:mod:`.classification`
    Rule-based category, intent and priority.  Microseconds, explainable, and
    entirely local; it is what lets a prompt say "the 12 compliance comments"
    instead of paying a model to work that out.

:mod:`.overrides`
    Manual corrections to those tags — from code, a dict, or a spreadsheet —
    validated and recorded as human labels.

:mod:`.dataset`
    :class:`~.dataset.LLMRecord` and the exports built on it — dicts, JSONL, a
    DataFrame, retrieval documents.

:mod:`.compression`
    Fewer tokens for the same review: whitespace, thread de-duplication,
    query-focused sentence extraction, filler removal.

:mod:`.chunking`
    Fitting a corpus into a context window without splitting a thread in half.
    Exact under ``tiktoken``, calibrated without it.

:mod:`.prompts` and :mod:`.safety`
    Six prompt builders that pack the corpus, state what did not fit, require
    every claim to cite a comment id, and treat the review as untrusted data.

:mod:`.responses`
    Reading the answer back: JSON extraction, citation checking, names
    restored, and a triage answer turned into overrides.

There are no AI dependencies here and no network calls anywhere — the roadmap's
first acceptance criterion.  Nothing in this package sends anything; it produces
strings and structures, and what happens to them is the caller's business.

Privacy sits one import away, in :mod:`docx_comment_parser.privacy`, and is
wired into every entry point here as ``anonymize=``::

    records = parser.to_llm_dataset(anonymize="strict")

which replaces names and identifiers *before* a record is built, and hands back
the vault that puts them back into the model's answer.
"""

from __future__ import annotations

from .chunking import Chunk, chunk_records, chunk_texts, estimate_tokens, token_backend
from .classification import (
    Category,
    Classification,
    Intent,
    Priority,
    category_breakdown,
    classify_comment,
    classify_comments,
    classify_text,
)
from .compression import (
    COMPRESSION_LEVELS,
    CompressionConfig,
    CompressionStats,
    compress_context,
    compress_records,
)
from .context import CONTEXT_UNITS, ContextSpec, ContextWindow, DocumentContext, split_sentences
from .dataset import (
    LLMDataset,
    LLMRecord,
    build_dataset,
    export_jsonl,
    to_embeddings_input,
    to_jsonl,
    to_llm_dataset,
)
from .overrides import (
    OVERRIDABLE_FIELDS,
    OverrideError,
    apply_overrides,
    export_labels,
    load_overrides,
)
from .prompts import (
    Prompt,
    create_action_items_prompt,
    create_diff_summary_prompt,
    create_resolution_prompt,
    create_review_summary_prompt,
    create_risk_prompt,
    create_triage_prompt,
)
from .responses import (
    ParsedResponse,
    ResponseParseError,
    extract_json,
    overrides_from_response,
    parse_response,
)
from .safety import screen_records, screen_text

__all__ = [
    # context (new in 1.5.1: sentence / paragraph / word windows)
    "ContextSpec",
    "CONTEXT_UNITS",
    "split_sentences",
    # manual classification (new in 1.5.1)
    "apply_overrides",
    "load_overrides",
    "export_labels",
    "OverrideError",
    "OVERRIDABLE_FIELDS",
    # compression (new in 1.5.1)
    "compress_records",
    "compress_context",
    "CompressionConfig",
    "CompressionStats",
    "COMPRESSION_LEVELS",
    # responses and safety (new in 1.5.1)
    "parse_response",
    "extract_json",
    "overrides_from_response",
    "ParsedResponse",
    "ResponseParseError",
    "screen_text",
    "screen_records",
    # dataset
    "to_llm_dataset",
    "build_dataset",
    "LLMDataset",
    "LLMRecord",
    "to_embeddings_input",
    "to_jsonl",
    "export_jsonl",
    # classification
    "classify_text",
    "classify_comment",
    "classify_comments",
    "category_breakdown",
    "Classification",
    "Category",
    "Intent",
    "Priority",
    # context
    "DocumentContext",
    "ContextWindow",
    # chunking
    "estimate_tokens",
    "token_backend",
    "chunk_records",
    "chunk_texts",
    "Chunk",
    # prompts
    "Prompt",
    "create_review_summary_prompt",
    "create_action_items_prompt",
    "create_resolution_prompt",
    "create_triage_prompt",
    "create_risk_prompt",
    "create_diff_summary_prompt",
]
