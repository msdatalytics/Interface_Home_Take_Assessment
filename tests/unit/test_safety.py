from __future__ import annotations

from pathlib import Path

import pytest

from cua.artifact.common import Risk
from cua.artifact.io import load_as
from cua.safety.policy import ActionType, Mode, Policy, PolicyDocument, RiskRule
from cua.safety.redaction import Redactor

REPO = Path(__file__).resolve().parents[2]


@pytest.fixture
def policy() -> Policy:
    return Policy(load_as(PolicyDocument, REPO / "config" / "policy.yaml").spec, "http://127.0.0.1:8401")


@pytest.fixture
def redactor() -> Redactor:
    return Redactor(fingerprint_key=b"test")


# ---------------------------------------------------------------- allowlist


@pytest.mark.parametrize(
    ("url", "allowed"),
    [
        ("http://127.0.0.1:8401/cb/main.asp", True),
        ("http://127.0.0.1:8401/cb/mbr_detail.asp?m=1", True),
        ("http://127.0.0.1:8401/__admin/faults", False),
        ("http://127.0.0.1:8401/cb/logoff.asp", False),
        ("http://127.0.0.1:8402/cb/main.asp", False),  # another tenant's origin
        ("https://evil.example.com/cb/main.asp", False),
        ("http://127.0.0.1:8401/other/app", False),
        ("data:image/png;base64,AAAA", True),
    ],
)
def test_url_allowlist(policy: Policy, url: str, allowed: bool) -> None:
    assert policy.check_url(url).allowed is allowed


# ---------------------------------------------------------------- risk


@pytest.mark.parametrize(
    ("label", "declared", "expected"),
    [
        ("Search", Risk.READ, Risk.READ),
        ("Continue", Risk.READ, Risk.WRITE),
        ("Submit", Risk.READ, Risk.IRREVERSIBLE),
        ("Post Transaction", Risk.READ, Risk.IRREVERSIBLE),
        ("Close Account", Risk.WRITE, Risk.IRREVERSIBLE),
        ("Back to Search", Risk.IRREVERSIBLE, Risk.IRREVERSIBLE),  # declared risk is never downgraded
        ("Postal code lookup", Risk.READ, Risk.READ),  # whole words only
    ],
)
def test_effective_risk_is_max_of_declared_and_inferred(
    policy: Policy, label: str, declared: Risk, expected: Risk
) -> None:
    assert policy.classify(ActionType.CLICK, label, declared) is expected


def test_fills_are_not_inferred_as_commits(policy: Policy) -> None:
    assert policy.classify(ActionType.FILL, "Submit comment", Risk.READ) is Risk.READ


def test_irreversible_needs_a_human_in_discovery(policy: Policy) -> None:
    decision = policy.decide(Mode.DISCOVERY, ActionType.CLICK, Risk.IRREVERSIBLE)
    assert not decision.allowed
    assert decision.rule is RiskRule.REQUIRE_HUMAN_APPROVAL
    assert decision.needs_human


def test_irreversible_replay_requires_an_approved_artifact(policy: Policy) -> None:
    assert not policy.decide(Mode.REPLAY, ActionType.CLICK, Risk.IRREVERSIBLE).allowed
    assert policy.decide(Mode.REPLAY, ActionType.CLICK, Risk.IRREVERSIBLE, artifact_approved=True).allowed
    assert policy.decide(Mode.REPLAY, ActionType.CLICK, Risk.WRITE).allowed


# ---------------------------------------------------------------- redaction


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("SSN 123-45-6789", "SSN [REDACTED:ssn]"),
        ("card 4111 1111 1111 1111", "card [REDACTED:card]"),
        ("mail jo.r@cu-alpha.org now", "mail [REDACTED:email] now"),
        ("Authorization: Bearer abcdefghijklmnop", "Authorization: [REDACTED:bearer]"),
        ("acct 004417291834", "acct [REDACTED:account_number]"),
    ],
)
def test_pattern_redaction(redactor: Redactor, text: str, expected: str) -> None:
    assert redactor.text(text) == expected


@pytest.mark.parametrize(
    "text",
    [
        "4111 1111 1111 1112",  # fails Luhn: not a card
        "corebank.member.get_savings_balance@1.0.0",  # capability refs are not e-mail addresses
        "Balance $2,481.37 on 2024-05-01",
        "CB-48211",
    ],
)
def test_no_false_positives_on_operational_text(redactor: Redactor, text: str) -> None:
    assert redactor.text(text) == text


def test_registered_values_are_masked_everywhere(redactor: Redactor) -> None:
    redactor.register("100234", "param:member_id")
    redactor.register("S0", "too-short-to-mask")
    assert redactor.text("member 100234 has S0") == "member [param:member_id] has S0"
    assert redactor.obj({"a": ["x 100234"], "password": "hunter2", "n": 3}) == {
        "a": ["x [param:member_id]"],
        "password": "[REDACTED]",
        "n": 3,
    }


def test_fingerprints_are_keyed_and_stable() -> None:
    a, b = Redactor(fingerprint_key=b"k1"), Redactor(fingerprint_key=b"k2")
    assert a.fingerprint("2481.37") == a.fingerprint("2481.37")
    assert a.fingerprint("2481.37") != b.fingerprint("2481.37")
    assert "2481" not in a.fingerprint("2481.37")
