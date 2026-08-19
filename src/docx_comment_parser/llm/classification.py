"""Reading a review comment without a model: what kind, how urgent, what it wants.

The roadmap calls for a rule-based classification layer, and rules are the right
answer here for a reason beyond cost.  A review corpus is small, private, and
answered in a terminal: a classifier that runs in microseconds, explains every
decision by naming the words that drove it, and never sends a sentence anywhere
is more useful in this position than one that is marginally more accurate and
requires an API key.  It is also the layer that makes the *LLM* export better —
a record that arrives pre-tagged lets a prompt say "here are the 12 compliance
comments" instead of spending its budget rediscovering that.

Three orthogonal questions are answered, because they are genuinely orthogonal:

:class:`Category`
    What the comment is *about* — the eight subject areas the roadmap names.
    "Add a citation for the retention period" is Compliance; "add a citation"
    alone is Editorial.

:class:`Intent`
    What the comment *wants* — a question, a change, an objection, an approval.
    A Technical comment that is a question needs a different response from a
    Technical comment that is an instruction.

:class:`Priority`
    How loudly it says so.  Driven by explicit urgency markers and by
    intent — a blocker phrased as a question is still a blocker.

Scoring is weighted term matching over word-boundary patterns, normalised so a
long comment cannot out-score a short one by repetition alone.  Confidence is
the winning category's share of the total, which means a comment matching two
categories equally reports low confidence rather than picking one and sounding
certain — the property that makes the output safe to filter on.
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass
from enum import Enum
from typing import (
    Dict,
    Iterable,
    List,
    Mapping,
    Optional,
    Pattern,
    Sequence,
    Tuple,
    TypeVar,
)

_Key = TypeVar("_Key")
"""The kind of label a term list is keyed by: a Category, an Intent or a
Priority. The scoring machinery is identical for all three, and a type
variable is what lets it stay identical without erasing the key type."""

from ..models import Comment

__all__ = [
    "Category",
    "Intent",
    "Priority",
    "Classification",
    "classify_text",
    "classify_comment",
    "classify_comments",
    "category_breakdown",
]


class Category(str, Enum):
    """What a review comment is about. The roadmap's eight, plus a fallback."""

    GRAMMAR = "Grammar"
    """Spelling, punctuation, agreement — mechanical language errors."""

    EDITORIAL = "Editorial"
    """Wording, structure, tone, clarity — language choices that are not errors."""

    TECHNICAL = "Technical"
    DESIGN = "Design"
    COMPLIANCE = "Compliance"
    LEGAL = "Legal"
    SECURITY = "Security"
    REQUIREMENTS = "Requirements"
    OTHER = "Other"
    """Nothing matched strongly enough to claim a subject."""

    def __str__(self) -> str:  # pragma: no cover - trivial
        return self.value


class Intent(str, Enum):
    """What the comment asks the author to do."""

    QUESTION = "question"
    CHANGE_REQUEST = "change_request"
    SUGGESTION = "suggestion"
    OBJECTION = "objection"
    APPROVAL = "approval"
    INFORMATION = "information"
    """A remark that asks for nothing."""

    def __str__(self) -> str:  # pragma: no cover - trivial
        return self.value


class Priority(str, Enum):
    """How urgent the comment is, as the comment itself presents it."""

    BLOCKER = "blocker"
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"

    def __str__(self) -> str:  # pragma: no cover - trivial
        return self.value


# ─────────────────────────────────────────────────────────────────────────────
#  Signals
# ─────────────────────────────────────────────────────────────────────────────
# Each category is a list of (weight, term) pairs. Weights are coarse on purpose
# — 3 for a term that alone settles the category, 2 for a strong indicator, 1 for
# a supporting one — because fine-tuned weights on hand-written rules are false
# precision. Terms are matched on word boundaries, so "sign" does not fire on
# "design" and "audit" does not fire on "auditory".

_CATEGORY_TERMS: Mapping[Category, Sequence[Tuple[int, str]]] = {
    Category.GRAMMAR: [
        (3, r"typo|misspell(?:ed|ing)?|spelling"),
        (3, r"grammar|grammatical"),
        (2, r"punctuation|comma|semicolon|apostrophe|hyphen"),
        (2, r"tense|plural|singular|subject-verb"),
        (2, r"capitali[sz](?:e|ed|ation)"),
        (1, r"sentence fragment|run-on"),
    ],
    Category.EDITORIAL: [
        (3, r"reword|rephrase|rewrite|tighten"),
        (2, r"wordy|verbose|awkward|clunky|reads? poorly"),
        (2, r"clarify|clarification|unclear|confusing|ambiguous"),
        (2, r"tone|style guide|voice|jargon"),
        (2, r"consistent(?:cy)?|terminology"),
        (1, r"heading|title|caption|formatting|format"),
        (1, r"shorten|expand|move this|reorder|restructure"),
    ],
    Category.TECHNICAL: [
        (3, r"bug|defect|regression|crash|exception|stack trace"),
        (3, r"algorithm|implementation|architecture"),
        (2, r"api|endpoint|payload|schema|database|query|index"),
        (2, r"performance|latency|throughput|memory leak|scalab\w*"),
        (2, r"code|function|method|class|module|library|dependency"),
        (2, r"technically (?:wrong|incorrect|inaccurate)"),
        (1, r"version|deploy(?:ment)?|configuration|config|log(?:ging)?"),
        (1, r"test(?:s|ing|case)?|coverage|ci\b|pipeline"),
    ],
    Category.DESIGN: [
        (3, r"ux|usability|user experience"),
        (3, r"mockup|wireframe|prototype"),
        (2, r"layout|spacing|padding|margin|alignment|align(?:ed|ment)?"),
        (2, r"colour|color|contrast|palette|typography|font"),
        (2, r"accessib\w+|a11y|screen reader|wcag"),
        (2, r"diagram|figure|illustration|icon|screenshot"),
        (1, r"visual|appearance|look and feel|responsive|mobile view"),
    ],
    Category.COMPLIANCE: [
        (3, r"gdpr|hipaa|sox|pci[- ]?dss|iso ?\d{4,5}|soc ?2"),
        (3, r"complian\w+|non-?compliant"),
        (2, r"regulat\w+|standard \d|policy requirement|control objective"),
        (2, r"audit(?:or|ed|ing)?|evidence|attestation|certification"),
        (2, r"retention period|data subject|consent|lawful basis"),
        (1, r"mandator\w+|must comply|sign-?off required"),
    ],
    Category.LEGAL: [
        (3, r"legal|counsel|lawyer|solicitor|attorney"),
        (3, r"liabilit\w+|indemnit\w+|indemnif\w+|warrant(?:y|ies)"),
        (2, r"contract(?:ual)?|clause|term(?:s)? and conditions|agreement|nda"),
        (2, r"copyright|trademark|licen[cs]e|intellectual property|patent"),
        (2, r"jurisdiction|governing law|arbitration|breach of"),
        (1, r"sign-?off|approval from legal|disclaimer"),
    ],
    Category.SECURITY: [
        (3, r"vulnerab\w+|exploit|injection|xss|csrf|rce\b"),
        (3, r"security|insecure"),
        (2, r"encrypt\w*|decrypt\w*|tls|ssl|cipher|hashing"),
        (2, r"authenticat\w+|authoris\w+|authoriz\w+|access control|permission"),
        (2, r"credential|password|secret|api key|token|certificate"),
        (2, r"threat model|attack surface|pen(?:etration)? test|cve-?\d*"),
        (1, r"leak|exposure|sanitis\w+|sanitiz\w+|hardening"),
    ],
    Category.REQUIREMENTS: [
        (3, r"requirement|acceptance criteri\w+|user stor(?:y|ies)"),
        (3, r"out of scope|in scope|scope creep"),
        (2, r"shall\b|must\b|should\b(?! be (?:reworded|rephrased))"),
        (2, r"specification|spec\b|functional|non-?functional"),
        (2, r"stakeholder|business rule|use case|traceab\w+"),
        (1, r"missing|incomplete|not covered|undefined behaviour|tbd"),
    ],
}

_INTENT_TERMS: Mapping[Intent, Sequence[Tuple[int, str]]] = {
    Intent.OBJECTION: [
        (3, r"disagree|object|strongly oppose|cannot accept"),
        (3, r"this is (?:wrong|incorrect|not right|false)"),
        (3, r"this is a (?:blocker|problem|serious \w+)|cannot ship|do not ship"),
        (2, r"no[,.]|absolutely not|that is not"),
    ],
    Intent.APPROVAL: [
        (3, r"looks good|lgtm|approved?|agreed?|fine by me|no objection"),
        (2, r"nice|great|well (?:put|written)|thanks for|good catch"),
        (2, r"resolved|done|fixed"),
    ],
    Intent.CHANGE_REQUEST: [
        (3, r"please (?:add|remove|change|update|fix|rewrite|delete|replace)"),
        (3, r"needs? (?:to be |a |an )?\w+|must be (?:changed|updated|removed)"),
        # A modal followed by a verb is the everyday way a reviewer asks for
        # work: "must sign off", "should be receive", "needs to document". The
        # bare modals alone are a Requirements *category* signal, not an intent
        # one, which is why the verb is required here.
        (3, r"(?:must|should|needs? to|has to|have to)\s+(?:be\s+|not\s+)?[a-z]\w+"),
        (2, r"^(?:add|remove|delete|change|update|fix|replace|move|rename)\b"),
        (2, r"required|mandatory"),
    ],
    Intent.SUGGESTION: [
        (3, r"consider|suggest|recommend|perhaps|maybe|it might be"),
        (2, r"could (?:we|you|be)|would be (?:better|nicer|clearer)"),
        (2, r"optional|nit:|nitpick"),
    ],
    Intent.QUESTION: [
        (3, r"\?"),
        (2, r"^(?:why|what|how|when|where|who|which|is|are|do|does|did|can|could|should|would)\b"),
        (2, r"any (?:reason|thoughts)|thoughts\?"),
    ],
}

_PRIORITY_TERMS: Mapping[Priority, Sequence[Tuple[int, str]]] = {
    Priority.BLOCKER: [
        (3, r"blocker|blocking|show-?stopper|cannot ship|do not ship"),
        (3, r"critical|severe|must fix before"),
        (2, r"legal (?:issue|risk)|data (?:loss|breach)|security (?:issue|risk|hole)"),
    ],
    Priority.HIGH: [
        (3, r"urgent|asap|high priority|important|major"),
        (2, r"needs? sign-?off|escalat\w+|deadline"),
        (2, r"incorrect|wrong|misleading|inaccurate"),
    ],
    Priority.LOW: [
        (3, r"nit:|nitpick|minor|trivial|cosmetic"),
        (2, r"optional|if you have time|no rush|not important"),
        # Weighted to outrank the HIGH signal "wrong", which a typo report will
        # almost always also contain: "'recieve' is wrong" is a low-priority
        # comment however emphatically it is phrased.
        (3, r"typo|whitespace|spelling"),
    ],
}


def _compile(
    groups: Mapping[_Key, Sequence[Tuple[int, str]]]
) -> Dict[_Key, Tuple[Tuple[int, Pattern[str]], ...]]:
    """Compile every term list once, at import.

    One pattern per (weight, term) pair rather than one giant alternation per
    category: the individual patterns are what let a classification name the
    terms that drove it, and matching a handful of short regexes over a comment
    is not the cost worth optimising away.
    """
    compiled: Dict[_Key, Tuple[Tuple[int, Pattern[str]], ...]] = {}
    for key, terms in groups.items():
        entries: List[Tuple[int, Pattern[str]]] = []
        for weight, term in terms:
            # A term anchored to the start of a line, or one that is punctuation
            # rather than a word, must not be wrapped in a word-boundary guard:
            # "(?<!\w)^" can never match and "(?<!\w)\?" never would either.
            body = term if term.startswith("^") or term == r"\?" else rf"(?<!\w)(?:{term})"
            entries.append((weight, re.compile(body, re.IGNORECASE | re.MULTILINE)))
        compiled[key] = tuple(entries)
    return compiled


_CATEGORY_PATTERNS = _compile(_CATEGORY_TERMS)
_INTENT_PATTERNS = _compile(_INTENT_TERMS)
_PRIORITY_PATTERNS = _compile(_PRIORITY_TERMS)

#: A category needs at least this much weight before it is claimed at all.
#: Below it the comment is ``OTHER``, which is a more honest answer than the
#: least-bad of eight guesses.
_MIN_CATEGORY_SCORE = 2


# ─────────────────────────────────────────────────────────────────────────────
#  Result
# ─────────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Classification:
    """What the rules concluded about one comment, and why."""

    category: Category
    confidence: float
    """The winning category's share of all category weight, 0.0–1.0.
    ``0.0`` when nothing matched and the category is ``OTHER``."""

    scores: Mapping[Category, int]
    """Raw weight per category, for a caller that wants a second choice."""

    intent: Intent
    priority: Priority
    action_required: bool
    """True when the comment asks the author to do something."""

    matched: Tuple[str, ...]
    """The terms that fired, in the order found — the audit trail that makes a
    surprising classification explainable in one line."""

    @property
    def is_confident(self) -> bool:
        """True when one category clearly won. The threshold a filter should use."""
        return self.confidence >= 0.5 and self.category is not Category.OTHER

    def to_dict(self) -> Dict[str, object]:
        return {
            "category": self.category.value,
            "confidence": round(self.confidence, 3),
            "intent": self.intent.value,
            "priority": self.priority.value,
            "action_required": self.action_required,
            "matched": list(self.matched),
        }


# ─────────────────────────────────────────────────────────────────────────────
#  Classification
# ─────────────────────────────────────────────────────────────────────────────


def _score(
    text: str, patterns: Mapping[_Key, Tuple[Tuple[int, Pattern[str]], ...]]
) -> Tuple[Dict[_Key, int], List[str]]:
    """Weight per key, plus the matched substrings, over one text.

    A term contributes its weight once however often it appears.  Repetition is
    emphasis, not evidence: a comment saying "security" four times is not four
    times as much about security, and counting it that way would let a long
    comment dominate every aggregate it appears in.
    """
    scores: Dict[_Key, int] = {}
    matched: List[str] = []

    for key, compiled in patterns.items():
        total = 0
        for weight, pattern in compiled:
            found = pattern.search(text)
            if found is not None:
                total += weight
                hit = found.group().strip()
                if hit and hit not in matched:
                    matched.append(hit)
        if total:
            scores[key] = total

    return scores, matched


def classify_text(text: str, referenced_text: str = "") -> Classification:
    """Classify a comment body, optionally informed by the text it points at.

    The anchored text is scored at half weight: it tells you what the comment is
    *near*, which is a real signal (a note on a liability clause is probably
    legal) but a weaker one than what the comment says.
    """
    if not text or not text.strip():
        return Classification(
            category=Category.OTHER,
            confidence=0.0,
            scores={},
            intent=Intent.INFORMATION,
            priority=Priority.MEDIUM,
            action_required=False,
            matched=(),
        )

    scores, matched = _score(text, _CATEGORY_PATTERNS)

    if referenced_text:
        anchor_scores, _ = _score(referenced_text, _CATEGORY_PATTERNS)
        for key, value in anchor_scores.items():
            scores[key] = scores.get(key, 0) + value // 2

    total = sum(scores.values())
    if total and max(scores.values()) >= _MIN_CATEGORY_SCORE:
        # max() over (score, -index) keeps the outcome deterministic when two
        # categories tie: the earlier one in the declaration order wins rather
        # than whichever the dict happened to yield first.
        order = list(_CATEGORY_TERMS)
        category = max(scores, key=lambda key: (scores[key], -order.index(key)))
        confidence = scores[category] / total
    else:
        category = Category.OTHER
        confidence = 0.0

    intent = _classify_intent(text)
    priority = _classify_priority(text, category, intent)

    return Classification(
        category=category,
        confidence=confidence,
        scores=scores,
        intent=intent,
        priority=priority,
        # A blocker is work whatever grammar it is phrased in. "This is a
        # blocker" states a fact and asks for nothing, and treating it as
        # informational would drop the most important comment in the review.
        action_required=(
            intent in {Intent.CHANGE_REQUEST, Intent.QUESTION, Intent.OBJECTION}
            or priority is Priority.BLOCKER
        ),
        matched=tuple(matched),
    )


def _classify_intent(text: str) -> Intent:
    """Pick the single intent with the most weight, with tie-breaks that matter.

    Approval only wins outright when nothing else does: "looks good, but please
    fix the citation" is a change request with a compliment attached, and
    treating it as an approval would drop the request.
    """
    scores, _ = _score(text, _INTENT_PATTERNS)
    if not scores:
        return Intent.INFORMATION

    actionable = {
        intent: value
        for intent, value in scores.items()
        if intent is not Intent.APPROVAL
    }
    pool = actionable or scores
    order = list(_INTENT_TERMS)
    return max(pool, key=lambda key: (pool[key], -order.index(key)))


def _classify_priority(text: str, category: Category, intent: Intent) -> Priority:
    """Explicit urgency wins; otherwise the subject and intent set the floor."""
    scores, _ = _score(text, _PRIORITY_PATTERNS)
    if scores:
        order = list(_PRIORITY_TERMS)
        return max(scores, key=lambda key: (scores[key], -order.index(key)))

    # No urgency words. A security, legal or compliance objection is high by
    # subject alone — those are the comments that become incidents when they are
    # triaged as ordinary edits.
    if category in {Category.SECURITY, Category.LEGAL, Category.COMPLIANCE}:
        return Priority.HIGH if intent is not Intent.APPROVAL else Priority.MEDIUM
    if category is Category.GRAMMAR:
        return Priority.LOW
    if intent in {Intent.CHANGE_REQUEST, Intent.OBJECTION}:
        return Priority.MEDIUM
    if intent is Intent.APPROVAL:
        return Priority.LOW
    return Priority.MEDIUM


def classify_comment(comment: Comment) -> Classification:
    """Classify one :class:`~docx_comment_parser.models.Comment`."""
    return classify_text(comment.text, comment.referenced_text)


def classify_comments(comments: Iterable[Comment]) -> List[Classification]:
    """Classify an export, in order."""
    return [classify_comment(comment) for comment in comments]


def category_breakdown(
    comments: Iterable[Comment],
    classifications: Optional[Sequence[Classification]] = None,
) -> Dict[Category, int]:
    """Count comments per category, most common first.

    Pass ``classifications`` to reuse a pass you have already made rather than
    classifying twice.
    """
    results = (
        list(classifications)
        if classifications is not None
        else classify_comments(comments)
    )
    counts = Counter(result.category for result in results)
    return dict(counts.most_common())
