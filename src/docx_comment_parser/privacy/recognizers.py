"""Detectors: the part that actually finds private information in text.

Every detector is a :class:`Recognizer` — a name, an entity type, and a
``find(text)`` that yields :class:`~.entities.Span` objects.  That is the whole
contract, so a caller who needs something this module does not ship (a
Presidio analyzer, a spaCy NER model, an internal customer-id format) can write
one object and hand it to the policy; nothing here needs to change.

Two design decisions run through the whole module.

**Validate, do not just match.**  A sixteen-digit number is not a credit card;
one that passes Luhn probably is.  A twenty-two character string is not an IBAN;
one that passes the mod-97 check is.  Shape-only matches are still reported, but
with a confidence below 1.0, so a policy can set a floor and keep them out.
This is what keeps a redactor from mangling every build number and order
reference in a document until people stop trusting it.

**Names come from the document, not from a gazetteer.**  A shipped list of first
names is both enormous and wrong — it misses ``Νίκος`` and it redacts a section
about a ``Mark``.  The reviewers of a document are known exactly: the parser
read them out of the file.  :class:`NameRecognizer` is built from that roster,
so recall on the people who actually matter is total, and precision does not
depend on guessing.

Scanning cost is kept off the hot path by a cheap gate: each recognizer declares
the characters a match cannot exist without (``@`` for an email, a digit for a
card), and a text lacking them skips the regex entirely.  On a corpus of ordinary
review comments that skips most of the work.
"""

from __future__ import annotations

import re
from typing import (
    Callable,
    Dict,
    FrozenSet,
    Iterable,
    Iterator,
    List,
    Optional,
    Pattern,
    Sequence,
    Tuple,
    Union,
)

from .entities import EntityType, Span

__all__ = [
    "Recognizer",
    "RegexRecognizer",
    "NameRecognizer",
    "TermRecognizer",
    "default_recognizers",
    "luhn_valid",
    "iban_valid",
]


# ─────────────────────────────────────────────────────────────────────────────
#  Validators
# ─────────────────────────────────────────────────────────────────────────────


def luhn_valid(digits: str) -> bool:
    """True when ``digits`` satisfies the Luhn checksum used by payment cards.

    Non-digits are ignored, so ``4111 1111 1111 1111`` and ``4111-1111-…`` both
    work.  Cards are 12–19 digits; anything outside that is rejected before the
    checksum, which stops a long invoice number from accidentally passing.
    """
    stripped = [int(char) for char in digits if char.isdigit()]
    if not 12 <= len(stripped) <= 19:
        return False

    total = 0
    # Double every second digit counting from the right, per ISO/IEC 7812-1.
    for index, value in enumerate(reversed(stripped)):
        if index % 2:
            value *= 2
            if value > 9:
                value -= 9
        total += value
    return total % 10 == 0


def iban_valid(candidate: str) -> bool:
    """True when ``candidate`` passes the ISO 13616 mod-97 check.

    The four leading characters move to the end, letters become numbers
    (A=10 … Z=35), and the resulting integer must be ≡ 1 (mod 97).  The integer
    is reduced in chunks rather than built whole: a 34-character IBAN would
    otherwise create a 40-digit int for no reason.
    """
    text = "".join(candidate.split()).replace("-", "").upper()
    if not 15 <= len(text) <= 34 or not text[:2].isalpha() or not text[2:4].isdigit():
        return False
    if not text.isalnum():
        return False

    rearranged = text[4:] + text[:4]
    remainder = 0
    for char in rearranged:
        if char.isdigit():
            remainder = (remainder * 10 + int(char)) % 97
        elif char.isalpha():
            remainder = (remainder * 100 + (ord(char) - 55)) % 97
        else:  # pragma: no cover - guarded by isalnum above
            return False
    return remainder == 1


def _ipv4_valid(candidate: str) -> bool:
    """True when every dotted part is 0–255 with no leading zeros.

    Leading zeros are rejected because ``1.2.3.010`` is not how addresses are
    written, while ``10.1.2024.3``-style date fragments are rejected by range.
    """
    parts = candidate.split(".")
    if len(parts) != 4:
        return False
    for part in parts:
        if not part.isdigit() or (len(part) > 1 and part[0] == "0") or len(part) > 3:
            return False
        if int(part) > 255:
            return False
    return True


def _phone_plausible(candidate: str) -> bool:
    """True when a separated run of digits could really be a phone number.

    E.164 allows at most 15 digits and no national scheme uses fewer than 7, so
    the count alone rejects most of what the shape pattern would otherwise
    collect — version strings, dates written ``12.03.2024``, and the four-group
    identifiers that appear throughout technical review comments.
    """
    digits = sum(1 for char in candidate if char.isdigit())
    if not 7 <= digits <= 15:
        return False

    # Dot-separated digit groups with no other phone marker are almost always
    # something else -- a build number, a dotted date, a clause reference. A
    # real phone number written that way carries an international prefix or
    # mixes in a space or a hyphen, so requiring one of those costs little
    # recall and removes the false positives that matter.
    if "." in candidate and not any(
        marker in candidate for marker in ("+", " ", "-", "(")
    ):
        return False
    return True


def _ssn_valid(candidate: str) -> bool:
    """Reject the SSN shapes the US Social Security Administration never issues.

    Area 000, 666 and 900–999, group 00 and serial 0000 are all invalid, and
    excluding them removes most of the false positives that a bare
    ``\\d{3}-\\d{2}-\\d{4}`` collects from part numbers and phone extensions.
    """
    digits = "".join(char for char in candidate if char.isdigit())
    if len(digits) != 9:
        return False
    area, group, serial = digits[:3], digits[3:5], digits[5:]
    if area in {"000", "666"} or area[0] == "9":
        return False
    return group != "00" and serial != "0000"


# ─────────────────────────────────────────────────────────────────────────────
#  Recognizer base
# ─────────────────────────────────────────────────────────────────────────────


class Recognizer:
    """Base class for detectors. Subclass and implement :meth:`find`.

    Implementations must be safe to reuse across texts and across threads: keep
    compiled patterns on the instance and nothing else.
    """

    name: str = "recognizer"
    entity: EntityType = EntityType.CUSTOM
    trigger: FrozenSet[str] = frozenset()
    """Characters a match cannot exist without. Empty means "always scan"."""

    def applies_to(self, text: str) -> bool:
        """Cheap pre-filter: can this text possibly contain a match?"""
        if not self.trigger:
            return True
        return any(char in text for char in self.trigger)

    def find(self, text: str) -> Iterator[Span]:  # pragma: no cover - abstract
        raise NotImplementedError


class RegexRecognizer(Recognizer):
    """A detector built from one regular expression and an optional validator.

    The validator receives the matched text and returns ``True`` to keep the
    span.  When ``validated_score`` is given, a match that passes the validator
    is reported at that confidence and one that fails is reported at ``score``
    instead of being dropped — which is how "shape looks right, checksum does
    not" is expressed without either hiding it or over-claiming it.
    """

    def __init__(
        self,
        name: str,
        entity: EntityType,
        pattern: str,
        *,
        flags: int = 0,
        score: float = 1.0,
        validator: Optional[Callable[[str], bool]] = None,
        validated_score: Optional[float] = None,
        group: Union[int, str] = 0,
        trigger: Iterable[str] = (),
    ) -> None:
        self.name = name
        self.entity = entity
        self.pattern: Pattern[str] = re.compile(pattern, flags)
        self.score = score
        self.validator = validator
        self.validated_score = validated_score
        # Specs are written with a named group because that is readable; the
        # scan loop wants an index because that is what match.span() takes.
        # Resolve once, here, rather than per match.
        self.group: int = (
            self.pattern.groupindex[group] if isinstance(group, str) else group
        )
        self.trigger = frozenset(trigger)

    def _shorten(self, text: str, match: "re.Match[str]") -> Optional[Tuple[int, int]]:
        """The longest prefix of a rejected match that is itself a valid match.

        Only prefixes ending on a token boundary are tried — a shortened value
        must not stop in the middle of a word or a digit group — and only for
        whole-match patterns, where the prefix is the value.
        """
        if self.group != 0 or self.validator is None:
            return None
        start, end = match.span()
        for cut in range(end - 1, start, -1):
            if not text[cut - 1].isalnum() or text[cut].isalnum():
                continue
            if self.pattern.fullmatch(text, start, cut) and self.validator(text[start:cut]):
                return start, cut
        return None

    def find(self, text: str) -> Iterator[Span]:
        position = 0
        length = len(text)
        while position <= length:
            match = self.pattern.search(text, position)
            if match is None:
                return
            # finditer would resume after a match even when the validator then
            # throws it away. A shape that starts one character too early --
            # the "3" of "version 1.2.3" glued to a card number after it --
            # would then hide the real value for good. A rejected match resumes
            # the search one character later instead.
            position = match.end() if match.end() > match.start() else match.start() + 1
            start, end = match.span(self.group)
            if start < 0:  # pragma: no cover - optional group did not take part
                continue
            matched = text[start:end]

            score = self.score
            if self.validator is not None:
                ok = bool(self.validator(matched))
                if self.validated_score is None and not ok:
                    # The match may be a real value that ran on into its
                    # neighbour ("+44 20 7946 0958 4111 1111 ..."); offer the
                    # longest valid shortening as well. Overlap resolution
                    # later keeps whichever candidate is longest.
                    shorter = self._shorten(text, match)
                    if shorter is not None:
                        yield Span(
                            start=shorter[0], end=shorter[1], entity=self.entity,
                            text=text[shorter[0]:shorter[1]], score=score,
                            recognizer=self.name,
                        )
                    position = match.start() + 1
                    continue
                if self.validated_score is not None:
                    score = self.validated_score if ok else self.score

            yield Span(
                start=start,
                end=end,
                entity=self.entity,
                text=matched,
                score=score,
                recognizer=self.name,
            )


# ─────────────────────────────────────────────────────────────────────────────
#  Names
# ─────────────────────────────────────────────────────────────────────────────

#: Name parts that are also ordinary English words. These match only when
#: capitalised, so a reviewer called "Mark" does not turn every "mark the
#: section" into a redaction. The list is short on purpose: it only needs to
#: cover words likely to appear in a review comment.
_AMBIGUOUS_NAME_PARTS: FrozenSet[str] = frozenset(
    """
    mark bill will page april may june august summer autumn winter dawn
    art rose lily jack grace hope faith joy sunny rich frank drew chase
    miles brook fields green white black brown young long short king baker
    cook carter turner walker parker taylor porter mason ray dean earl
    guy max victor major minor case bond field stone reed rush moss
    """.split()
)

#: Particles inside a surname that are never a name on their own.
_NAME_PARTICLES: FrozenSet[str] = frozenset(
    "de la van von der den del di da dos das du le el bin ibn al mac mc o".split()
)


def _name_variants(full_name: str) -> Tuple[List[str], List[str]]:
    """Split one roster name into (case-insensitive, capitalised-only) terms.

    The full name always goes in the case-insensitive bucket — matching
    ``alice tester`` in a lower-cased quotation is desirable and carries no
    false-positive risk.  Individual parts go there too unless they are also
    ordinary words, in which case they are only matched when capitalised.
    """
    cleaned = " ".join(full_name.split())
    if not cleaned:
        return [], []

    insensitive: List[str] = [cleaned]
    capitalised: List[str] = []

    parts = [part.strip(".,;:()[]{}\"'") for part in cleaned.split()]
    for part in parts:
        if len(part) < 3 or part.casefold() in _NAME_PARTICLES:
            # Two-letter fragments and particles are pure noise as standalone
            # terms; they are still covered by the full-name variant.
            continue
        if part.casefold() in _AMBIGUOUS_NAME_PARTS:
            capitalised.append(part)
        else:
            insensitive.append(part)

    # Also cover "Tester, Alice" and "Alice.Tester" spellings of the same name.
    if len(parts) > 1:
        insensitive.append(".".join(parts))
        insensitive.append(f"{parts[-1]}, {parts[0]}")

    return insensitive, capitalised


def _escape_flexible(term: str) -> str:
    """Escape ``term``, letting each space in it match any run of whitespace.

    A name typed once as ``Alice Tester`` appears in documents with a double
    space, a line break or a non-breaking space between the parts; matching
    only the literal single space would leak every one of those.
    """
    return r"\s+".join(re.escape(part) for part in term.split(" ") if part)


def _alternation(terms: Sequence[str]) -> str:
    """Build a longest-first alternation of escaped terms.

    Longest-first matters: Python's ``|`` is first-match, not longest-match, so
    ``Alice|Alice Tester`` would only ever match the first name.
    """
    unique = sorted(
        {" ".join(term.split()) for term in terms if term and term.strip()},
        key=len,
        reverse=True,
    )
    return "|".join(_escape_flexible(term) for term in unique)


class NameRecognizer(Recognizer):
    """Finds the people this document actually involves.

    Built from the reviewer roster plus any extra names the caller supplies.
    Also catches ``@handle`` mentions, whose shape is unambiguous enough to
    detect without a roster.

    Every distinct spelling of a person maps back to the *canonical* roster
    name, exposed through :meth:`canonical`, so ``Alice``, ``alice tester`` and
    ``Alice Tester`` all pseudonymise to the same token rather than to three
    different people.
    """

    name = "names"
    entity = EntityType.PERSON

    def __init__(
        self,
        names: Iterable[str],
        *,
        detect_mentions: bool = True,
        score: float = 0.95,
    ) -> None:
        self.score = score
        self._canonical: Dict[str, str] = {}

        insensitive: List[str] = []
        capitalised: List[str] = []

        for full_name in names:
            loose, strict = _name_variants(full_name)
            canonical = " ".join(full_name.split())
            for term in (*loose, *strict):
                # First roster entry to claim a spelling keeps it. Two people
                # sharing a surname is real; silently reassigning the surname to
                # whoever was parsed last would be worse than picking one.
                self._canonical.setdefault(term.casefold(), canonical)
            insensitive.extend(loose)
            capitalised.extend(strict)

        self._loose: Optional[Pattern[str]] = None
        if insensitive:
            self._loose = re.compile(
                rf"(?<!\w)(?:{_alternation(insensitive)})(?!\w)", re.IGNORECASE
            )

        self._strict: Optional[Pattern[str]] = None
        if capitalised:
            self._strict = re.compile(rf"(?<!\w)(?:{_alternation(capitalised)})(?!\w)")

        # A mention is a handle, not a name: lower confidence, own entity type.
        self._mention: Optional[Pattern[str]] = (
            re.compile(r"@[A-Za-z][\w.\-]{1,38}") if detect_mentions else None
        )

    def canonical(self, text: str) -> Optional[str]:
        """Map a matched spelling back to its roster name, if it is one."""
        return self._canonical.get(" ".join(text.split()).casefold())

    def terms(self) -> FrozenSet[str]:
        """Every case-folded spelling this recognizer will match.

        Read by the anonymizer so it never mints a stand-in built from a word
        that is itself a real name here — a document reviewed by a *Bob
        Reviewer* must not have its people renamed to ``Reviewer 1``.
        """
        return frozenset(self._canonical)

    def applies_to(self, text: str) -> bool:
        return bool(text)

    def find(self, text: str) -> Iterator[Span]:
        for pattern in (self._loose, self._strict):
            if pattern is None:
                continue
            for match in pattern.finditer(text):
                yield Span(
                    start=match.start(),
                    end=match.end(),
                    entity=EntityType.PERSON,
                    text=match.group(),
                    score=self.score,
                    recognizer=self.name,
                )

        if self._mention is not None and "@" in text:
            for match in self._mention.finditer(text):
                yield Span(
                    start=match.start(),
                    end=match.end(),
                    entity=EntityType.USERNAME,
                    text=match.group(),
                    score=0.8,
                    recognizer="mentions",
                )


class TermRecognizer(Recognizer):
    """Matches an explicit list of caller-supplied terms as one entity type.

    The escape hatch for everything a general detector cannot know: internal
    project codenames, customer names, a site address, an unreleased product.
    """

    def __init__(
        self,
        terms: Iterable[str],
        entity: EntityType = EntityType.CUSTOM,
        *,
        name: str = "custom-terms",
        case_sensitive: bool = False,
        score: float = 1.0,
    ) -> None:
        self.name = name
        self.entity = entity
        self.score = score
        collected = [term for term in terms if term and term.strip()]
        self._pattern: Optional[Pattern[str]] = None
        if collected:
            self._pattern = re.compile(
                rf"(?<!\w)(?:{_alternation(collected)})(?!\w)",
                0 if case_sensitive else re.IGNORECASE,
            )

    def applies_to(self, text: str) -> bool:
        return self._pattern is not None and bool(text)

    def find(self, text: str) -> Iterator[Span]:
        if self._pattern is None:
            return
        for match in self._pattern.finditer(text):
            yield Span(
                start=match.start(),
                end=match.end(),
                entity=self.entity,
                text=match.group(),
                score=self.score,
                recognizer=self.name,
            )


# ─────────────────────────────────────────────────────────────────────────────
#  The shipped detector set
# ─────────────────────────────────────────────────────────────────────────────

_DIGITS = frozenset("0123456789")

# Ordered roughly by specificity. Order does not affect correctness — conflicts
# are resolved by rank in entities.resolve_spans — but keeping the strong
# detectors first makes a debug dump of raw spans easier to read.
_BUILTIN_SPECS: Tuple[Recognizer, ...] = (
    # ── Credentials ─────────────────────────────────────────────────────────
    RegexRecognizer(
        "private-key",
        EntityType.PRIVATE_KEY,
        r"-----BEGIN[ A-Z]*PRIVATE KEY-----.*?-----END[ A-Z]*PRIVATE KEY-----",
        flags=re.DOTALL,
        trigger="-",
    ),
    RegexRecognizer(
        "jwt",
        EntityType.JWT,
        # A JWT always starts with a base64url'd '{"' — 'eyJ' — in its header.
        r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b",
        trigger="e",
    ),
    RegexRecognizer(
        "aws-access-key",
        EntityType.API_KEY,
        r"\b(?:AKIA|ASIA|ABIA|ACCA)[0-9A-Z]{16}\b",
        trigger="A",
    ),
    RegexRecognizer(
        "github-token",
        EntityType.API_KEY,
        r"\bgh[pousr]_[A-Za-z0-9]{36,255}\b",
        trigger="g",
    ),
    RegexRecognizer(
        "openai-key",
        EntityType.API_KEY,
        r"\bsk-(?:proj-|ant-)?[A-Za-z0-9_-]{20,}\b",
        trigger="s",
    ),
    RegexRecognizer(
        "slack-token",
        EntityType.API_KEY,
        r"\bxox[abposr]-[A-Za-z0-9-]{10,}\b",
        trigger="x",
    ),
    RegexRecognizer(
        "google-api-key",
        EntityType.API_KEY,
        r"\bAIza[0-9A-Za-z_-]{35}\b",
        trigger="A",
    ),
    RegexRecognizer(
        "keyword-secret",
        EntityType.SECRET,
        # "password: hunter2", "api_key = abc123", "token=..." — the value only,
        # so the label survives and the sentence still reads.
        r"(?i)\b(?:pass(?:word|wd)?|secret|api[_-]?key|access[_-]?token|token|"
        r"credential|auth)\b\s*[:=]\s*[\"']?(?P<value>[^\s\"',;]{6,})[\"']?",
        group="value",
        score=0.75,
    ),
    # ── Financial and government identifiers ────────────────────────────────
    RegexRecognizer(
        "iban",
        EntityType.IBAN,
        r"\b[A-Z]{2}\d{2}(?:[ ]?[A-Z0-9]{4}){2,7}(?:[ ]?[A-Z0-9]{1,3})?\b",
        validator=iban_valid,
        trigger=_DIGITS,
    ),
    RegexRecognizer(
        "credit-card",
        EntityType.CREDIT_CARD,
        r"\b(?:\d[ -]?){12,18}\d\b",
        validator=luhn_valid,
        trigger=_DIGITS,
    ),
    RegexRecognizer(
        "us-ssn",
        EntityType.SSN,
        r"\b\d{3}-\d{2}-\d{4}\b",
        validator=_ssn_valid,
        trigger="-",
    ),
    RegexRecognizer(
        "uk-nino",
        EntityType.NATIONAL_ID,
        r"\b[A-CEGHJ-PR-TW-Z]{2}[ ]?\d{2}[ ]?\d{2}[ ]?\d{2}[ ]?[A-D]\b",
        score=0.8,
        trigger=_DIGITS,
    ),
    RegexRecognizer(
        "passport",
        EntityType.PASSPORT,
        r"(?i)\bpassport\s*(?:no\.?|number|#)?\s*[:#]?\s*(?P<value>[A-Z0-9]{6,10})\b",
        group="value",
        score=0.85,
        trigger="p",
    ),
    # ── Contact details ─────────────────────────────────────────────────────
    RegexRecognizer(
        "email",
        EntityType.EMAIL,
        r"\b[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,63}\b",
        trigger="@",
    ),
    RegexRecognizer(
        "phone",
        EntityType.PHONE,
        # Requires either an international prefix or bracketed/separated groups:
        # a bare run of digits is far more likely to be an identifier. The
        # trailing guard excludes word characters but *not* a full stop, because
        # a phone number at the end of a sentence is the common case -- unless
        # the stop is followed by a digit. Without that exception the match ran
        # on into a following dotted number ("+1-202-555-0143 192.168.0.5"),
        # grew past fifteen digits, failed validation, and the phone number
        # was dropped instead of the regex backtracking to it.
        r"(?<![\w.\-])(?:\+\d{1,3}[ .\-]?)?(?:\(\d{2,4}\)[ .\-]?|\d{2,4}[ .\-])"
        r"\d{2,4}(?:[ .\-]?\d{2,4}){1,3}(?![\w\-]|[.,:]\d)",
        score=0.6,
        validator=_phone_plausible,
        trigger=_DIGITS,
    ),
    # ── Technical identifiers ───────────────────────────────────────────────
    RegexRecognizer(
        "url",
        EntityType.URL,
        r"\b(?:https?|ftp)://[^\s<>\"'\]),]+",
        trigger=":",
    ),
    RegexRecognizer(
        "ipv4",
        EntityType.IP_ADDRESS,
        # The trailing guard rejects a fifth dotted group -- so 1.2.3.4.5 is not
        # read as an address -- but must not reject a sentence-ending full stop,
        # which is where an address most often sits.
        r"(?<![\w.])\d{1,3}(?:\.\d{1,3}){3}(?!\.?\d)(?!\w)",
        validator=_ipv4_valid,
        trigger=".",
    ),
    RegexRecognizer(
        "ipv6",
        EntityType.IP_ADDRESS,
        # Only the unambiguous forms: enough groups that it cannot be mistaken
        # for a time, a range, or a Windows drive reference.
        r"(?<![\w:])(?:[0-9A-Fa-f]{1,4}:){7}[0-9A-Fa-f]{1,4}(?![\w:])"
        r"|(?<![\w:])(?:[0-9A-Fa-f]{1,4}:){2,6}:[0-9A-Fa-f]{1,4}(?![\w:])",
        score=0.9,
        trigger=":",
    ),
    RegexRecognizer(
        "mac-address",
        EntityType.MAC_ADDRESS,
        r"\b[0-9A-Fa-f]{2}(?::[0-9A-Fa-f]{2}){5}\b",
        trigger=":",
    ),
    RegexRecognizer(
        "home-path",
        EntityType.FILE_PATH,
        # Only paths that carry a login name. A bare /var/log/app.log identifies
        # nobody and redacting it would destroy a useful technical comment.
        r"(?:[A-Za-z]:\\Users\\[^\\/:*?\"<>|\r\n]+"
        r"|/home/[^/\s:]+"
        r"|/Users/[^/\s:]+)",
        score=0.9,
        trigger="/\\",
    ),
    # ── Contextual ──────────────────────────────────────────────────────────
    RegexRecognizer(
        "date-of-birth",
        EntityType.DATE_OF_BIRTH,
        r"(?i)\b(?:date\s+of\s+birth|d\.?o\.?b\.?|born(?:\s+on)?)\b\s*[:=]?\s*"
        r"(?P<value>\d{1,4}[-/.]\d{1,2}[-/.]\d{1,4}"
        r"|\d{1,2}\s+\w+\s+\d{4}"
        r"|\w+\s+\d{1,2},?\s+\d{4})",
        group="value",
        score=0.9,
        trigger=_DIGITS,
    ),
)



def default_recognizers(
    names: Iterable[str] = (),
    *,
    custom_terms: Iterable[str] = (),
    custom_patterns: Iterable[Union[Tuple[str, str], Tuple[str, str, EntityType]]] = (),
    detect_mentions: bool = True,
    organizations: Iterable[str] = (),
    locations: Iterable[str] = (),
) -> List[Recognizer]:
    """Assemble the detector set for one anonymisation run.

    Parameters
    ----------
    names:
        Reviewer names — normally ``stats().unique_authors``.  Drives
        :class:`NameRecognizer`.
    custom_terms:
        Extra literals to treat as ``CUSTOM``: codenames, customer names.
    custom_patterns:
        ``(name, regex)`` pairs matched as ``CUSTOM``, or ``(name, regex,
        entity)`` triples matched as that entity.  Compiled here so a bad
        pattern raises at configuration time rather than mid-export.
    organizations / locations:
        Literal company and place names, matched as ``ORGANIZATION`` and
        ``LOCATION`` — the only source of those types, since no gazetteer is
        shipped.
    """
    recognizers: List[Recognizer] = list(_BUILTIN_SPECS)

    roster = [name for name in names if name and name.strip()]
    if roster:
        recognizers.append(NameRecognizer(roster, detect_mentions=detect_mentions))
    elif detect_mentions:
        recognizers.append(NameRecognizer((), detect_mentions=True))

    terms = list(custom_terms)
    if terms:
        recognizers.append(TermRecognizer(terms))

    for entity, values, label in (
        (EntityType.ORGANIZATION, list(organizations), "organizations"),
        (EntityType.LOCATION, list(locations), "locations"),
    ):
        if values:
            recognizers.append(TermRecognizer(values, entity, name=label))

    for spec in custom_patterns:
        pattern_name, pattern = spec[0], spec[1]
        entity = spec[2] if len(spec) > 2 else EntityType.CUSTOM
        recognizers.append(RegexRecognizer(pattern_name, entity, pattern))

    return recognizers
