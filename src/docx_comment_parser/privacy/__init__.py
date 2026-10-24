"""Masking names and other private information before an export leaves the machine.

A .docx review is one of the most personal artefacts a company holds.  Every
comment carries a named human, an opinion, a timestamp, and often a phone
number, a customer name or — genuinely, routinely — an API key someone pasted
into a comment and forgot.  Sending that to a hosted model is a disclosure, and
"we only sent the comments" is not a defence.

This package exists so the disclosure can be a decision rather than an
accident::

    from docx_comment_parser import anonymize_comments

    clean, report = anonymize_comments(parser.to_comments(), "strict")
    print(report.summary())          # what was replaced, and what survived

Four modules, in the order the work happens:

:mod:`.entities`
    What counts as private, and how overlapping findings are resolved.

:mod:`.recognizers`
    The detectors.  Checksum-validated where a checksum exists (Luhn for cards,
    mod-97 for IBANs), roster-driven for people.  Pluggable: any object with a
    ``find(text)`` can join.

:mod:`.policy`
    Which findings to act on and how — redact, pseudonymise, mask, hash,
    remove — with presets from ``names_only`` through ``gdpr``, plus your own
    values and regular expressions (:func:`~.policy.make_policy`).

:mod:`.validation`
    Checks those values and patterns before they are trusted, and says what is
    wrong with one that is not.

:mod:`.anonymizer`
    The engine, the local vault that can put the real names back into a model's
    answer, and the verification pass that re-scans the output.

The pre-flight check, when you only want to know what is in there::

    from docx_comment_parser.privacy import scan_comments

    scan = scan_comments(parser.to_comments())
    if scan.has_secrets:
        raise SystemExit(scan.summary())

Nothing here is imported by ``import docx_comment_parser``; the package resolves
on first use, and the whole of it is standard library only.
"""

from __future__ import annotations

from .anonymizer import (
    AnonymizationReport,
    Anonymizer,
    PrivacyScan,
    Vault,
    anonymize_comments,
    scan_comments,
)
from .entities import EntityType, Span, resolve_spans
from .policy import (
    PRESET_NAMES,
    AnonymizationPolicy,
    PolicyLike,
    Strategy,
    make_policy,
    resolve_policy,
)
from .validation import (
    InvalidPatternError,
    PatternIssue,
    check_pattern,
    validate_patterns,
    validate_values,
)
from .recognizers import (
    NameRecognizer,
    Recognizer,
    RegexRecognizer,
    TermRecognizer,
    default_recognizers,
    iban_valid,
    luhn_valid,
)

__all__ = [
    # the two calls most users need
    "anonymize_comments",
    "scan_comments",
    # configuration
    "AnonymizationPolicy",
    "Strategy",
    "PolicyLike",
    "PRESET_NAMES",
    "resolve_policy",
    "make_policy",
    # the caller's own values and patterns (new in 1.5.1)
    "check_pattern",
    "validate_patterns",
    "validate_values",
    "PatternIssue",
    "InvalidPatternError",
    # engine and results
    "Anonymizer",
    "Vault",
    "AnonymizationReport",
    "PrivacyScan",
    # detection
    "EntityType",
    "Span",
    "resolve_spans",
    "Recognizer",
    "RegexRecognizer",
    "NameRecognizer",
    "TermRecognizer",
    "default_recognizers",
    "luhn_valid",
    "iban_valid",
]
