"""Checking a caller's own patterns and values before they are trusted.

A custom regex is the one input to the privacy layer that the library did not
write, and the ways it goes wrong are all quiet.  A pattern that does not
compile fails mid-export with a bare ``re.error``.  A pattern that can match the
empty string "matches" at every position and redacts nothing — or, with
``REMOVE``, everything.  ``\\w+`` compiles, matches, and replaces every word in
the review.  ``(a+)+`` compiles, matches, and takes minutes on one long comment.

So every pattern is checked once, when it is configured, and the result is a
list of :class:`PatternIssue` objects that say what is wrong in words, with the
position of the problem where there is one.  Errors stop the configuration;
warnings are surfaced and the pattern is still used, because a heuristic must
never be allowed to refuse a pattern that is in fact fine.

The same goes for literal values: an empty string or a single character is
rejected, because "mask every ``a``" is never what was meant.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable, List, Optional, Sequence, Tuple, Union

from .entities import EntityType

__all__ = [
    "PatternIssue",
    "InvalidPatternError",
    "check_pattern",
    "validate_patterns",
    "validate_values",
]

#: Ordinary review prose containing nothing private. A pattern that covers a
#: large share of it would redact the review itself.
_NEUTRAL_SAMPLE = (
    "The reviewer suggested that we update the summary and check the figures "
    "in section two before the meeting, because the wording is unclear."
)

#: Share of the neutral sample a pattern may cover before it is refused.
_MAX_NEUTRAL_COVERAGE = 0.5

#: A group that contains a quantifier and is itself quantified -- ``(a+)+``,
#: ``(\w*)*``, ``(x+){2,}``. The classic catastrophic-backtracking shape.
_NESTED_QUANTIFIER = re.compile(
    r"\((?:[^()\\]|\\.)*(?:[+*]|\{\d+,\d*\})(?:[^()\\]|\\.)*\)(?:[+*]|\{\d+,\d*\})"
)

#: The shortest literal value accepted. One character is a letter, not a value.
MIN_VALUE_LENGTH = 2

#: What a custom pattern may be given as: ``regex``, ``(name, regex)`` or
#: ``(name, regex, entity)``.
PatternLike = Union[str, Tuple[str, str], Tuple[str, str, Union[str, EntityType]]]


@dataclass(frozen=True)
class PatternIssue:
    """One problem found in a caller-supplied pattern or value."""

    name: str
    pattern: str
    message: str
    severity: str = "error"
    """``"error"`` refuses the pattern; ``"warning"`` reports and keeps it."""

    position: Optional[int] = None
    """Character offset of the problem inside ``pattern``, when known."""

    @property
    def is_error(self) -> bool:
        return self.severity == "error"

    def describe(self) -> str:
        """A readable explanation, with a caret under the bad character."""
        label = f"pattern {self.name!r}" if self.name else "pattern"
        text = f"{label}: {self.message}"
        if self.position is not None:
            text += f"\n    {self.pattern}\n    {' ' * self.position}^"
        return text

    def __str__(self) -> str:
        return self.describe()


class InvalidPatternError(ValueError):
    """Raised when one or more custom patterns or values cannot be used.

    Carries every problem, not just the first, so a caller fixing a list of
    patterns sees all of it in one go.
    """

    def __init__(self, issues: Sequence[PatternIssue]) -> None:
        self.issues: Tuple[PatternIssue, ...] = tuple(issues)
        lines = [f"{len(self.issues)} invalid custom pattern(s) or value(s):"]
        lines.extend(f"  - {issue.describe()}" for issue in self.issues)
        super().__init__("\n".join(lines))


def check_pattern(pattern: object, name: str = "") -> List[PatternIssue]:
    """Return every problem with one regular expression; empty when it is fine.

    Checks, in order: that it is a non-empty string, that it compiles, that it
    cannot match empty text, that it does not cover ordinary prose, and that it
    has no nested quantifier.  The last two are warnings or errors depending on
    how certain the evidence is.
    """
    if not isinstance(pattern, str):
        return [PatternIssue(name, repr(pattern), "must be a string")]
    if not pattern.strip():
        return [PatternIssue(name, pattern, "is empty")]

    try:
        compiled = re.compile(pattern)
    except re.error as exc:
        # ``exc.msg`` is the bare reason; ``str(exc)`` repeats the position.
        reason = getattr(exc, "msg", None) or str(exc)
        return [
            PatternIssue(
                name, pattern, f"is not a valid regular expression ({reason})",
                position=getattr(exc, "pos", None),
            )
        ]

    issues: List[PatternIssue] = []
    # search("") misses zero-width patterns that need text around them, such
    # as \b or (?=x); a zero-length match anywhere in real text catches those.
    if compiled.search("") is not None or any(
        match.start() == match.end() for match in compiled.finditer(_NEUTRAL_SAMPLE)
    ):
        issues.append(
            PatternIssue(
                name, pattern,
                "can match empty text, so it would fire at every position. "
                "Use + instead of *, or require at least one character",
            )
        )
        return issues

    covered = sum(match.end() - match.start() for match in compiled.finditer(_NEUTRAL_SAMPLE))
    share = covered / len(_NEUTRAL_SAMPLE)
    if share >= _MAX_NEUTRAL_COVERAGE:
        issues.append(
            PatternIssue(
                name, pattern,
                f"matches {share:.0%} of ordinary text, so it would redact the "
                "review itself. Make it more specific",
            )
        )
    elif covered:
        sample = compiled.search(_NEUTRAL_SAMPLE)
        assert sample is not None
        issues.append(
            PatternIssue(
                name, pattern,
                f"also matches ordinary words (e.g. {sample.group()!r}); check "
                "that this is intended",
                severity="warning",
            )
        )

    nested = _NESTED_QUANTIFIER.search(pattern)
    if nested is not None:
        issues.append(
            PatternIssue(
                name, pattern,
                "has a quantified group that contains a quantifier, which can "
                "make matching extremely slow on long text",
                severity="warning",
                position=nested.start(),
            )
        )
    return issues


def _entity(value: Union[str, EntityType], name: str, pattern: str) -> EntityType:
    """Coerce an entity given by name, case-insensitively."""
    if isinstance(value, EntityType):
        return value
    try:
        return EntityType(str(value).strip().upper())
    except ValueError:
        valid = ", ".join(entity.value for entity in EntityType)
        raise InvalidPatternError(
            [PatternIssue(name, pattern, f"unknown entity type {value!r}; use one of: {valid}")]
        ) from None


def _normalise(spec: PatternLike, index: int) -> Tuple[str, str, Union[str, EntityType]]:
    """Accept the three spellings of a pattern and return ``(name, regex, entity)``."""
    if isinstance(spec, str):
        return f"pattern-{index}", spec, EntityType.CUSTOM
    if isinstance(spec, (tuple, list)) and len(spec) in (2, 3):
        name = str(spec[0]) if spec[0] else f"pattern-{index}"
        entity = spec[2] if len(spec) == 3 else EntityType.CUSTOM
        return name, spec[1], entity
    raise InvalidPatternError(
        [PatternIssue("", repr(spec), "must be a regex string, (name, regex) or (name, regex, entity)")]
    )


def validate_patterns(
    patterns: Iterable[PatternLike],
) -> Tuple[Tuple[Tuple[str, str, EntityType], ...], Tuple[PatternIssue, ...]]:
    """Check a set of patterns and return them normalised, plus any warnings.

    Raises :class:`InvalidPatternError` listing *every* error when at least one
    pattern is unusable.  On success returns ``(patterns, warnings)`` where each
    pattern is ``(name, regex, entity)``.
    """
    normalised: List[Tuple[str, str, EntityType]] = []
    errors: List[PatternIssue] = []
    warnings: List[PatternIssue] = []

    for index, spec in enumerate(patterns, start=1):
        try:
            name, regex, entity = _normalise(spec, index)
            resolved = _entity(entity, name, str(regex))
        except InvalidPatternError as exc:
            errors.extend(exc.issues)
            continue
        found = check_pattern(regex, name)
        errors.extend(issue for issue in found if issue.is_error)
        warnings.extend(issue for issue in found if not issue.is_error)
        normalised.append((name, regex, resolved))

    if errors:
        raise InvalidPatternError(errors)
    return tuple(normalised), tuple(warnings)


def validate_values(values: Iterable[object], label: str = "value") -> Tuple[str, ...]:
    """Check literal values to mask and return them cleaned and de-duplicated.

    Whitespace inside a value is normalised to single spaces; the recognizer
    matches any run of whitespace there, so ``"Project  Falcon"`` and a
    non-breaking space still count as the same value.
    """
    cleaned: List[str] = []
    errors: List[PatternIssue] = []
    for value in values:
        if not isinstance(value, str):
            errors.append(PatternIssue("", repr(value), f"{label} must be a string"))
            continue
        text = " ".join(value.split())
        if len(text) < MIN_VALUE_LENGTH:
            errors.append(
                PatternIssue(
                    "", value,
                    f"{label} {value!r} is shorter than {MIN_VALUE_LENGTH} characters "
                    "and would mask ordinary text",
                )
            )
            continue
        cleaned.append(text)
    if errors:
        raise InvalidPatternError(errors)
    return tuple(dict.fromkeys(cleaned))
