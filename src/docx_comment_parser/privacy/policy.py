"""What to do with what was found: strategies, field selection, presets.

Detection answers "where is the private information".  A policy answers the two
questions that follow: *which* findings to act on, and *how* to replace them.

The how matters more than it looks.  Deleting a name and deleting an email
address produce a document a model can still read; replacing both with the same
opaque blob produces one it cannot.  So the strategies here are graded, and the
default preset picks a different one per entity type:

``PSEUDONYMIZE``
    Replace with a stable stand-in — ``Reviewer 1``, ``person-4f2a``.  The model
    can still follow who said what, which is exactly what review analysis needs,
    and the mapping back is held locally.  This is the default for people.

``REDACT``
    Replace with a type label — ``[EMAIL]``.  The model knows an address was
    there without learning it.  The default for contact details and identifiers.

``MASK``
    Keep a few characters — ``a***@***.com``, ``**** **** **** 1111``.  Useful
    when a human still has to recognise the value in the output.

``HASH``
    Replace with a keyed digest — ``EMAIL_9f2ab41c``.  Two occurrences of the
    same value collide deliberately, so joins across documents still work,
    without the value itself travelling.

``REMOVE``
    Delete the characters entirely.

``KEEP``
    Leave it alone — how you switch one entity type back on inside an otherwise
    strict policy.

Credentials are never pseudonymised or masked by any preset.  A partially
visible API key is still a leaked API key.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field, replace
from enum import Enum
from typing import (
    Callable,
    FrozenSet,
    Iterable,
    Mapping,
    Optional,
    Tuple,
    Union,
)

from .entities import EntityType
from .validation import (
    PatternIssue,
    PatternLike,
    validate_patterns,
    validate_values,
)

__all__ = [
    "Strategy",
    "AnonymizationPolicy",
    "PolicyLike",
    "resolve_policy",
    "make_policy",
    "PRESET_NAMES",
    "COMMENT_TEXT_FIELDS",
]


class Strategy(str, Enum):
    """How a detected value is replaced."""

    PSEUDONYMIZE = "pseudonymize"
    REDACT = "redact"
    MASK = "mask"
    HASH = "hash"
    REMOVE = "remove"
    KEEP = "keep"

    def __str__(self) -> str:  # pragma: no cover - trivial
        return self.value


#: Fields of :class:`~docx_comment_parser.models.Comment` that hold free text a
#: person could be named in. ``author`` and ``initials`` are handled separately
#: (they are a whole name, not text containing one), and the rest of the schema
#: is ids and flags that carry no personal information.
COMMENT_TEXT_FIELDS: Tuple[str, ...] = ("text", "referenced_text")


#: Entity types where any partial disclosure is a full disclosure. A policy that
#: asks to mask or pseudonymise one of these is upgraded to REDACT, loudly
#: documented rather than silently obeyed.
_NEVER_PARTIAL: FrozenSet[EntityType] = frozenset(
    {
        EntityType.PRIVATE_KEY,
        EntityType.API_KEY,
        EntityType.JWT,
        EntityType.SECRET,
    }
)


@dataclass(frozen=True)
class AnonymizationPolicy:
    """The complete configuration of one anonymisation run.

    Frozen, so a policy can be shared between threads and stored as a constant.
    Use :meth:`with_` to derive a variant.

    Attributes
    ----------
    strategies:
        Per-entity overrides.  Anything absent uses ``default_strategy``.
    entities:
        The entity types to act on at all.  ``None`` means every type a
        recognizer reports.  Listing types here rather than setting them to
        ``KEEP`` also skips the work of scanning for them.
    min_score:
        Confidence floor.  Detections below it are reported in the audit but not
        replaced — the setting that trades recall for precision.
    redact_authors:
        Replace the ``author`` and ``initials`` columns too.  On by default:
        the author column is the single most identifying field in the export.
    redact_document_name:
        Replace the ``document_name`` column.  Off by default — file names are
        usually project names, and losing them makes a multi-document export
        unreadable — but on in :meth:`strict`.
    mask_visible:
        How many trailing characters :attr:`Strategy.MASK` leaves visible.
    custom_terms:
        Exact values to treat as private — codenames, customer names, account
        numbers.  Matched case-insensitively, on word boundaries, with any run
        of whitespace standing for a space.
    custom_patterns:
        Regular expressions to treat as private: a bare regex, ``(name,
        regex)``, or ``(name, regex, entity)`` to report the match as a specific
        :class:`~.entities.EntityType`.  Every pattern is checked when the
        policy is built; an unusable one raises
        :class:`~.validation.InvalidPatternError` naming the problem.
    organizations / locations:
        Literal company and place names, reported as ``ORGANIZATION`` and
        ``LOCATION`` so their own strategies apply.
    extra_names:
        People to redact who are not comment authors — named in the text but
        never commenting themselves.
    detect_mentions:
        Treat ``@handle`` as a username even when the handle is not on the
        roster.
    pseudonym_style:
        ``"role"`` gives ``Reviewer 1`` / ``Person 2``; ``"token"`` gives
        ``person-4f2a91``.  Role reads better in a prompt; token is
        unmistakably synthetic, which matters when the output is shown to a
        human who might otherwise think ``Reviewer 1`` is a real account.
    """

    default_strategy: Strategy = Strategy.REDACT
    strategies: Mapping[EntityType, Strategy] = field(default_factory=dict)
    entities: Optional[FrozenSet[EntityType]] = None
    min_score: float = 0.5

    redact_authors: bool = True
    redact_document_name: bool = False

    mask_visible: int = 4
    custom_terms: Tuple[str, ...] = ()
    custom_patterns: Tuple[Tuple[str, str, EntityType], ...] = ()
    extra_names: Tuple[str, ...] = ()
    detect_mentions: bool = True
    pseudonym_style: str = "role"
    organizations: Tuple[str, ...] = ()
    locations: Tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not 0.0 <= self.min_score <= 1.0:
            raise ValueError(f"min_score must be in 0.0–1.0, got {self.min_score}")
        if self.mask_visible < 0:
            raise ValueError("mask_visible cannot be negative")
        if self.pseudonym_style not in {"role", "token"}:
            raise ValueError(
                f"pseudonym_style must be 'role' or 'token', got {self.pseudonym_style!r}"
            )
        # Validate and normalise the caller's own values here, at configuration
        # time: a bad regex found mid-export is a half-written file and a bare
        # re.error, while one found here is a sentence naming the problem.
        # object.__setattr__ because the dataclass is frozen; the normalised
        # forms are what make two equivalent policies compare equal.
        patterns, _ = validate_patterns(self.custom_patterns)
        object.__setattr__(self, "custom_patterns", patterns)
        object.__setattr__(self, "custom_terms", validate_values(self.custom_terms))
        object.__setattr__(
            self, "organizations", validate_values(self.organizations, "organization")
        )
        object.__setattr__(self, "locations", validate_values(self.locations, "location"))
        object.__setattr__(self, "extra_names", tuple(self.extra_names))

    def pattern_warnings(self) -> Tuple[PatternIssue, ...]:
        """Non-fatal concerns about the custom patterns, such as a regex that
        also matches ordinary words or one prone to slow backtracking."""
        _, warnings = validate_patterns(self.custom_patterns)
        return warnings

    # ── Lookup ──────────────────────────────────────────────────────────────

    def strategy_for(self, entity: EntityType) -> Strategy:
        """The strategy to apply to one entity type, after safety upgrades."""
        chosen = self.strategies.get(entity, self.default_strategy)
        if entity in _NEVER_PARTIAL and chosen in {
            Strategy.MASK,
            Strategy.PSEUDONYMIZE,
        }:
            # A masked secret is a leaked secret; a pseudonymised one is a
            # secret with a lookup table pointing back at it. Neither is a
            # defensible reading of "protect this", so both become REDACT.
            return Strategy.REDACT
        return chosen

    def covers(self, entity: EntityType) -> bool:
        """True when this policy acts on ``entity`` at all."""
        if self.entities is not None and entity not in self.entities:
            return False
        return self.strategy_for(entity) is not Strategy.KEEP

    def with_(self, **changes: object) -> "AnonymizationPolicy":
        """Return a copy with the given fields changed."""
        return replace(self, **changes)  # type: ignore[arg-type]

    def with_values(
        self,
        values: Iterable[str],
        entity: Union[str, EntityType] = EntityType.CUSTOM,
    ) -> "AnonymizationPolicy":
        """Return a copy that also masks these exact values.

        ``entity`` decides how they are reported and replaced: ``PERSON`` adds
        them to the names (pseudonymised like a reviewer), ``ORGANIZATION`` and
        ``LOCATION`` to those lists, anything else to the custom terms.
        """
        kind = entity if isinstance(entity, EntityType) else EntityType(str(entity).upper())
        added = tuple(values)
        if kind is EntityType.PERSON:
            return self.with_(extra_names=(*self.extra_names, *added))
        if kind is EntityType.ORGANIZATION:
            return self.with_(organizations=(*self.organizations, *added))
        if kind is EntityType.LOCATION:
            return self.with_(locations=(*self.locations, *added))
        if kind is not EntityType.CUSTOM:
            return self.with_patterns(
                (f"value-{index}", _literal_regex(value), kind)
                for index, value in enumerate(validate_values(added), start=1)
            )
        return self.with_(custom_terms=(*self.custom_terms, *added))

    def with_patterns(self, patterns: Iterable[PatternLike]) -> "AnonymizationPolicy":
        """Return a copy that also masks text matching these regular expressions.

        Each entry is a regex, ``(name, regex)`` or ``(name, regex, entity)``.
        Raises :class:`~.validation.InvalidPatternError` listing every pattern
        that cannot be used — check them up front with
        :func:`~.validation.check_pattern` to report problems interactively.
        """
        return self.with_(custom_patterns=(*self.custom_patterns, *patterns))

    # ── Presets ─────────────────────────────────────────────────────────────

    @classmethod
    def balanced(cls) -> "AnonymizationPolicy":
        """The default: people become stable stand-ins, identifiers disappear.

        Chosen so a review-analysis prompt still works.  "Reviewer 1 raised this
        three times and Reviewer 2 disagreed" is a useful sentence; the same
        sentence with both names blanked is not.
        """
        return cls(
            default_strategy=Strategy.REDACT,
            strategies={
                EntityType.PERSON: Strategy.PSEUDONYMIZE,
                EntityType.USERNAME: Strategy.PSEUDONYMIZE,
                EntityType.ORGANIZATION: Strategy.PSEUDONYMIZE,
                EntityType.URL: Strategy.KEEP,
            },
            min_score=0.6,
        )

    @classmethod
    def strict(cls) -> "AnonymizationPolicy":
        """Everything identifying goes, including file names and URLs.

        The setting for sending a document to a model you do not control:
        detections are acted on down to a low confidence floor, and nothing is
        left partially visible.
        """
        return cls(
            default_strategy=Strategy.REDACT,
            strategies={
                EntityType.PERSON: Strategy.PSEUDONYMIZE,
                EntityType.USERNAME: Strategy.PSEUDONYMIZE,
            },
            min_score=0.4,
            redact_authors=True,
            redact_document_name=True,
            pseudonym_style="token",
        )

    @classmethod
    def names_only(cls) -> "AnonymizationPolicy":
        """Just the people. Technical and financial content is left alone.

        What most users mean by "anonymise this before I paste it into a chat".

        Email addresses are included even though they are not names, because
        ``bob.reviewer@acme.com`` identifies Bob Reviewer at least as well as
        the word "Bob" does.  Leaving them would make the preset a promise it
        does not keep.
        """
        return cls(
            default_strategy=Strategy.KEEP,
            strategies={
                EntityType.PERSON: Strategy.PSEUDONYMIZE,
                EntityType.USERNAME: Strategy.PSEUDONYMIZE,
                EntityType.EMAIL: Strategy.REDACT,
            },
            entities=frozenset(
                {EntityType.PERSON, EntityType.USERNAME, EntityType.EMAIL}
            ),
            min_score=0.5,
        )

    @classmethod
    def secrets_only(cls) -> "AnonymizationPolicy":
        """Only credentials and financial identifiers; names stay readable.

        For an internal pipeline where the people are colleagues and the real
        risk is a pasted API key ending up in a model provider's logs.
        """
        secret_types = frozenset(
            {
                EntityType.PRIVATE_KEY,
                EntityType.API_KEY,
                EntityType.JWT,
                EntityType.SECRET,
                EntityType.CREDIT_CARD,
                EntityType.IBAN,
                EntityType.SSN,
                EntityType.NATIONAL_ID,
                EntityType.PASSPORT,
            }
        )
        return cls(
            default_strategy=Strategy.REDACT,
            entities=secret_types,
            min_score=0.5,
            redact_authors=False,
        )

    @classmethod
    def gdpr(cls) -> "AnonymizationPolicy":
        """Pseudonymisation in the sense Article 4(5) uses the word.

        Personal data is replaced with stand-ins that cannot be attributed to a
        person without the mapping, and the mapping is kept separately — which
        is what :class:`~.anonymizer.Vault` is and why it never leaves the
        process unless you write it out yourself.  Direct identifiers are
        removed outright rather than pseudonymised, since a reversible email
        address is still personal data.
        """
        return cls(
            default_strategy=Strategy.REDACT,
            strategies={
                EntityType.PERSON: Strategy.PSEUDONYMIZE,
                EntityType.USERNAME: Strategy.PSEUDONYMIZE,
                EntityType.ORGANIZATION: Strategy.PSEUDONYMIZE,
                EntityType.LOCATION: Strategy.REDACT,
            },
            min_score=0.5,
            redact_authors=True,
            redact_document_name=True,
            pseudonym_style="token",
        )


#: What every ``anonymize=`` parameter in this package accepts: a policy, the
#: name of a preset, or ``True`` for the balanced default.
PolicyLike = Union[bool, str, AnonymizationPolicy]


#: Preset name → constructor. An explicit table rather than a getattr on the
#: class: a name typed into a CLI flag must never be able to reach an arbitrary
#: attribute of AnonymizationPolicy.
_PRESETS: Mapping[str, Callable[[], AnonymizationPolicy]] = {
    "balanced": AnonymizationPolicy.balanced,
    "strict": AnonymizationPolicy.strict,
    "names_only": AnonymizationPolicy.names_only,
    "secrets_only": AnonymizationPolicy.secrets_only,
    "gdpr": AnonymizationPolicy.gdpr,
}

PRESET_NAMES: Tuple[str, ...] = tuple(sorted(_PRESETS))
"""Every accepted preset name, for CLI help and validation."""


def resolve_policy(policy: PolicyLike) -> AnonymizationPolicy:
    """Accept a policy, a preset name, or ``True`` and return a policy.

    Lets every public entry point take ``anonymize="strict"`` or
    ``anonymize=True`` without each of them re-implementing the lookup.
    """
    if isinstance(policy, AnonymizationPolicy):
        return policy
    if policy is True:
        return AnonymizationPolicy.balanced()
    if isinstance(policy, str):
        name = policy.replace("-", "_").casefold()
        if name not in _PRESETS:
            valid = ", ".join(sorted(_PRESETS))
            raise ValueError(f"unknown policy preset {policy!r}; choose one of: {valid}")
        return _PRESETS[name]()
    raise TypeError(f"cannot interpret {policy!r} as an anonymisation policy")


def _literal_regex(value: str) -> str:
    """A case-insensitive, word-bounded regex for one literal value.

    Spaces inside the value match any run of whitespace, so a value typed with
    one space still catches a line break or a non-breaking space in a document.
    """
    body = r"\s+".join(re.escape(part) for part in value.split())
    return rf"(?i)(?<!\w){body}(?!\w)"


def make_policy(
    base: PolicyLike = True,
    *,
    values: Iterable[str] = (),
    patterns: Iterable[PatternLike] = (),
    names: Iterable[str] = (),
    organizations: Iterable[str] = (),
    locations: Iterable[str] = (),
) -> AnonymizationPolicy:
    """A preset, plus the caller's own values and patterns, in one call.

    ::

        policy = make_policy(
            "balanced",
            values=["Project Falcon", "ACME-4471"],
            patterns=[("ticket", r"\\bTCK-\\d{6}\\b")],
            organizations=["Acme Corp"],
        )

    Every pattern is validated before the policy exists; an unusable one raises
    :class:`~.validation.InvalidPatternError` describing each problem.
    """
    policy = resolve_policy(base)
    return policy.with_(
        custom_terms=(*policy.custom_terms, *values),
        custom_patterns=(*policy.custom_patterns, *patterns),
        extra_names=(*policy.extra_names, *names),
        organizations=(*policy.organizations, *organizations),
        locations=(*policy.locations, *locations),
    )
