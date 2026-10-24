"""Reading a model's answer back: parse it, check it, put the names back.

A prompt builder asks for JSON and for every claim to cite a comment id.  Asking
is half of it.  The other half is checking, because models wrap JSON in prose and
markdown fences, drop required keys, and — the failure that matters — cite ids
that were never in the prompt.  An action item citing comment #57 when the prompt
held #1 to #40 is not traceable to anything, and a pipeline that files it as a
ticket has filed a hallucination.

:func:`parse_response` does all of it in one call, locally::

    prompt = create_triage_prompt(dataset.records)
    answer = client.messages.create(**prompt.to_anthropic(), max_tokens=4096)
    parsed = parse_response(prompt, answer.content[0].text,
                            anonymizer=dataset.anonymizer)
    if not parsed.valid:
        print(parsed.problems())

and :func:`overrides_from_response` turns a triage answer into overrides, so a
model's reclassification flows back into the dataset through the same validated
path a person's does — labels checked, unknown ids refused.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import (
    TYPE_CHECKING,
    Any,
    Dict,
    List,
    Mapping,
    Optional,
    Sequence,
    Set,
    Tuple,
    Union,
)

if TYPE_CHECKING:  # pragma: no cover - typing only
    from ..privacy import Anonymizer, Vault
    from .prompts import Prompt

__all__ = [
    "ResponseParseError",
    "ParsedResponse",
    "extract_json",
    "parse_response",
    "overrides_from_response",
    "EXPECTED_KEYS",
]

#: The top-level keys each builder's schema requires.
EXPECTED_KEYS: Mapping[str, Tuple[str, ...]] = {
    "review_summary": ("summary", "themes", "disagreements", "open_questions", "readiness"),
    "action_items": ("actions",),
    "resolution": ("resolutions",),
    "triage": ("classifications",),
    "risk": ("risks", "overall"),
    "diff_summary": ("verdict", "progress", "stuck", "regressions", "recommended_next_step"),
}

#: The list under which each kind's items sit, for counting uncited items.
_ITEM_LISTS: Mapping[str, str] = {
    "review_summary": "themes",
    "action_items": "actions",
    "resolution": "resolutions",
    "triage": "classifications",
    "risk": "risks",
}


class ResponseParseError(ValueError):
    """The response contains no JSON that can be read."""


def _balanced_block(text: str, start: int) -> Optional[str]:
    """The JSON object or array opening at ``start``, string-aware, or None."""
    closing = {"{": "}", "[": "]"}
    stack: List[str] = []
    in_string = False
    escaped = False
    for index in range(start, len(text)):
        char = text[index]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char in closing:
            stack.append(closing[char])
        elif char in "}]":
            if not stack or stack.pop() != char:
                return None
            if not stack:
                return text[start:index + 1]
    return None


def extract_json(text: str) -> Any:
    """Pull the JSON value out of a model's response.

    Tolerates the usual wrapping: a markdown code fence, a sentence of preamble,
    a trailing remark.  Tries the whole text first, then a fenced block, then
    each ``{`` or ``[`` in turn until one opens a block that parses.
    """
    if not text or not text.strip():
        raise ResponseParseError("the response is empty")
    stripped = text.strip()
    try:
        return json.loads(stripped)
    except json.JSONDecodeError:
        pass

    fence = stripped.find("```")
    if fence != -1:
        body_start = stripped.find("\n", fence)
        body_end = stripped.find("```", body_start + 1) if body_start != -1 else -1
        if body_start != -1 and body_end != -1:
            try:
                return json.loads(stripped[body_start + 1:body_end])
            except json.JSONDecodeError:
                pass

    for index, char in enumerate(stripped):
        if char not in "{[":
            continue
        block = _balanced_block(stripped, index)
        if block is None:
            continue
        try:
            return json.loads(block)
        except json.JSONDecodeError:
            continue
    raise ResponseParseError("no parseable JSON object or array found in the response")


def _walk_ids(value: Any, found: List[int]) -> None:
    """Collect every integer under a ``comment_id`` or ``comment_ids`` key."""
    if isinstance(value, Mapping):
        for key, item in value.items():
            if key == "comment_id" and isinstance(item, int) and not isinstance(item, bool):
                found.append(item)
            elif key == "comment_ids" and isinstance(item, list):
                found.extend(
                    entry for entry in item
                    if isinstance(entry, int) and not isinstance(entry, bool)
                )
            else:
                _walk_ids(item, found)
    elif isinstance(value, list):
        for item in value:
            _walk_ids(item, found)


def _restore(value: Any, restore: Any) -> Any:
    """Apply ``restore`` to every string inside a JSON value."""
    if isinstance(value, str):
        return restore(value)
    if isinstance(value, list):
        return [_restore(item, restore) for item in value]
    if isinstance(value, dict):
        return {key: _restore(item, restore) for key, item in value.items()}
    return value


@dataclass(frozen=True)
class ParsedResponse:
    """A model's answer, parsed and checked against the prompt that asked."""

    kind: str
    data: Any
    """The parsed JSON — with real names restored when a vault was given."""

    cited: Tuple[int, ...]
    """Every comment id the answer cites, in order of first appearance."""

    unknown_ids: Tuple[int, ...]
    """Cited ids that were not in the prompt: hallucinated references."""

    missing_keys: Tuple[str, ...]
    """Top-level keys the schema requires and the answer left out."""

    uncited_items: int
    """Items in the answer's main list that cite no comment at all."""

    @property
    def valid(self) -> bool:
        """True when every citation is real and the schema's keys are present."""
        return not self.unknown_ids and not self.missing_keys

    def problems(self) -> List[str]:
        """Readable descriptions of what is wrong; empty when valid."""
        found: List[str] = []
        if self.missing_keys:
            found.append("missing required keys: " + ", ".join(self.missing_keys))
        if self.unknown_ids:
            found.append(
                "cites comment ids that were not in the prompt: "
                + ", ".join(f"#{identifier}" for identifier in self.unknown_ids)
            )
        if self.uncited_items:
            found.append(f"{self.uncited_items} item(s) cite no comment")
        return found

    def to_dict(self) -> Dict[str, Any]:
        return {
            "kind": self.kind,
            "valid": self.valid,
            "cited": list(self.cited),
            "unknown_ids": list(self.unknown_ids),
            "missing_keys": list(self.missing_keys),
            "uncited_items": self.uncited_items,
            "data": self.data,
        }


def parse_response(
    prompt: Union["Prompt", Mapping[str, Any], str],
    text: str,
    *,
    included: Optional[Sequence[int]] = None,
    anonymizer: Optional[Union["Anonymizer", "Vault"]] = None,
) -> ParsedResponse:
    """Parse and verify a model's answer to one of this package's prompts.

    ``prompt`` is the :class:`~.prompts.Prompt` that was sent, its
    ``to_dict()`` form (what ``docx-comments prompt --json`` writes), or just
    the kind name together with ``included``.  Citations are checked against
    the ids that were actually in the prompt; ``anonymizer`` (or a loaded
    :class:`~docx_comment_parser.privacy.Vault`) restores the real names in
    every string of the answer, after the check, on this machine.
    """
    if isinstance(prompt, str):
        kind, allowed = prompt, included
    elif isinstance(prompt, Mapping):
        kind = str(prompt.get("kind", ""))
        allowed = included if included is not None else prompt.get("included")
    else:
        kind = prompt.kind
        allowed = included if included is not None else prompt.included

    data = extract_json(text)

    missing: Tuple[str, ...] = ()
    if isinstance(data, Mapping):
        missing = tuple(key for key in EXPECTED_KEYS.get(kind, ()) if key not in data)
    elif kind in EXPECTED_KEYS:
        missing = EXPECTED_KEYS[kind]

    cited_list: List[int] = []
    _walk_ids(data, cited_list)
    cited = tuple(dict.fromkeys(cited_list))

    # The diff prompt carries no per-comment ids of its own, so there is
    # nothing to check citations against.
    unknown: Tuple[int, ...] = ()
    if allowed is not None and (kind != "diff_summary" or allowed):
        permitted: Set[int] = set(int(identifier) for identifier in allowed)
        unknown = tuple(identifier for identifier in cited if identifier not in permitted)

    uncited = 0
    list_key = _ITEM_LISTS.get(kind)
    if list_key and isinstance(data, Mapping) and isinstance(data.get(list_key), list):
        for item in data[list_key]:
            ids: List[int] = []
            _walk_ids(item, ids)
            if not ids:
                uncited += 1

    if anonymizer is not None:
        restore = getattr(anonymizer, "deanonymize", None) or getattr(anonymizer, "restore")
        data = _restore(data, restore)

    return ParsedResponse(
        kind=kind,
        data=data,
        cited=cited,
        unknown_ids=unknown,
        missing_keys=missing,
        uncited_items=uncited,
    )


def overrides_from_response(
    parsed: ParsedResponse,
) -> Tuple[Dict[Tuple[str, int], Dict[str, Any]], List[str]]:
    """Turn a triage answer into overrides, keeping only what checks out.

    Returns ``(overrides, problems)``.  An entry is dropped — and described in
    ``problems`` — when it cites an id that was not in the prompt or uses a
    label outside the package's sets; the rest are returned ready for
    ``dataset.with_overrides(overrides)``.  Only ``category``, ``priority`` and
    ``intent`` are taken: the model's ``reason`` is not a field.
    """
    from .overrides import OverrideError, normalize_overrides

    if parsed.kind != "triage":
        raise ValueError(f"only a triage response carries classifications, not {parsed.kind!r}")
    entries = parsed.data.get("classifications") if isinstance(parsed.data, Mapping) else None
    if not isinstance(entries, list):
        return {}, ["the response has no 'classifications' list"]

    unknown = set(parsed.unknown_ids)
    overrides: Dict[Tuple[str, int], Dict[str, Any]] = {}
    problems: List[str] = []
    for position, entry in enumerate(entries, start=1):
        if not isinstance(entry, Mapping):
            problems.append(f"entry {position}: not an object")
            continue
        identifier = entry.get("comment_id")
        if identifier in unknown:
            problems.append(f"entry {position}: comment #{identifier} was not in the prompt")
            continue
        row = {
            key: entry[key]
            for key in ("comment_id", "category", "priority", "intent")
            if key in entry
        }
        try:
            overrides.update(normalize_overrides([row]))
        except OverrideError as exc:
            problems.extend(f"entry {position}: {problem}" for problem in exc.problems)
    return overrides, problems
