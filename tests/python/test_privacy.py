"""Tests for the v1.5 privacy layer: detection, policy, replacement, verification.

A redactor is only as good as the case it fails on, so the emphasis here is on
the ways one quietly goes wrong rather than on the happy path:

* the *inside* of a match — a person's name inside an email address, an email
  inside a URL — which is where an order-dependent implementation leaks;
* the checksum boundary, where a number that merely looks like a card must not
  be treated as one, and one that is must not be missed;
* consistency, because a pseudonym that is stable in the text and different in
  the author column tells a reader exactly who it is;
* a pseudonym that collides with a real name in the same document, which reads
  as correct output and is not;
* reversal, which must restore what was replaced and nothing else.

Every test builds real ``Comment`` rows, and the end-to-end ones go through a
genuine .docx and the real parser.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional

import pytest

import docx_comment_parser as dcp
from docx_comment_parser.models import Comment
from docx_comment_parser.privacy import (
    AnonymizationPolicy,
    Anonymizer,
    EntityType,
    NameRecognizer,
    RegexRecognizer,
    Span,
    Strategy,
    TermRecognizer,
    Vault,
    anonymize_comments,
    iban_valid,
    luhn_valid,
    resolve_spans,
    scan_comments,
)
from docx_comment_parser.privacy.entities import merge_adjacent
from docx_comment_parser.privacy.policy import PRESET_NAMES, resolve_policy

from conftest import CommentSpec, build_docx


# ─────────────────────────────────────────────────────────────────────────────
#  Helpers
# ─────────────────────────────────────────────────────────────────────────────


def make_comment(
    comment_id: int = 0,
    author: str = "Alice Tester",
    text: str = "A comment.",
    referenced_text: str = "",
    initials: str = "AT",
    document: str = "review.docx",
    resolved: bool = False,
    parent_id: Optional[int] = None,
) -> Comment:
    """One Comment row, with only the fields these tests care about set."""
    return Comment(
        comment_id=comment_id,
        parent_id=parent_id,
        author=author,
        initials=initials,
        date="2024-03-01T10:00:00Z",
        date_parsed=datetime(2024, 3, 1, 10, tzinfo=timezone.utc),
        text=text,
        referenced_text=referenced_text,
        paragraph_style="CommentText",
        resolved=resolved,
        is_reply=parent_id is not None,
        thread_depth=0 if parent_id is None else 1,
        document_name=document,
        root_id=comment_id if parent_id is None else parent_id,
        reply_count=0,
        para_id="",
        para_id_parent="",
        range_start_para_id="",
        range_end_para_id="",
        paragraph_index=-1,
        run_index=-1,
    )


def entities_in(anonymizer: Anonymizer, text: str) -> List[EntityType]:
    return [span.entity for span in anonymizer.scan(text, all_entities=True)]


# ─────────────────────────────────────────────────────────────────────────────
#  Validators
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "number",
    ["4111111111111111", "4111 1111 1111 1111", "5500-0000-0000-0004",
     "378282246310005", "6011111111111117"],
)
def test_luhn_accepts_real_card_numbers(number: str) -> None:
    assert luhn_valid(number)


@pytest.mark.parametrize(
    "number",
    ["4111111111111112", "1234567890123456", "1234", "12345678901234567890",
     "0000000000000001"],
)
def test_luhn_rejects_numbers_that_are_not_cards(number: str) -> None:
    assert not luhn_valid(number)


@pytest.mark.parametrize(
    "iban",
    ["GB82WEST12345698765432", "DE89370400440532013000", "FR1420041010050500013M02606",
     "GB82 WEST 1234 5698 7654 32"],
)
def test_iban_accepts_valid_accounts(iban: str) -> None:
    assert iban_valid(iban)


@pytest.mark.parametrize(
    "iban", ["GB82WEST12345698765433", "XX00", "GB82WEST", "1234567890123456"]
)
def test_iban_rejects_invalid_accounts(iban: str) -> None:
    assert not iban_valid(iban)


def test_a_long_order_number_is_not_treated_as_a_card() -> None:
    """The precision case: shape alone must not be enough."""
    anonymizer = Anonymizer("strict")
    text = "Order 1234567890123456 shipped."
    assert EntityType.CREDIT_CARD not in entities_in(anonymizer, text)
    assert "1234567890123456" in anonymizer.anonymize_text(text)


def test_a_real_card_number_is_redacted() -> None:
    anonymizer = Anonymizer("strict")
    assert "[CREDIT_CARD]" in anonymizer.anonymize_text("Card 4111 1111 1111 1111.")


# ─────────────────────────────────────────────────────────────────────────────
#  Detection
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "text,entity",
    [
        ("Write to bob@example.com", EntityType.EMAIL),
        ("See https://intranet.acme.example/spec", EntityType.URL),
        ("Server 192.168.10.240 is down", EntityType.IP_ADDRESS),
        ("MAC de:ad:be:ef:00:01", EntityType.MAC_ADDRESS),
        ("Key AKIAIOSFODNN7EXAMPLE", EntityType.API_KEY),
        ("token ghp_" + "a" * 36, EntityType.API_KEY),
        ("SSN 123-45-6789", EntityType.SSN),
        ("IBAN GB82WEST12345698765432", EntityType.IBAN),
        ("Call +44 20 7946 0958 today", EntityType.PHONE),
        ("Path C:\\Users\\nick\\spec.docx", EntityType.FILE_PATH),
        ("password: hunter2secret", EntityType.SECRET),
        ("Date of birth: 1985-04-12", EntityType.DATE_OF_BIRTH),
    ],
)
def test_each_detector_finds_its_own_kind(text: str, entity: EntityType) -> None:
    assert entity in entities_in(Anonymizer("strict"), text)


def test_a_jwt_is_detected() -> None:
    token = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dozjgNryP4J3jVmNHl0w5N_XgL0n3I9PlFUP0THsR8U"
    assert EntityType.JWT in entities_in(Anonymizer("strict"), f"Bearer {token}")


def test_a_private_key_block_is_detected_whole() -> None:
    block = (
        "-----BEGIN RSA PRIVATE KEY-----\n"
        "MIIEpAIBAAKCAQEA0Z3VS5JJcds3xfn/ygWyF\n"
        "-----END RSA PRIVATE KEY-----"
    )
    anonymizer = Anonymizer("strict")
    spans = [s for s in anonymizer.scan(block) if s.entity is EntityType.PRIVATE_KEY]
    assert len(spans) == 1
    assert spans[0].length == len(block)
    assert "MIIEpAIBAAKCAQEA" not in anonymizer.anonymize_text(block)


def test_a_version_string_is_not_a_phone_number() -> None:
    """The false positive that would make the redactor untrustworthy."""
    anonymizer = Anonymizer("strict")
    for text in ("Version 1.2.3", "Build 2024.11.05", "Section 4.2.1"):
        assert EntityType.PHONE not in entities_in(anonymizer, text)


def test_a_phone_number_ending_a_sentence_is_still_found() -> None:
    anonymizer = Anonymizer("strict")
    assert "[PHONE]" in anonymizer.anonymize_text("Call +44 20 7946 0958.")


def test_only_home_paths_are_redacted() -> None:
    """A technical path that names nobody must survive: redacting it would
    destroy the comment without protecting anyone."""
    anonymizer = Anonymizer("strict")
    assert "/var/log/app.log" in anonymizer.anonymize_text("See /var/log/app.log")
    assert "/home/alice" not in anonymizer.anonymize_text("See /home/alice/notes.txt")


# ─────────────────────────────────────────────────────────────────────────────
#  Names
# ─────────────────────────────────────────────────────────────────────────────


def test_a_roster_name_is_found_in_any_spelling() -> None:
    anonymizer = Anonymizer("names_only", names=["Alice Tester"])
    for spelling in ("Alice", "alice tester", "Alice Tester", "ALICE"):
        replaced = anonymizer.anonymize_text(f"Ask {spelling} about it.")
        assert spelling not in replaced, spelling


def test_every_spelling_of_one_person_gets_one_stand_in() -> None:
    """The property that makes an anonymised export still analysable."""
    anonymizer = Anonymizer("names_only", names=["Alice Tester"])
    first = anonymizer.anonymize_text("Alice raised this.")
    second = anonymizer.anonymize_text("alice tester raised it again.")
    third = anonymizer.anonymize_name("Alice Tester")
    assert first.split()[0] == second.split()[0] == third.split()[0]
    assert first.replace("raised this.", "").strip() == third


def test_an_ambiguous_first_name_only_matches_when_capitalised() -> None:
    """A reviewer called Mark must not turn 'mark the section' into a redaction."""
    anonymizer = Anonymizer("names_only", names=["Mark Jones"])
    assert "mark the section" in anonymizer.anonymize_text("Please mark the section.")
    assert "Mark" not in anonymizer.anonymize_text("Mark disagreed with this.")


def test_a_name_inside_an_email_does_not_leak_the_domain() -> None:
    """Order of resolution: the containing span must win over the one inside it.

    Replacing the ``bob.reviewer`` inside ``bob.reviewer@acme.example`` and
    leaving the rest would publish the employer while claiming to have hidden
    the person.
    """
    anonymizer = Anonymizer("strict", names=["Bob Reviewer"])
    replaced = anonymizer.anonymize_text("Mail bob.reviewer@acme.example please.")
    assert "acme.example" not in replaced
    assert "[EMAIL]" in replaced


def test_a_pseudonym_never_collides_with_a_real_name() -> None:
    """A document reviewed by a Bob *Reviewer* must not rename people to
    'Reviewer 1' — which would read as a real name and defeat the audit."""
    anonymizer = Anonymizer("names_only", names=["Bob Reviewer", "Alice Tester"])
    stand_in = anonymizer.anonymize_name("Alice Tester")
    assert "Reviewer" not in stand_in
    assert stand_in.startswith("Person")


def test_mentions_are_caught_without_a_roster() -> None:
    anonymizer = Anonymizer("strict")
    replaced = anonymizer.anonymize_text("@carol please look at this.")
    assert "@carol" not in replaced


def test_extra_names_cover_people_who_never_commented() -> None:
    policy = AnonymizationPolicy.names_only().with_(extra_names=("Dave Architect",))
    anonymizer = Anonymizer(policy, names=["Alice Tester"])
    assert "Dave" not in anonymizer.anonymize_text("Dave Architect asked for this.")


def test_roster_names_number_deterministically() -> None:
    """Same roster, same numbering, whatever order the comments arrive in."""
    forwards = Anonymizer("names_only", names=["Alice Tester", "Bob Reviewer"])
    backwards = Anonymizer("names_only", names=["Bob Reviewer", "Alice Tester"])
    assert forwards.anonymize_name("Alice Tester") == backwards.anonymize_name(
        "Alice Tester"
    )


# ─────────────────────────────────────────────────────────────────────────────
#  Span resolution
# ─────────────────────────────────────────────────────────────────────────────


def test_the_longer_span_wins_an_overlap() -> None:
    short = Span(5, 10, EntityType.PERSON, "alice", 0.9)
    long = Span(5, 22, EntityType.EMAIL, "alice@acme.example", 1.0)
    assert resolve_spans([short, long]) == [long]


def test_specificity_breaks_a_length_tie() -> None:
    weak = Span(0, 8, EntityType.PERSON, "12345678", 1.0)
    strong = Span(0, 8, EntityType.IBAN, "12345678", 1.0)
    assert resolve_spans([weak, strong]) == [strong]


def test_resolution_is_independent_of_input_order() -> None:
    spans = [
        Span(0, 5, EntityType.PERSON, "Alice", 0.9),
        Span(0, 18, EntityType.EMAIL, "alice@acme.example", 1.0),
        Span(30, 40, EntityType.PHONE, "0123456789", 0.6),
    ]
    assert resolve_spans(spans) == resolve_spans(list(reversed(spans)))


def test_resolved_spans_come_back_in_text_order() -> None:
    spans = [
        Span(20, 25, EntityType.PERSON, "Alice", 0.9),
        Span(0, 5, EntityType.PERSON, "Carol", 0.9),
    ]
    assert [span.start for span in resolve_spans(spans)] == [0, 20]


def test_adjacent_name_parts_merge_into_one() -> None:
    merged = merge_adjacent(
        [
            Span(0, 5, EntityType.PERSON, "Alice", 0.9),
            Span(6, 12, EntityType.PERSON, "Tester", 0.9),
        ]
    )
    assert len(merged) == 1
    assert merged[0].text == "Alice Tester"


def test_different_entity_types_never_merge() -> None:
    spans = [
        Span(0, 5, EntityType.PERSON, "Alice", 0.9),
        Span(6, 12, EntityType.EMAIL, "a@b.co", 1.0),
    ]
    assert len(merge_adjacent(spans)) == 2


# ─────────────────────────────────────────────────────────────────────────────
#  Policies and strategies
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("preset", PRESET_NAMES)
def test_every_preset_resolves(preset: str) -> None:
    assert isinstance(resolve_policy(preset), AnonymizationPolicy)


def test_a_hyphenated_preset_name_is_accepted() -> None:
    assert resolve_policy("names-only") == AnonymizationPolicy.names_only()


def test_true_means_the_balanced_default() -> None:
    assert resolve_policy(True) == AnonymizationPolicy.balanced()


def test_an_unknown_preset_names_the_valid_ones() -> None:
    with pytest.raises(ValueError, match="balanced"):
        resolve_policy("paranoid")


def test_a_preset_name_cannot_reach_an_arbitrary_attribute() -> None:
    """The lookup is a table, not a getattr — a CLI flag must not be a way in."""
    with pytest.raises(ValueError):
        resolve_policy("__init__")


def test_a_credential_is_never_merely_masked() -> None:
    """A partially visible API key is still a leaked API key."""
    policy = AnonymizationPolicy(
        default_strategy=Strategy.MASK,
        strategies={EntityType.API_KEY: Strategy.MASK},
    )
    assert policy.strategy_for(EntityType.API_KEY) is Strategy.REDACT
    anonymizer = Anonymizer(policy)
    replaced = anonymizer.anonymize_text("Key AKIAIOSFODNN7EXAMPLE here")
    assert "[API_KEY]" in replaced
    assert "EXAMPLE" not in replaced


def test_mask_keeps_the_tail_and_the_shape() -> None:
    policy = AnonymizationPolicy(
        default_strategy=Strategy.MASK, entities=frozenset({EntityType.CREDIT_CARD})
    )
    replaced = Anonymizer(policy).anonymize_text("Card 4111 1111 1111 1111 ok")
    assert "1111 ok" in replaced
    assert replaced.count("*") > 10
    assert "4111 1111 1111" not in replaced


def test_hash_is_stable_within_a_run_and_carries_no_value() -> None:
    anonymizer = Anonymizer(
        AnonymizationPolicy(
            default_strategy=Strategy.HASH, entities=frozenset({EntityType.EMAIL})
        )
    )
    first = anonymizer.anonymize_text("Mail a@b.example now")
    second = anonymizer.anonymize_text("Also mail a@b.example")
    token = first.split()[1]
    assert token in second
    assert "a@b.example" not in first


def test_remove_deletes_the_value() -> None:
    anonymizer = Anonymizer(
        AnonymizationPolicy(
            default_strategy=Strategy.REMOVE, entities=frozenset({EntityType.EMAIL})
        )
    )
    assert anonymizer.anonymize_text("Mail a@b.example now") == "Mail  now"


def test_keep_leaves_the_value_alone() -> None:
    policy = AnonymizationPolicy(
        default_strategy=Strategy.REDACT,
        strategies={EntityType.URL: Strategy.KEEP},
    )
    text = "See https://acme.example/spec"
    assert Anonymizer(policy).anonymize_text(text) == text


def test_secrets_only_leaves_the_people_readable() -> None:
    rows = [make_comment(0, "Alice Tester", "Alice: key AKIAIOSFODNN7EXAMPLE leaked.")]
    cleaned, _ = anonymize_comments(rows, "secrets_only")
    assert cleaned[0].author == "Alice Tester"
    assert "Alice" in cleaned[0].text
    assert "AKIAIOSFODNN7EXAMPLE" not in cleaned[0].text


def test_names_only_leaves_technical_content_alone() -> None:
    rows = [make_comment(0, "Alice Tester", "Alice: see 192.168.1.1 and card 4111111111111111.")]
    cleaned, _ = anonymize_comments(rows, "names_only")
    assert "192.168.1.1" in cleaned[0].text
    assert "4111111111111111" in cleaned[0].text
    assert "Alice" not in cleaned[0].text


def test_names_only_still_removes_email_addresses() -> None:
    """An address identifies a person as well as a name does."""
    rows = [make_comment(0, "Bob Reviewer", "Reach me at bob.reviewer@acme.example.")]
    cleaned, _ = anonymize_comments(rows, "names_only")
    assert "acme.example" not in cleaned[0].text


def test_min_score_can_exclude_the_weaker_detectors() -> None:
    strict_floor = AnonymizationPolicy.balanced().with_(min_score=0.95)
    text = "Call +44 20 7946 0958 now"
    assert "0958" in Anonymizer(strict_floor).anonymize_text(text)
    assert "0958" not in Anonymizer("balanced").anonymize_text(text)


def test_an_invalid_policy_is_rejected_at_construction() -> None:
    with pytest.raises(ValueError, match="min_score"):
        AnonymizationPolicy(min_score=2.0)
    with pytest.raises(ValueError, match="pseudonym_style"):
        AnonymizationPolicy(pseudonym_style="fancy")


# ─────────────────────────────────────────────────────────────────────────────
#  Comments
# ─────────────────────────────────────────────────────────────────────────────


def test_the_author_column_is_replaced_consistently_with_the_text() -> None:
    """The leak that survives every text-only redaction."""
    rows = [
        make_comment(0, "Alice Tester", "I think we should ship."),
        make_comment(1, "Bob Reviewer", "Alice is right about that."),
    ]
    cleaned, _ = anonymize_comments(rows, "balanced")
    assert cleaned[0].author != "Alice Tester"
    assert cleaned[0].author in cleaned[1].text


def test_initials_do_not_give_the_name_back() -> None:
    rows = [make_comment(0, "Alice Tester", "A comment.", initials="AT")]
    cleaned, _ = anonymize_comments(rows, "balanced")
    assert cleaned[0].initials != "AT"


def test_the_originals_are_never_mutated() -> None:
    rows = [make_comment(0, "Alice Tester", "Alice wrote this.")]
    anonymize_comments(rows, "strict")
    assert rows[0].author == "Alice Tester"
    assert "Alice" in rows[0].text


def test_a_comment_with_nothing_private_is_returned_unchanged() -> None:
    """Identity, not a copy: an untouched row must not cost an allocation."""
    rows = [make_comment(0, "", "The third paragraph is fine.")]
    cleaned, _ = anonymize_comments(rows, "balanced")
    assert cleaned[0] is rows[0]


def test_the_document_name_is_kept_by_default_and_hashed_by_strict() -> None:
    rows = [make_comment(0, "Alice Tester", "x", document="ProjectFalcon.docx")]
    balanced, _ = anonymize_comments(rows, "balanced")
    strict, _ = anonymize_comments(rows, "strict")
    assert balanced[0].document_name == "ProjectFalcon.docx"
    assert strict[0].document_name.startswith("document_")


def test_one_document_hashes_to_one_name() -> None:
    rows = [
        make_comment(0, "Alice Tester", "x", document="spec.docx"),
        make_comment(1, "Bob Reviewer", "y", document="spec.docx"),
    ]
    cleaned, _ = anonymize_comments(rows, "strict")
    assert cleaned[0].document_name == cleaned[1].document_name


def test_the_referenced_text_is_redacted_too() -> None:
    rows = [make_comment(0, "Alice Tester", "Fix this", referenced_text="Contact bob@acme.example")]
    cleaned, _ = anonymize_comments(rows, "strict")
    assert "bob@acme.example" not in cleaned[0].referenced_text


def test_unicode_names_are_handled() -> None:
    rows = [make_comment(0, "Νίκος Ηλιόπουλος", "Ο Νίκος συμφωνεί με αυτό.")]
    cleaned, _ = anonymize_comments(rows, "strict")
    assert "Νίκος" not in cleaned[0].text
    assert cleaned[0].author != "Νίκος Ηλιόπουλος"


# ─────────────────────────────────────────────────────────────────────────────
#  Report and verification
# ─────────────────────────────────────────────────────────────────────────────


def test_the_report_counts_what_it_replaced() -> None:
    rows = [
        make_comment(0, "Alice Tester", "Mail bob@acme.example and card 4111111111111111."),
        make_comment(1, "Bob Reviewer", "Key AKIAIOSFODNN7EXAMPLE."),
    ]
    _, report = anonymize_comments(rows, "strict")
    assert report.comments == 2
    assert report.entity_counts[EntityType.EMAIL] == 1
    assert report.entity_counts[EntityType.CREDIT_CARD] == 1
    assert report.entity_counts[EntityType.API_KEY] == 1
    assert report.replacements >= 3


def test_verification_reports_a_clean_result() -> None:
    rows = [
        make_comment(0, "Alice Tester", "Alice: mail bob@acme.example, key AKIAIOSFODNN7EXAMPLE."),
        make_comment(1, "Bob Reviewer", "Call +44 20 7946 0958 or see /home/bob/notes."),
    ]
    _, report = anonymize_comments(rows, "strict")
    assert report.clean, [str(span.entity) for span in report.residual]


def test_verification_can_be_switched_off() -> None:
    rows = [make_comment(0, "Alice Tester", "Mail a@b.example")]
    _, report = anonymize_comments(rows, "strict", verify=False)
    assert report.residual == ()


def test_the_report_summary_names_the_policy_and_the_counts() -> None:
    rows = [make_comment(0, "Alice Tester", "Mail a@b.example")]
    _, report = anonymize_comments(rows, "strict")
    summary = report.summary()
    assert "strict" in summary
    assert "EMAIL" in summary
    assert "Verified" in summary


def test_the_report_serialises_without_any_original_value() -> None:
    rows = [make_comment(0, "Alice Tester", "Mail bob@acme.example")]
    _, report = anonymize_comments(rows, "strict")
    payload = json.dumps(report.to_dict())
    assert "bob@acme.example" not in payload
    assert "Alice" not in payload


def test_a_custom_policy_is_reported_as_custom() -> None:
    policy = AnonymizationPolicy(default_strategy=Strategy.REMOVE)
    _, report = anonymize_comments([make_comment()], policy)
    assert report.policy_name == "custom"


# ─────────────────────────────────────────────────────────────────────────────
#  Scanning
# ─────────────────────────────────────────────────────────────────────────────


def test_a_scan_changes_nothing_and_finds_everything() -> None:
    rows = [
        make_comment(0, "Alice Tester", "Key AKIAIOSFODNN7EXAMPLE and mail a@b.example."),
    ]
    scan = scan_comments(rows)
    assert scan.total >= 2
    assert scan.has_secrets
    assert rows[0].text.startswith("Key AKIA")


def test_a_scan_report_carries_no_values() -> None:
    rows = [make_comment(0, "Alice Tester", "Mail secret.person@acme.example")]
    summary = scan_comments(rows).summary()
    assert "secret.person" not in summary
    assert "chars" in summary


def test_a_clean_document_scans_clean() -> None:
    rows = [make_comment(0, "", "The third paragraph reads well.")]
    scan = scan_comments(rows)
    assert scan.total == 0
    assert not scan.has_secrets
    assert "nothing detected" in scan.summary()


# ─────────────────────────────────────────────────────────────────────────────
#  Vault and reversal
# ─────────────────────────────────────────────────────────────────────────────


def test_a_model_answer_can_be_de_anonymised() -> None:
    """The workflow the whole feature exists for."""
    rows = [
        make_comment(0, "Alice Tester", "This needs work."),
        make_comment(1, "Bob Reviewer", "Agreed."),
    ]
    engine = Anonymizer("balanced", names=["Alice Tester", "Bob Reviewer"])
    cleaned, _ = anonymize_comments(rows, anonymizer=engine)

    answer = f"{cleaned[0].author} raised it and {cleaned[1].author} agreed."
    restored = engine.deanonymize(answer)
    assert restored == "Alice Tester raised it and Bob Reviewer agreed."


def test_reversal_does_not_corrupt_a_longer_stand_in() -> None:
    """'Person 1' must not eat the front of 'Person 11'."""
    names = [f"Reviewer Number{index}" for index in range(1, 13)]
    engine = Anonymizer("names_only", names=names)
    stand_ins = [engine.anonymize_name(name) for name in names]
    restored = engine.deanonymize(" and ".join(stand_ins))
    for name in names:
        assert name in restored


def test_a_redacted_value_is_not_reversible() -> None:
    engine = Anonymizer("strict")
    cleaned = engine.anonymize_text("Mail bob@acme.example")
    assert "bob@acme.example" not in engine.deanonymize(cleaned)


def test_the_vault_round_trips_through_a_file(tmp_path: Path) -> None:
    engine = Anonymizer("balanced", names=["Alice Tester"])
    stand_in = engine.anonymize_name("Alice Tester")

    target = engine.vault.export(tmp_path / "vault.json")
    loaded = Vault.load(target)
    assert loaded.original_for(stand_in) == "Alice Tester"


def test_the_vault_file_warns_about_itself(tmp_path: Path) -> None:
    engine = Anonymizer("balanced", names=["Alice Tester"])
    engine.anonymize_name("Alice Tester")
    payload = json.loads(engine.vault.export(tmp_path / "v.json").read_text("utf-8"))
    assert "re-identifies" in payload["warning"]


@pytest.mark.skipif(os.name == "nt", reason="POSIX file modes only")
def test_the_vault_file_is_private(tmp_path: Path) -> None:
    engine = Anonymizer("balanced", names=["Alice Tester"])
    engine.anonymize_name("Alice Tester")
    target = engine.vault.export(tmp_path / "vault.json")
    assert target.stat().st_mode & 0o077 == 0


def test_a_supplied_key_gives_stable_tokens_across_runs() -> None:
    policy = AnonymizationPolicy.strict()
    first = Anonymizer(policy, names=["Alice Tester"], key=b"k" * 32)
    second = Anonymizer(policy, names=["Alice Tester"], key=b"k" * 32)
    assert first.anonymize_name("Alice Tester") == second.anonymize_name("Alice Tester")


def test_without_a_key_two_runs_differ() -> None:
    """The safe default: a token stable across everything you ever exported is
    itself a persistent identifier."""
    policy = AnonymizationPolicy.strict()
    first = Anonymizer(policy, names=["Alice Tester"])
    second = Anonymizer(policy, names=["Alice Tester"])
    assert first.anonymize_name("Alice Tester") != second.anonymize_name("Alice Tester")


def test_one_anonymizer_spans_several_documents() -> None:
    engine = Anonymizer("balanced", names=["Alice Tester"])
    first, _ = anonymize_comments(
        [make_comment(0, "Alice Tester", "x", document="a.docx")], anonymizer=engine
    )
    second, _ = anonymize_comments(
        [make_comment(0, "Alice Tester", "y", document="b.docx")], anonymizer=engine
    )
    assert first[0].author == second[0].author


# ─────────────────────────────────────────────────────────────────────────────
#  Extensibility
# ─────────────────────────────────────────────────────────────────────────────


def test_custom_terms_are_redacted() -> None:
    policy = AnonymizationPolicy.balanced().with_(custom_terms=("Project Falcon",))
    replaced = Anonymizer(policy).anonymize_text("Project Falcon ships in May.")
    assert "Falcon" not in replaced


def test_a_custom_pattern_is_redacted() -> None:
    policy = AnonymizationPolicy.balanced().with_(
        custom_patterns=(("customer-id", r"CUST-\d{6}"),)
    )
    replaced = Anonymizer(policy).anonymize_text("Raised by CUST-004417 today.")
    assert "CUST-004417" not in replaced


def test_a_caller_supplied_recognizer_is_used() -> None:
    """The Presidio / spaCy extension point, exercised with a trivial detector."""
    recognizer = RegexRecognizer(
        "ticket", EntityType.CUSTOM, r"JIRA-\d+", score=1.0
    )
    engine = Anonymizer("strict", recognizers=[recognizer])
    assert "JIRA-1234" not in engine.anonymize_text("See JIRA-1234 for detail.")


def test_a_term_recognizer_can_target_an_entity_type() -> None:
    recognizer = TermRecognizer(["Acme Corp"], EntityType.ORGANIZATION)
    engine = Anonymizer("balanced", recognizers=[recognizer])
    replaced = engine.anonymize_text("Acme Corp signed off.")
    assert "Acme Corp" not in replaced


def test_a_name_recognizer_maps_spellings_to_the_roster_name() -> None:
    recognizer = NameRecognizer(["Alice Tester"])
    assert recognizer.canonical("alice") == "Alice Tester"
    assert recognizer.canonical("Alice Tester") == "Alice Tester"
    assert recognizer.canonical("Zoë") is None


# ─────────────────────────────────────────────────────────────────────────────
#  End to end, through a real document
# ─────────────────────────────────────────────────────────────────────────────


@pytest.fixture(scope="module")
def sensitive_docx(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A document carrying one of everything the detectors look for."""
    directory = tmp_path_factory.mktemp("privacy")
    specs = [
        CommentSpec(
            id=0, author="Alice Tester", initials="AT",
            text="Alice here: mail me at alice.tester@acme.example or +44 20 7946 0958.",
            anchor="The parties agree to the following terms.",
        ),
        CommentSpec(
            id=1, author="Bob Reviewer", initials="BR", parent=0,
            text="Key AKIAIOSFODNN7EXAMPLE is in the sample. Card 4111 1111 1111 1111 too.",
        ),
        CommentSpec(
            id=2, author="Νίκος Ηλιόπουλος", initials="ΝΗ",
            text="Ο Νίκος: δες C:\\Users\\nikos\\draft.docx και το 192.168.0.5.",
            anchor="Δεδομένα πελατών",
        ),
    ]
    return build_docx(directory / "sensitive.docx", specs)


def test_parser_anonymize_end_to_end(sensitive_docx: Path) -> None:
    parser = dcp.DocxParser()
    parser.parse(str(sensitive_docx))

    cleaned, report = parser.anonymize("strict")
    joined = " ".join(f"{c.author} {c.text} {c.referenced_text}" for c in cleaned)

    for leaked in (
        "alice.tester@acme.example", "AKIAIOSFODNN7EXAMPLE",
        "4111 1111 1111 1111", "Alice", "Νίκος", "nikos", "192.168.0.5",
    ):
        assert leaked not in joined, leaked
    assert report.clean, [str(span.entity) for span in report.residual]


def test_parser_privacy_scan_end_to_end(sensitive_docx: Path) -> None:
    parser = dcp.DocxParser()
    parser.parse(str(sensitive_docx))

    scan = parser.privacy_scan()
    assert scan.has_secrets
    assert EntityType.EMAIL in scan.findings
    assert EntityType.CREDIT_CARD in scan.findings


def test_batch_anonymize_shares_one_set_of_stand_ins(
    tmp_path: Path, batch_dir: Path
) -> None:
    parser = dcp.BatchParser()
    parser.parse_all([str(path) for path in sorted(batch_dir.glob("doc*.docx"))])

    cleaned, report = parser.anonymize("balanced")
    authors = {comment.author for comment in cleaned}
    # Three documents built from the same three-author roster: anonymising them
    # together must not produce nine people.
    assert len(authors) == 3
    assert report.clean


# ─────────────────────────────────────────────────────────────────────────────
#  Scale
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.perf
def test_ten_thousand_comments_anonymise_in_reasonable_time(large_docx: Path) -> None:
    import time

    parser = dcp.DocxParser()
    parser.parse(str(large_docx))
    comments = parser.to_comments()

    start = time.perf_counter()
    cleaned, report = parser.anonymize("balanced")
    elapsed = time.perf_counter() - start

    assert len(cleaned) == len(comments)
    assert report.clean
    # Generous, because CI machines are not fast; the point is that the scan is
    # linear and cached, not that it hits a particular number.
    assert elapsed < 60.0, f"anonymising 10,000 comments took {elapsed:.1f}s"


@pytest.mark.perf
def test_repeated_authors_are_cached(large_docx: Path) -> None:
    """The author column repeats on every row; scanning it 10,000 times would
    be the difference between a fast export and a slow one."""
    parser = dcp.DocxParser()
    parser.parse(str(large_docx))
    engine = Anonymizer("balanced", names=["Alice Tester"])

    engine.anonymize_text("Alice Tester")
    hits_before = len(engine._cache)
    for _ in range(1000):
        engine.anonymize_text("Alice Tester")
    assert len(engine._cache) == hits_before
