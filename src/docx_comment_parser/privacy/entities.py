"""What counts as private information, and how overlapping findings are resolved.

A detector reports a :class:`Span`: a half-open ``[start, end)`` slice of some
text, the :class:`EntityType` it believes sits there, and how confident it is.
Several detectors routinely fire on the same characters — ``alice@acme.com`` is
an email address, but the ``alice`` inside it is also a reviewer's name, and the
``acme.com`` inside it is also a domain — so the spans a scan produces are a
*proposal*, not an answer.  :func:`resolve_spans` turns the proposal into a
partition: a set of non-overlapping spans that can be substituted into the text
in one left-to-right pass.

The resolution rule is deliberate and worth stating plainly, because it decides
what leaks:

1. A span that covers more characters wins over one that covers fewer.
2. Ties break on the detector's specificity (an IBAN outranks a bare number).
3. Remaining ties break on confidence, then on position, so the outcome does not
   depend on the order the recognizers happened to run in.

"Longest wins" is the safe direction.  Preferring the *shorter* span would
replace ``alice`` inside ``alice@acme.com`` and publish the domain; preferring
the longer one replaces the whole address, which is never worse than necessary.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Dict, Iterable, List, Sequence, Tuple

__all__ = ["EntityType", "Span", "resolve_spans", "merge_adjacent"]


class EntityType(str, Enum):
    """A kind of information that should not reach a third-party model as-is.

    Subclassing ``str`` keeps these usable as plain dictionary keys and as JSON
    values, which matters because policies are configured by name and reports
    are serialised.
    """

    # ── Directly identifying ────────────────────────────────────────────────
    PERSON = "PERSON"
    """A human name — reviewers, people named inside a comment."""

    USERNAME = "USERNAME"
    """An account handle: ``@alice``, ``alice.smith`` in a path or a mention."""

    EMAIL = "EMAIL"
    PHONE = "PHONE"

    # ── Government and financial identifiers ────────────────────────────────
    SSN = "SSN"
    """US Social Security number."""

    NATIONAL_ID = "NATIONAL_ID"
    """Other national identifiers: UK NINO, and similar formatted ids."""

    PASSPORT = "PASSPORT"
    CREDIT_CARD = "CREDIT_CARD"
    IBAN = "IBAN"

    # ── Technical identifiers ───────────────────────────────────────────────
    IP_ADDRESS = "IP_ADDRESS"
    MAC_ADDRESS = "MAC_ADDRESS"
    URL = "URL"
    FILE_PATH = "FILE_PATH"
    """A path carrying a home directory, and therefore a login name."""

    # ── Credentials ─────────────────────────────────────────────────────────
    API_KEY = "API_KEY"
    """A recognisably shaped provider token: AWS, GitHub, Slack, OpenAI, Google."""

    JWT = "JWT"
    PRIVATE_KEY = "PRIVATE_KEY"
    SECRET = "SECRET"
    """A value introduced by a password/secret/token keyword."""

    # ── Contextual ──────────────────────────────────────────────────────────
    DATE_OF_BIRTH = "DATE_OF_BIRTH"
    ORGANIZATION = "ORGANIZATION"
    """Only from a caller-supplied term list — no name gazetteer is shipped."""

    LOCATION = "LOCATION"
    """Likewise caller-supplied."""

    CUSTOM = "CUSTOM"
    """Anything matched by a caller-supplied pattern."""

    def __str__(self) -> str:  # pragma: no cover - trivial
        return self.value


#: How specific a detection of each type is, used to break length ties.
#: A checksum-validated identifier outranks a shape-matched one, which outranks
#: anything inferred from surrounding words.  Absent types score 0.
_SPECIFICITY: Dict[EntityType, int] = {
    EntityType.PRIVATE_KEY: 100,
    EntityType.JWT: 95,
    EntityType.API_KEY: 95,
    EntityType.IBAN: 90,
    EntityType.CREDIT_CARD: 90,
    EntityType.SSN: 85,
    EntityType.NATIONAL_ID: 80,
    EntityType.PASSPORT: 75,
    EntityType.EMAIL: 70,
    EntityType.URL: 65,
    EntityType.FILE_PATH: 60,
    EntityType.IP_ADDRESS: 60,
    EntityType.MAC_ADDRESS: 60,
    # Above PHONE: a date of birth is only ever detected because the words
    # "date of birth" precede it, and that context is stronger evidence than
    # the digit-grouping shape a phone number is matched on. Ranked the other
    # way, "DOB: 1985-04-12" resolves to a phone number and the birth date is
    # reported as one.
    EntityType.DATE_OF_BIRTH: 58,
    EntityType.PHONE: 55,
    EntityType.SECRET: 50,
    EntityType.CUSTOM: 40,
    EntityType.PERSON: 35,
    EntityType.ORGANIZATION: 30,
    EntityType.LOCATION: 30,
    EntityType.USERNAME: 25,
}


@dataclass(frozen=True)
class Span:
    """One detection: a slice of text believed to hold information of a type."""

    start: int
    end: int
    entity: EntityType
    text: str
    """The matched substring, carried so the caller never has to re-slice."""

    score: float = 1.0
    """Confidence, 0.0–1.0. Checksum-validated matches are 1.0; shape-only
    matches sit lower so a policy can raise its floor and drop them."""

    recognizer: str = ""
    """Which detector produced this, for auditing a surprising redaction."""

    def __post_init__(self) -> None:
        if self.end < self.start:  # pragma: no cover - defensive
            raise ValueError(f"span end {self.end} precedes start {self.start}")

    @property
    def length(self) -> int:
        return self.end - self.start

    def overlaps(self, other: "Span") -> bool:
        """True when the two spans share at least one character."""
        return self.start < other.end and other.start < self.end

    @property
    def specificity(self) -> int:
        """How specific this entity type is; higher wins a length tie."""
        return _SPECIFICITY.get(self.entity, 0)


def _rank(span: Span) -> Tuple[int, int, float, int]:
    """Sort key placing the span that should survive a conflict first.

    Negated positions give descending order on the first three components while
    keeping the last (position) ascending, so the whole comparison is one tuple
    and the result never depends on recognizer registration order.
    """
    return (-span.length, -span.specificity, -span.score, span.start)


def resolve_spans(spans: Iterable[Span]) -> List[Span]:
    """Reduce overlapping detections to a non-overlapping list, in text order.

    Greedy by :func:`_rank`: take the strongest span, discard everything that
    touches it, repeat.  With ``n`` spans this is O(n log n) for the sort plus a
    linear sweep per accepted span — and the number of spans found in a review
    comment is small, so the simple formulation is the right one.

    The result is sorted by ``start``, which is what a substitution pass needs.
    """
    ordered = sorted(spans, key=_rank)
    accepted: List[Span] = []

    for span in ordered:
        if span.length <= 0:
            continue
        # Compare against accepted spans only. The list stays short (a comment
        # holds a handful of entities), so a scan beats maintaining an interval
        # tree that would never pay for its own construction.
        if any(span.overlaps(kept) for kept in accepted):
            continue
        accepted.append(span)

    accepted.sort(key=lambda span: span.start)
    return accepted


def merge_adjacent(spans: Sequence[Span], gap: int = 1) -> List[Span]:
    """Join same-type spans separated by at most ``gap`` characters.

    ``Alice`` followed by ``Smith`` is two PERSON detections with a space
    between them; emitting two pseudonyms for one person would both read badly
    and leak the fact that the name has two parts.  Only same-type runs merge,
    and the merged span keeps the lowest confidence of its parts — a chain is no
    more certain than its weakest link.

    Input must already be non-overlapping and sorted, as :func:`resolve_spans`
    returns it.
    """
    if not spans:
        return []

    merged: List[Span] = [spans[0]]
    for span in spans[1:]:
        previous = merged[-1]
        if span.entity is previous.entity and span.start - previous.end <= gap:
            merged[-1] = Span(
                start=previous.start,
                end=span.end,
                entity=previous.entity,
                # The joined text is reconstructed from the parts plus the gap;
                # the caller re-slices from the source when it needs the exact
                # characters, so a synthesised separator here is safe.
                text=previous.text + " " * (span.start - previous.end) + span.text,
                score=min(previous.score, span.score),
                recognizer=previous.recognizer,
            )
        else:
            merged.append(span)

    return merged
