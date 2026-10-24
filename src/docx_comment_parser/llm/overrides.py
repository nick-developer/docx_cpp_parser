"""Correcting the classifier by hand: category, priority, intent, action_required.

The rule-based tags are a good first pass and occasionally plainly wrong — a
comment about a contract's typography is not Legal however many times it says
"clause".  A reviewer who knows better must be able to say so, and the
correction must survive a re-export rather than being lost the next time the
rules run.

So corrections are data, kept apart from the rules: a mapping from a comment to
the fields being set, validated against the label sets the rest of the package
uses, and applied on top of the rules wherever records are built::

    dataset = build_dataset(comments, overrides={4: {"category": "Compliance",
                                                     "priority": "high"}})
    dataset = dataset.with_overrides("labels.csv")      # or after the fact

A record remembers which of its fields were set by hand (``overridden``), which
matters twice over: a fine-tuning or evaluation set built from these records
must be able to tell human labels from rule labels, and the triage prompt must
not send a human-confirmed category back to a model to be second-guessed.

Comment ids are unique only within one document, so a key is either a bare id —
accepted when it is unambiguous in the dataset — or ``(document, id)``, or the
``"document#id"`` string the retrieval export already uses as an id.
"""

from __future__ import annotations

import csv
import json
from dataclasses import replace
from pathlib import Path
from typing import (
    TYPE_CHECKING,
    Any,
    Dict,
    Iterable,
    List,
    Mapping,
    Optional,
    Sequence,
    Tuple,
    Union,
)

from .classification import Category, Intent, Priority

if TYPE_CHECKING:  # pragma: no cover - typing only
    from .dataset import LLMRecord

__all__ = [
    "OVERRIDABLE_FIELDS",
    "OverrideError",
    "OverridesLike",
    "normalize_overrides",
    "load_overrides",
    "export_labels",
    "apply_overrides",
]

#: The record fields a person may set by hand.
OVERRIDABLE_FIELDS: Tuple[str, ...] = ("category", "priority", "intent", "action_required")

#: ``(document, comment_id)``, with ``""`` as the document meaning "any".
OverrideKey = Tuple[str, int]

#: Everything ``overrides=`` accepts: a mapping keyed by id, ``(document, id)``
#: or ``"document#id"``; a list of row dicts carrying ``comment_id`` (and
#: optionally ``document``); or a path to a .csv, .json or .jsonl file.
OverridesLike = Union[
    Mapping[Any, Mapping[str, Any]],
    Iterable[Mapping[str, Any]],
    str,
    Path,
]

_TRUE = {"true", "yes", "y", "1", "x", "on"}
_FALSE = {"false", "no", "n", "0", "off"}

_INTENT_SPELLINGS = {intent.value.replace("_", " "): intent for intent in Intent}


class OverrideError(ValueError):
    """An override names an unknown comment, field or label. Lists every problem."""

    def __init__(self, problems: Sequence[str]) -> None:
        self.problems: Tuple[str, ...] = tuple(problems)
        super().__init__(
            f"{len(self.problems)} problem(s) with the overrides:\n"
            + "\n".join(f"  - {problem}" for problem in self.problems)
        )


# ─────────────────────────────────────────────────────────────────────────────
#  Validation
# ─────────────────────────────────────────────────────────────────────────────


def _label(value: Any, enum: Any, field: str) -> str:
    """Match ``value`` against an enum's values, ignoring case and separators."""
    text = str(value).strip()
    folded = text.casefold().replace("-", "_").replace(" ", "_")
    for member in enum:
        if member.value.casefold() == folded:
            return str(member.value)
    if enum is Intent and text.casefold() in _INTENT_SPELLINGS:
        return _INTENT_SPELLINGS[text.casefold()].value
    valid = ", ".join(member.value for member in enum)
    raise ValueError(f"{field} {value!r} is not one of: {valid}")


def _flag(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    text = str(value).strip().casefold()
    if text in _TRUE:
        return True
    if text in _FALSE:
        return False
    raise ValueError(f"action_required {value!r} is not a yes/no value")


def _clean_fields(fields: Mapping[str, Any], where: str, problems: List[str]) -> Dict[str, Any]:
    """Validate one override's fields; blank cells mean "leave as is"."""
    cleaned: Dict[str, Any] = {}
    for name, value in fields.items():
        if name in {"comment_id", "document", "id"} or _ignorable(name):
            continue
        if name not in OVERRIDABLE_FIELDS:
            problems.append(
                f"{where}: unknown field {name!r}; overridable fields are "
                + ", ".join(OVERRIDABLE_FIELDS)
            )
            continue
        if value is None or (isinstance(value, str) and not value.strip()):
            continue
        try:
            if name == "category":
                cleaned[name] = _label(value, Category, name)
            elif name == "priority":
                cleaned[name] = _label(value, Priority, name)
            elif name == "intent":
                cleaned[name] = _label(value, Intent, name)
            else:
                cleaned[name] = _flag(value)
        except ValueError as exc:
            problems.append(f"{where}: {exc}")
    return cleaned


#: Columns a labels file carries for the reader's benefit, which are not
#: overrides and must not be reported as unknown fields.
_CONTEXT_COLUMNS = frozenset(
    {"author", "comment", "text", "referenced_text", "category_confidence",
     "resolved", "overridden", "thread_id", "reason"}
)


def _ignorable(name: str) -> bool:
    return name in _CONTEXT_COLUMNS


def _key(raw: Any, where: str) -> OverrideKey:
    """Parse an override key into ``(document, comment_id)``."""
    if isinstance(raw, bool):
        raise ValueError(f"{where}: {raw!r} is not a comment id")
    if isinstance(raw, int):
        return "", raw
    if isinstance(raw, tuple) and len(raw) == 2:
        return str(raw[0] or ""), int(raw[1])
    if isinstance(raw, str):
        document, _, identifier = raw.rpartition("#")
        try:
            return document, int(identifier.strip())
        except ValueError:
            raise ValueError(f"{where}: {raw!r} does not end in a comment id") from None
    raise ValueError(f"{where}: {raw!r} is not a comment id, (document, id) or 'document#id'")


def normalize_overrides(overrides: Optional[OverridesLike]) -> Dict[OverrideKey, Dict[str, Any]]:
    """Validate overrides in any accepted shape and return ``{(document, id): fields}``.

    Raises :class:`OverrideError` listing every bad key, field and label at
    once, so a hand-edited spreadsheet is fixed in one pass rather than one
    error at a time.  Empty cells are ignored, which is what lets a labels file
    leave most of its rows untouched.
    """
    if overrides is None:
        return {}
    if isinstance(overrides, (str, Path)):
        return load_overrides(overrides)

    problems: List[str] = []
    result: Dict[OverrideKey, Dict[str, Any]] = {}

    if isinstance(overrides, Mapping):
        items: Iterable[Tuple[Any, Mapping[str, Any]]] = overrides.items()
    else:
        rows = []
        for position, row in enumerate(overrides, start=1):
            if not isinstance(row, Mapping):
                problems.append(f"row {position}: expected a mapping, got {type(row).__name__}")
                continue
            identifier = row.get("comment_id", row.get("id"))
            if identifier is None or str(identifier).strip() == "":
                problems.append(f"row {position}: missing comment_id")
                continue
            document = str(row.get("document") or "")
            try:
                rows.append(((document, int(str(identifier).strip())), row))
            except ValueError:
                problems.append(f"row {position}: comment_id {identifier!r} is not an integer")
        items = rows

    for raw_key, fields in items:
        where = f"comment {raw_key!r}"
        try:
            key = _key(raw_key, where)
        except ValueError as exc:
            problems.append(str(exc))
            continue
        if not isinstance(fields, Mapping):
            problems.append(f"{where}: expected a mapping of fields")
            continue
        cleaned = _clean_fields(fields, where, problems)
        if cleaned:
            result.setdefault(key, {}).update(cleaned)

    if problems:
        raise OverrideError(problems)
    return result


def load_overrides(path: Union[str, Path]) -> Dict[OverrideKey, Dict[str, Any]]:
    """Read overrides from a .csv, .json or .jsonl file.

    A CSV needs a ``comment_id`` column, optionally ``document``, and any of the
    overridable fields — exactly what :func:`export_labels` writes, so the round
    trip is export, edit in a spreadsheet, load.  JSON may be a list of rows or a
    mapping of id to fields.
    """
    target = Path(path)
    suffix = target.suffix.lower()
    try:
        if suffix == ".csv":
            with target.open(encoding="utf-8-sig", newline="") as handle:
                return normalize_overrides(list(csv.DictReader(handle)))
        if suffix == ".jsonl":
            with target.open(encoding="utf-8") as handle:
                rows = [json.loads(line) for line in handle if line.strip()]
            return normalize_overrides(rows)
        payload = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, csv.Error) as exc:
        raise OverrideError([f"{target}: {exc}"]) from exc
    if isinstance(payload, Mapping):
        return normalize_overrides(payload)
    if isinstance(payload, list):
        return normalize_overrides(payload)
    raise OverrideError([f"{target}: expected a JSON list of rows or an object keyed by id"])


# ─────────────────────────────────────────────────────────────────────────────
#  Application
# ─────────────────────────────────────────────────────────────────────────────


def _action_required(intent: str, priority: str) -> bool:
    """The classifier's own rule, re-applied after a person changes its inputs."""
    return (
        intent in {Intent.CHANGE_REQUEST.value, Intent.QUESTION.value, Intent.OBJECTION.value}
        or priority == Priority.BLOCKER.value
    )


def apply_overrides(
    records: Sequence["LLMRecord"],
    overrides: Optional[OverridesLike],
    *,
    strict: bool = True,
) -> Tuple["LLMRecord", ...]:
    """Return ``records`` with the overrides applied; the inputs are unchanged.

    A manual category is reported with confidence ``1.0`` — it is no longer a
    guess.  When intent or priority change and ``action_required`` is not set
    explicitly, it is re-derived from the new values with the classifier's rule,
    so "make this a blocker" also makes it work to do.  A value equal to the
    record's current tag is not a correction and is not marked as manual.

    ``strict`` (the default) raises :class:`OverrideError` for an override that
    matches no record or a bare id that matches records in several documents;
    pass ``False`` to skip those instead.
    """
    normalised = normalize_overrides(overrides)
    if not normalised:
        return tuple(records)

    by_key: Dict[OverrideKey, List[int]] = {}
    for position, record in enumerate(records):
        by_key.setdefault((record.document, record.comment_id), []).append(position)
        by_key.setdefault(("", record.comment_id), []).append(position)

    problems: List[str] = []
    targets: Dict[int, Dict[str, Any]] = {}
    for key, fields in normalised.items():
        positions = by_key.get(key)
        if positions is None and key[0]:
            # A document given as a bare file name still matches a full path.
            positions = [
                index for index, record in enumerate(records)
                if record.comment_id == key[1]
                and Path(record.document).name == Path(key[0]).name
            ] or None
        label = f"{key[0]}#{key[1]}" if key[0] else f"comment {key[1]}"
        if not positions:
            problems.append(f"{label}: no such comment in this dataset")
            continue
        if not key[0] and len({records[index].document for index in positions}) > 1:
            problems.append(
                f"{label}: id exists in several documents; key it as (document, id)"
            )
            continue
        for index in positions:
            targets.setdefault(index, {}).update(fields)

    if problems and strict:
        raise OverrideError(problems)

    updated = list(records)
    for index, fields in targets.items():
        record = updated[index]
        # Only a value that differs from the current one is a correction. A
        # labels file carries every tag of every row, and treating an
        # untouched cell as a human label would mark the whole export manual
        # the first time it went through a spreadsheet.
        changes: Dict[str, Any] = {
            name: value for name, value in fields.items()
            if getattr(record, name) != value
        }
        if not changes:
            continue
        if "category" in changes:
            changes["category_confidence"] = 1.0
        if "action_required" not in fields and ({"intent", "priority"} & set(changes)):
            derived = _action_required(
                changes.get("intent", record.intent),
                changes.get("priority", record.priority),
            )
            if derived != record.action_required:
                changes["action_required"] = derived
        manual = tuple(
            name for name in OVERRIDABLE_FIELDS
            if name in changes or name in record.overridden
        )
        updated[index] = replace(record, overridden=manual, **changes)
    return tuple(updated)


def export_labels(records: Iterable["LLMRecord"], path: Union[str, Path]) -> Path:
    """Write an editable labels CSV: one row per record, current tags filled in.

    Edit the ``category``, ``priority``, ``intent`` or ``action_required`` cells
    in any spreadsheet and feed the file back through ``overrides=``.  The
    ``comment`` column is there to read, not to edit; it is ignored on load.
    """
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    columns = [
        "document", "comment_id", "author", "comment", *OVERRIDABLE_FIELDS,
        "category_confidence", "overridden",
    ]
    # utf-8-sig so Excel opens non-ASCII names correctly by double-click.
    with target.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns)
        writer.writeheader()
        for record in records:
            writer.writerow(
                {
                    "document": record.document,
                    "comment_id": record.comment_id,
                    "author": record.author,
                    "comment": " ".join(record.comment.split()),
                    "category": record.category,
                    "priority": record.priority,
                    "intent": record.intent,
                    "action_required": "yes" if record.action_required else "no",
                    "category_confidence": record.category_confidence,
                    "overridden": " ".join(record.overridden),
                }
            )
    return target
