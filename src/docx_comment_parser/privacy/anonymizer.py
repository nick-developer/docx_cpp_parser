"""The engine: scan, replace, verify, and — locally — put it back.

:class:`Anonymizer` is the object that does the work.  Give it a policy, hand it
comments or text, get back the same shape with the private parts replaced.

Three properties are worth knowing before trusting it with anything.

**Replacement is consistent.**  Every occurrence of one person maps to one
stand-in, in every field of every comment, whether they were spelled ``Alice``,
``alice tester`` or ``@alice``.  Without that, a model reading the output cannot
tell that two comments came from the same reviewer, and the export loses the
thing review analysis is for.

**The mapping never leaves the process.**  The :class:`Vault` holding
placeholder → original lives in memory on the caller's machine.  It is what
makes :meth:`Anonymizer.deanonymize` able to put the real names back into a
model's *answer*, which is the workflow that makes anonymisation practical
rather than merely virtuous: send stand-ins, get an analysis back, restore the
names locally before a human reads it.  Writing the vault to disk is possible
and is an explicit, separately named call, because a vault beside its own
anonymised export is the same data with an extra step.

**The result is checked.**  After replacing, the output is scanned again with
the same detectors and anything still matching is reported as residual risk.  A
redactor that cannot tell you whether it worked is a redactor that will one day
quietly stop working.

Pseudonym derivation uses HMAC-SHA256 under a key that is random per
:class:`Anonymizer` unless one is supplied.  A random key means two runs produce
different tokens for the same person, which is the safe default — a token that
is stable across everything you have ever exported is itself a persistent
identifier.  Pass ``key=`` when you deliberately want stable tokens across runs,
and treat that key as a secret, because with the token scheme published here,
anyone holding it can confirm a guessed name.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import threading
from dataclasses import dataclass, replace
from pathlib import Path
from typing import (
    Any,
    Callable,
    Dict,
    FrozenSet,
    Iterable,
    List,
    Mapping,
    Optional,
    Sequence,
    Tuple,
    Union,
)

from ..models import Comment
from .entities import EntityType, Span, merge_adjacent, resolve_spans
from .policy import (
    COMMENT_TEXT_FIELDS,
    AnonymizationPolicy,
    PolicyLike,
    Strategy,
    resolve_policy,
)
from .recognizers import NameRecognizer, Recognizer, default_recognizers

__all__ = [
    "Anonymizer",
    "Vault",
    "AnonymizationReport",
    "PrivacyScan",
    "anonymize_comments",
    "scan_comments",
]

PathLike = Union[str, "Path"]

#: Mints a stand-in from (entity type, original value, 1-based index for that
#: type). Passed into the vault rather than called by it, so the vault holds the
#: mapping and the anonymizer holds the naming scheme.
PseudonymFactory = Callable[[EntityType, str, int], str]

#: Values shorter than this are cached by the text-level anonymiser. Author
#: names, initials and document names repeat on every row of a large export;
#: comment bodies are long and mostly unique, so caching them would grow without
#: bound for no hit rate.
_CACHE_MAX_CHARS = 96

#: Candidate labels for the role-style pseudonyms, in preference order. More
#: than one per type because the first choice can collide with a real name --
#: "Reviewer" is a plausible surname, and a document reviewed by a Bob Reviewer
#: must not have its people renamed to "Reviewer 1", which would both read as a
#: real name and defeat the verification pass.
_ROLE_LABELS: Mapping[EntityType, Tuple[str, ...]] = {
    EntityType.PERSON: ("Reviewer", "Person", "Participant", "Contributor"),
    EntityType.USERNAME: ("user", "handle", "account"),
    EntityType.ORGANIZATION: ("Organisation", "Company", "Entity"),
    EntityType.LOCATION: ("Location", "Place", "Site"),
}


# ─────────────────────────────────────────────────────────────────────────────
#  Vault
# ─────────────────────────────────────────────────────────────────────────────


class Vault:
    """The local, in-memory mapping between originals and their stand-ins.

    Deliberately not a dataclass and deliberately not serialised by anything
    that serialises a report: the whole point is that this object is the piece
    that stays behind.  It is safe to share between threads.
    """

    __slots__ = ("_forward", "_reverse", "_counters", "_lock")

    def __init__(self) -> None:
        self._forward: Dict[Tuple[EntityType, str], str] = {}
        self._reverse: Dict[str, str] = {}
        self._counters: Dict[EntityType, int] = {}
        self._lock = threading.Lock()

    def __len__(self) -> int:
        return len(self._forward)

    def __contains__(self, original: object) -> bool:
        return any(key[1] == original for key in self._forward)

    def get_or_create(
        self, entity: EntityType, original: str, make: PseudonymFactory
    ) -> str:
        """Return the stand-in for ``original``, minting one on first sight.

        The counter passed to ``make`` is 1-based per entity type, so the first
        person seen is ``Reviewer 1``.
        """
        key = (entity, original)
        # Fast path outside the lock: after the first few comments every lookup
        # is a hit, and a 10,000-comment export should not take a lock 10,000
        # times to learn nothing.
        existing = self._forward.get(key)
        if existing is not None:
            return existing

        with self._lock:
            existing = self._forward.get(key)
            if existing is not None:
                return existing
            index = self._counters.get(entity, 0) + 1
            self._counters[entity] = index
            placeholder = make(entity, original, index)
            self._forward[key] = placeholder
            self._reverse[placeholder] = original
            return placeholder

    def original_for(self, placeholder: str) -> Optional[str]:
        """The real value behind a stand-in, or ``None`` if it is not one."""
        return self._reverse.get(placeholder)

    def placeholders(self) -> Tuple[str, ...]:
        """Every stand-in issued, longest first.

        Longest first is what :meth:`Anonymizer.deanonymize` needs: replacing
        ``Reviewer 1`` before ``Reviewer 11`` would corrupt the second.
        """
        return tuple(sorted(self._reverse, key=len, reverse=True))

    def to_dict(self) -> Dict[str, str]:
        """A plain ``{placeholder: original}`` copy.

        This is the sensitive half of the anonymisation.  Do not send it
        anywhere you would not have sent the original document.
        """
        return dict(self._reverse)

    def export(self, path: PathLike) -> "Path":
        """Write the mapping to a JSON file, readable only by the current user.

        On POSIX the file is created with mode 0600 before anything is written
        to it, so the contents are never briefly world-readable.  Windows
        inherits the directory ACL — put it somewhere already private.
        """
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)

        # Open through os.open with an explicit mode: Path.write_text would
        # create the file at the process umask and chmod afterwards, leaving a
        # window in which the mapping is readable by anyone.
        descriptor = os.open(
            str(target), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600
        )
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(
                {
                    "warning": (
                        "This file re-identifies an anonymised export. "
                        "Keep it separate from the export it belongs to."
                    ),
                    "mapping": self.to_dict(),
                },
                handle,
                ensure_ascii=False,
                indent=2,
            )
        return target

    @classmethod
    def load(cls, path: PathLike) -> "Vault":
        """Rebuild a vault from :meth:`export`, for de-anonymising later."""
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
        mapping = payload.get("mapping", payload)
        vault = cls()
        for placeholder, original in mapping.items():
            vault._reverse[placeholder] = original
            # The entity type is not recorded — it is not needed to reverse a
            # replacement, and leaving it out keeps the file to what it must
            # contain and nothing more.
            vault._forward[(EntityType.CUSTOM, original)] = placeholder
        return vault





# ─────────────────────────────────────────────────────────────────────────────
#  Reports
# ─────────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class AnonymizationReport:
    """What one anonymisation run did, and whether anything survived it."""

    comments: int
    fields_changed: int
    replacements: int
    entity_counts: Mapping[EntityType, int]
    """Detections acted on, per type."""

    skipped_low_confidence: int
    """Detections below the policy's ``min_score``, left in place."""

    residual: Tuple[Span, ...]
    """Anything the verification pass still found in the *output*. Empty is the
    expected result; a non-empty tuple is a bug report about the policy."""

    pseudonyms: int
    """Distinct stand-ins issued — usually the number of people involved."""

    policy_name: str

    @property
    def clean(self) -> bool:
        """True when the verification pass found nothing left to redact."""
        return not self.residual

    def to_dict(self) -> Dict[str, Any]:
        """Plain data, ready for ``json.dumps``. Contains no original values."""
        return {
            "comments": self.comments,
            "fields_changed": self.fields_changed,
            "replacements": self.replacements,
            "entity_counts": {
                str(entity): count for entity, count in sorted(
                    self.entity_counts.items(), key=lambda item: str(item[0])
                )
            },
            "skipped_low_confidence": self.skipped_low_confidence,
            "residual": [
                {"entity": str(span.entity), "recognizer": span.recognizer}
                for span in self.residual
            ],
            "pseudonyms": self.pseudonyms,
            "policy": self.policy_name,
            "clean": self.clean,
        }

    def summary(self) -> str:
        """A short report suitable for a terminal or a log line."""
        lines = [
            f"Anonymisation — {self.policy_name} policy",
            "",
            f"  Comments          {self.comments}",
            f"  Fields changed    {self.fields_changed}",
            f"  Replacements      {self.replacements}",
            f"  Pseudonyms        {self.pseudonyms}",
        ]
        if self.skipped_low_confidence:
            lines.append(
                f"  Below threshold   {self.skipped_low_confidence} (left in place)"
            )
        if self.entity_counts:
            lines.append("")
            width = max(len(str(entity)) for entity in self.entity_counts)
            for entity, count in sorted(
                self.entity_counts.items(), key=lambda item: (-item[1], str(item[0]))
            ):
                lines.append(f"    {str(entity):<{width}}  {count}")

        lines.append("")
        if self.clean:
            lines.append("  Verified          nothing detectable remains")
        else:
            kinds = sorted({str(span.entity) for span in self.residual})
            lines.append(
                f"  WARNING           {len(self.residual)} residual match(es): "
                + ", ".join(kinds)
            )
        return "\n".join(lines)


@dataclass(frozen=True)
class PrivacyScan:
    """What a read-only scan found, with nothing changed.

    The thing to run *before* deciding whether an export needs anonymising —
    and what the CLI runs automatically so it can warn rather than let a
    document with an API key in it leave the machine unremarked.
    """

    comments: int
    findings: Mapping[EntityType, int]
    examples: Mapping[EntityType, Tuple[str, ...]]
    """Up to three redacted samples per type — the type and length only, never
    the value, so a scan report is itself safe to paste into a ticket."""

    @property
    def total(self) -> int:
        return sum(self.findings.values())

    @property
    def has_secrets(self) -> bool:
        """True when a credential was found — the finding that cannot wait."""
        return any(
            entity
            in {
                EntityType.PRIVATE_KEY,
                EntityType.API_KEY,
                EntityType.JWT,
                EntityType.SECRET,
            }
            for entity in self.findings
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "comments": self.comments,
            "total": self.total,
            "findings": {
                str(entity): count
                for entity, count in sorted(
                    self.findings.items(), key=lambda item: str(item[0])
                )
            },
            "has_secrets": self.has_secrets,
        }

    def summary(self) -> str:
        if not self.findings:
            return f"Privacy scan — {self.comments} comment(s), nothing detected."

        lines = [
            f"Privacy scan — {self.total} finding(s) across {self.comments} comment(s)",
            "",
        ]
        width = max(len(str(entity)) for entity in self.findings)
        for entity, count in sorted(
            self.findings.items(), key=lambda item: (-item[1], str(item[0]))
        ):
            samples = ", ".join(self.examples.get(entity, ()))
            suffix = f"   e.g. {samples}" if samples else ""
            lines.append(f"    {str(entity):<{width}}  {count}{suffix}")

        if self.has_secrets:
            lines.append("")
            lines.append("  A credential was detected. Do not export without redacting.")
        return "\n".join(lines)


# ─────────────────────────────────────────────────────────────────────────────
#  The anonymizer
# ─────────────────────────────────────────────────────────────────────────────


class Anonymizer:
    """Applies one policy to text, comments, or a whole export.

    Reusable and safe to share between threads: the vault takes its own lock and
    everything else is either immutable or per-call.  Reusing one instance
    across several documents is in fact the point — the same person gets the
    same stand-in in all of them.
    """

    def __init__(
        self,
        policy: PolicyLike = True,
        *,
        names: Iterable[str] = (),
        key: Optional[bytes] = None,
        recognizers: Optional[Sequence[Recognizer]] = None,
        vault: Optional[Vault] = None,
    ) -> None:
        self.policy: AnonymizationPolicy = resolve_policy(policy)
        # A fresh random key per instance: stable tokens across runs are a
        # feature you must ask for, not one you get by accident.
        self._key: bytes = key if key is not None else secrets.token_bytes(32)
        self.vault: Vault = vault if vault is not None else Vault()

        roster = list(dict.fromkeys([*names, *self.policy.extra_names]))
        self._recognizers: Tuple[Recognizer, ...] = tuple(
            recognizers
            if recognizers is not None
            else default_recognizers(
                roster,
                custom_terms=self.policy.custom_terms,
                custom_patterns=self.policy.custom_patterns,
                detect_mentions=self.policy.detect_mentions,
            )
        )
        self._name_recognizer: Optional[NameRecognizer] = next(
            (r for r in self._recognizers if isinstance(r, NameRecognizer)), None
        )
        self._roster_terms: FrozenSet[str] = (
            self._name_recognizer.terms()
            if self._name_recognizer is not None
            else frozenset()
        )
        # Resolved once, up front: a label chosen per call could differ between
        # the first person and the hundredth if the roster were ever extended.
        self._labels: Dict[EntityType, str] = {
            entity: self._label_for(entity) for entity in _ROLE_LABELS
        }

        # Assign stand-ins to the roster up front, in sorted order, so
        # "Reviewer 1" identifies the same person however the comments happen to
        # be ordered — otherwise a re-export after adding one early comment
        # would renumber everybody.
        for name in sorted(roster):
            if self.policy.covers(EntityType.PERSON):
                self._pseudonym(EntityType.PERSON, name)

        self._cache: Dict[str, str] = {}
        self._cache_lock = threading.Lock()

        # Per-run counters, reset by :meth:`reset_stats`.
        self._counts: Dict[EntityType, int] = {}
        self._skipped = 0

    # ── Detection ───────────────────────────────────────────────────────────

    def scan(self, text: str, *, all_entities: bool = False) -> List[Span]:
        """Find everything in ``text``, resolved to non-overlapping spans.

        ``all_entities=True`` ignores the policy's entity selection and
        confidence floor, which is what an audit wants: report what is there,
        not what would be replaced.
        """
        if not text:
            return []

        found: List[Span] = []
        for recognizer in self._recognizers:
            if not recognizer.applies_to(text):
                continue
            found.extend(recognizer.find(text))

        # Overlaps are resolved over *every* detection, including those the
        # policy will not act on, and only then filtered. The order matters:
        # filtering first would let the PERSON inside bob.reviewer@acme.com win
        # under a names-only policy, replacing the local part and publishing the
        # domain. A span the policy keeps must still shield what sits inside it.
        resolved = resolve_spans(found)
        if not all_entities:
            kept: List[Span] = []
            for span in resolved:
                if not self.policy.covers(span.entity):
                    continue
                if span.score < self.policy.min_score:
                    self._skipped += 1
                    continue
                kept.append(span)
            resolved = kept

        # Only people get joined across a space: "Alice" "Tester" is one person,
        # whereas two adjacent card numbers are two card numbers.
        if any(span.entity is EntityType.PERSON for span in resolved):
            resolved = merge_adjacent(
                [s for s in resolved if s.entity is EntityType.PERSON]
            ) + [s for s in resolved if s.entity is not EntityType.PERSON]
            resolved.sort(key=lambda span: span.start)
        return resolved

    # ── Replacement ─────────────────────────────────────────────────────────

    def _digest(self, entity: EntityType, value: str) -> str:
        """A keyed, truncated digest of one value — stable within this instance."""
        mac = hmac.new(
            self._key, f"{entity.value}\x00{value}".encode("utf-8"), hashlib.sha256
        )
        return mac.hexdigest()[:12]

    def _label_for(self, entity: EntityType) -> str:
        """Pick a role label that is not itself a name in this document.

        Falls back through the candidate list and, if every candidate collides,
        to a label the name matcher cannot match at all -- a trailing underscore
        makes the word boundary fail, which is ugly but correct, and reaching it
        requires a document reviewed by a Reviewer, a Person, a Participant and
        a Contributor at once.
        """
        candidates = _ROLE_LABELS.get(
            entity, (entity.value.replace("_", " ").title(),)
        )
        taken = self._roster_terms
        for candidate in candidates:
            if candidate.casefold() not in taken:
                return candidate
        return candidates[0] + "_"

    def _make_pseudonym(self, entity: EntityType, original: str, index: int) -> str:
        """Mint a stand-in. Called once per distinct value, under the vault lock."""
        if self.policy.pseudonym_style == "token":
            return f"{entity.value.lower()}-{self._digest(entity, original)[:6]}"
        label = self._labels.get(entity) or self._label_for(entity)
        if entity is EntityType.USERNAME:
            return f"@{label}{index}"
        return f"{label} {index}"

    def _pseudonym(self, entity: EntityType, original: str) -> str:
        """The stable stand-in for one value, minting it on first sight."""
        return self.vault.get_or_create(entity, original, self._make_pseudonym)

    def _canonical(self, span: Span) -> str:
        """The key a span's replacement is looked up under.

        For people this is the roster spelling, so ``Alice``, ``alice tester``
        and ``Alice Tester`` share one stand-in.  For everything else the
        matched text is the key, case-folded for types where case carries no
        meaning.
        """
        if span.entity is EntityType.PERSON and self._name_recognizer is not None:
            canonical = self._name_recognizer.canonical(span.text)
            if canonical is not None:
                return canonical
        if span.entity in {EntityType.EMAIL, EntityType.USERNAME, EntityType.URL}:
            return span.text.casefold()
        return span.text

    def _mask(self, value: str) -> str:
        """Replace all but the last ``mask_visible`` characters with asterisks.

        Separators are preserved so the shape stays recognisable: a masked card
        number still looks like a card number, which is the only reason to mask
        rather than redact.
        """
        visible = self.policy.mask_visible
        if visible <= 0:
            return "*" * len(value)

        keep_from = max(0, len(value) - visible)
        head = "".join(
            "*" if char.isalnum() else char for char in value[:keep_from]
        )
        return head + value[keep_from:]

    def replacement_for(self, span: Span) -> str:
        """The text that replaces one detected span."""
        strategy = self.policy.strategy_for(span.entity)
        if strategy is Strategy.KEEP:
            return span.text
        if strategy is Strategy.REMOVE:
            return ""
        if strategy is Strategy.REDACT:
            return f"[{span.entity.value}]"
        if strategy is Strategy.MASK:
            return self._mask(span.text)
        if strategy is Strategy.HASH:
            return f"{span.entity.value}_{self._digest(span.entity, self._canonical(span))[:8]}"
        return self._pseudonym(span.entity, self._canonical(span))

    def anonymize_text(self, text: str, *, cache: bool = True) -> str:
        """Return ``text`` with everything the policy covers replaced.

        One left-to-right pass over resolved spans, so a value is never
        substituted into a region that a later replacement then rewrites.
        """
        if not text:
            return text

        cacheable = cache and len(text) <= _CACHE_MAX_CHARS
        if cacheable:
            hit = self._cache.get(text)
            if hit is not None:
                return hit

        spans = self.scan(text)
        if not spans:
            if cacheable:
                with self._cache_lock:
                    self._cache[text] = text
            return text

        pieces: List[str] = []
        cursor = 0
        for span in spans:
            pieces.append(text[cursor:span.start])
            pieces.append(self.replacement_for(span))
            cursor = span.end
            self._counts[span.entity] = self._counts.get(span.entity, 0) + 1
        pieces.append(text[cursor:])

        result = "".join(pieces)
        if cacheable:
            with self._cache_lock:
                self._cache[text] = result
        return result

    def anonymize_name(self, name: str) -> str:
        """Replace a field that *is* a person's name, such as ``author``.

        Distinct from :meth:`anonymize_text` because there is nothing to detect:
        the whole value is the name, so a roster miss must not leave it intact.
        """
        cleaned = name.strip()
        if not cleaned:
            return name
        if not self.policy.covers(EntityType.PERSON):
            return name

        canonical = (
            self._name_recognizer.canonical(cleaned)
            if self._name_recognizer is not None
            else None
        ) or cleaned
        strategy = self.policy.strategy_for(EntityType.PERSON)
        if strategy is Strategy.REDACT:
            return f"[{EntityType.PERSON.value}]"
        if strategy is Strategy.REMOVE:
            return ""
        if strategy is Strategy.MASK:
            return self._mask(cleaned)
        if strategy is Strategy.HASH:
            return f"PERSON_{self._digest(EntityType.PERSON, canonical)[:8]}"
        self._counts[EntityType.PERSON] = self._counts.get(EntityType.PERSON, 0) + 1
        return self._pseudonym(EntityType.PERSON, canonical)

    def _initials_for(self, author: str) -> str:
        """Initials consistent with the replaced author name.

        Derived from the stand-in rather than from the real name, because
        keeping ``AT`` beside ``Reviewer 1`` would re-identify Alice Tester to
        anyone who has seen the original.
        """
        replaced = self.anonymize_name(author)
        letters = [part[0] for part in replaced.split() if part and part[0].isalnum()]
        digits = [char for char in replaced if char.isdigit()]
        initials = "".join(letters[:2]).upper()
        if digits:
            initials = (initials[:1] or "R") + "".join(digits[:2])
        return initials or "R"

    # ── Comments ────────────────────────────────────────────────────────────

    def anonymize_comment(self, comment: Comment) -> Comment:
        """Return a copy of one comment with the policy applied.

        The original is never mutated: an anonymised export and the real
        comments can coexist in one program without one contaminating the other.
        """
        changes: Dict[str, Any] = {}

        for field_name in COMMENT_TEXT_FIELDS:
            original = getattr(comment, field_name)
            replaced = self.anonymize_text(original)
            if replaced != original:
                changes[field_name] = replaced

        if self.policy.redact_authors and self.policy.covers(EntityType.PERSON):
            author = self.anonymize_name(comment.author)
            if author != comment.author:
                changes["author"] = author
                changes["initials"] = self._initials_for(comment.author)

        if self.policy.redact_document_name and comment.document_name:
            # The file name is a single opaque value, not prose: hash it so two
            # comments from one document still group together.
            digest = self._digest(EntityType.CUSTOM, comment.document_name)[:8]
            changes["document_name"] = f"document_{digest}"

        if not changes:
            return comment
        return replace(comment, **changes)

    def anonymize(self, comments: Iterable[Comment]) -> List[Comment]:
        """Apply the policy to a whole export."""
        return [self.anonymize_comment(comment) for comment in comments]

    # ── Verification and reversal ───────────────────────────────────────────

    def audit(self, texts: Iterable[str]) -> List[Span]:
        """Re-scan already-anonymised text and report anything still detectable.

        Placeholders this anonymizer issued are excluded: ``Reviewer 1`` is
        supposed to look like a person, and reporting it would drown the real
        findings.
        """
        issued = self.vault.placeholders()
        residual: List[Span] = []
        for text in texts:
            spans = self.scan(text)
            if not spans:
                continue
            # Locate the stand-ins in this text once, then drop any detection
            # that lands inside one. Comparing the matched text against the set
            # of placeholders is not enough: a detector can match a *part* of a
            # stand-in, which is a finding about the stand-in, not a leak.
            safe: List[Tuple[int, int]] = []
            for placeholder in issued:
                start = text.find(placeholder)
                while start != -1:
                    safe.append((start, start + len(placeholder)))
                    start = text.find(placeholder, start + len(placeholder))

            for span in spans:
                if any(start <= span.start and span.end <= end for start, end in safe):
                    continue
                residual.append(span)
        return residual

    def deanonymize(self, text: str) -> str:
        """Put the real values back — for a model's *answer*, on your machine.

        Only stand-ins that were actually issued are reversed, longest first so
        ``Reviewer 1`` cannot eat the front of ``Reviewer 11``.  Redacted and
        hashed values are not reversible by design and are left as they are.
        """
        result = text
        for placeholder in self.vault.placeholders():
            original = self.vault.original_for(placeholder)
            if original is not None and placeholder in result:
                result = result.replace(placeholder, original)
        return result

    # ── Run bookkeeping ─────────────────────────────────────────────────────

    def reset_stats(self) -> None:
        """Zero the per-run counters without discarding the vault."""
        self._counts = {}
        self._skipped = 0

    def stats(self) -> Tuple[Dict[EntityType, int], int]:
        """Replacements per entity type, and how many were skipped as too weak."""
        return dict(self._counts), self._skipped


# ─────────────────────────────────────────────────────────────────────────────
#  Module-level entry points
# ─────────────────────────────────────────────────────────────────────────────


def _roster(comments: Sequence[Comment]) -> List[str]:
    """Every distinct author in an export, in first-seen order."""
    return list(dict.fromkeys(comment.author for comment in comments if comment.author))


def anonymize_comments(
    comments: Iterable[Comment],
    policy: PolicyLike = True,
    *,
    anonymizer: Optional[Anonymizer] = None,
    key: Optional[bytes] = None,
    verify: bool = True,
) -> Tuple[List[Comment], AnonymizationReport]:
    """Anonymise an export and report what happened.

    The one-call form::

        clean, report = anonymize_comments(parser.to_comments(), "strict")
        print(report.summary())

    Parameters
    ----------
    policy:
        A :class:`~.policy.AnonymizationPolicy`, a preset name (``"strict"``,
        ``"balanced"``, ``"names_only"``, ``"secrets_only"``, ``"gdpr"``), or
        ``True`` for the balanced default.
    anonymizer:
        Reuse an existing one — the way to give several documents a single
        consistent set of stand-ins.  Its own policy wins over ``policy``.
    verify:
        Re-scan the output and report residual matches.  On by default; the
        cost is one extra scan of the text that changed.

    Returns
    -------
    The anonymised comments and an :class:`AnonymizationReport`.  The vault
    needed to reverse the pseudonyms is on the anonymizer, reachable as
    ``anonymizer.vault`` when you pass one in.
    """
    materialised = list(comments)
    engine = anonymizer or Anonymizer(
        policy, names=_roster(materialised), key=key
    )
    engine.reset_stats()

    result = engine.anonymize(materialised)

    changed = 0
    for before, after in zip(materialised, result):
        if before is not after:
            changed += sum(
                1
                for name in (*COMMENT_TEXT_FIELDS, "author", "initials", "document_name")
                if getattr(before, name) != getattr(after, name)
            )

    residual: Tuple[Span, ...] = ()
    if verify:
        residual = tuple(
            engine.audit(
                text
                for comment in result
                for text in (comment.text, comment.referenced_text, comment.author)
            )
        )

    counts, skipped = engine.stats()
    report = AnonymizationReport(
        comments=len(materialised),
        fields_changed=changed,
        replacements=sum(counts.values()),
        entity_counts=counts,
        skipped_low_confidence=skipped,
        residual=residual,
        pseudonyms=len(engine.vault),
        policy_name=_policy_name(engine.policy),
    )
    return result, report


def scan_comments(
    comments: Iterable[Comment],
    *,
    names: Optional[Iterable[str]] = None,
    policy: PolicyLike = True,
) -> PrivacyScan:
    """Report what private information an export contains, changing nothing.

    The pre-flight check: run it before an export leaves the machine, decide
    from the answer whether it needs redacting.  Samples in the result are
    reduced to a type and a length, so the scan report can itself be shared.
    """
    materialised = list(comments)
    engine = Anonymizer(
        policy, names=names if names is not None else _roster(materialised)
    )

    findings: Dict[EntityType, int] = {}
    examples: Dict[EntityType, List[str]] = {}
    for comment in materialised:
        for text in (comment.text, comment.referenced_text):
            for span in engine.scan(text, all_entities=True):
                findings[span.entity] = findings.get(span.entity, 0) + 1
                samples = examples.setdefault(span.entity, [])
                if len(samples) < 3:
                    samples.append(f"<{len(span.text)} chars>")

    return PrivacyScan(
        comments=len(materialised),
        findings=findings,
        examples={entity: tuple(values) for entity, values in examples.items()},
    )


def _policy_name(policy: AnonymizationPolicy) -> str:
    """Name a policy by the preset it equals, or ``custom``."""
    from .policy import _PRESETS

    for name, factory in _PRESETS.items():
        if factory() == policy:
            return name
    return "custom"
