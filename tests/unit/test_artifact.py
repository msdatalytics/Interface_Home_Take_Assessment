from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from cua.artifact.capability import Approval, Capability
from cua.artifact.common import Status
from cua.artifact.io import load_capability, load_yaml, to_yaml
from cua.artifact.lint import lint
from cua.safety.redaction import Redactor

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "get_savings_balance.yaml"


@pytest.fixture
def capability() -> Capability:
    return load_capability(FIXTURE)


@pytest.fixture
def raw() -> dict[str, Any]:
    data: dict[str, Any] = load_yaml(FIXTURE)
    return data


def approve(cap: Capability) -> Capability:
    digest = cap.compute_digest()
    approval = Approval(by="reviewer", at=datetime.now(UTC), digest=digest)
    return cap.model_copy(
        update={"metadata": cap.metadata.model_copy(update={"status": Status.APPROVED, "approvals": [approval]})}
    )


def test_yaml_round_trip_is_lossless(capability: Capability) -> None:
    again = Capability.model_validate(load_yaml_text(to_yaml(capability)))
    assert again == capability
    assert to_yaml(again) == to_yaml(capability)


def load_yaml_text(text: str) -> Any:
    import yaml

    return yaml.safe_load(text)


def test_locators_are_ranked_most_stable_first(capability: Capability) -> None:
    for step in capability.spec.steps:
        target = getattr(step, "target", None)
        if target is None:
            continue
        stabilities = [loc.STABILITY for loc in target.locators]
        assert stabilities == sorted(stabilities, reverse=True), step.id


def test_digest_is_stable_and_content_addressed(capability: Capability) -> None:
    assert capability.compute_digest() == capability.compute_digest()
    assert capability.compute_digest().startswith("sha256:")
    retitled = capability.model_copy(
        update={"metadata": capability.metadata.model_copy(update={"tags": ["something-else"]})}
    )
    assert retitled.compute_digest() == capability.compute_digest(), "tags are not behaviour"
    steps = list(capability.spec.steps)
    steps[0] = steps[0].model_copy(update={"timeout_ms": 1234})
    changed = capability.model_copy(update={"spec": capability.spec.model_copy(update={"steps": steps})})
    assert changed.compute_digest() != capability.compute_digest()


def test_approval_is_bound_to_digest(capability: Capability) -> None:
    assert capability.approval_state() == "unapproved"
    approved = approve(capability)
    assert approved.is_approved()
    steps = list(approved.spec.steps)
    steps[1] = steps[1].model_copy(update={"timeout_ms": 1})
    tampered = approved.model_copy(update={"spec": approved.spec.model_copy(update={"steps": steps})})
    assert tampered.approval_state() == "stale"
    assert not tampered.is_approved()
    assert any(f.level == "error" and f.where == "metadata.approvals" for f in lint(tampered))


def test_unknown_fields_are_rejected(raw: dict[str, Any]) -> None:
    raw["spec"]["steps"][0]["selector_hint"] = "#foo"
    with pytest.raises(ValidationError, match="selector_hint"):
        Capability.model_validate(raw)


def test_steps_must_reference_declared_inputs(raw: dict[str, Any]) -> None:
    raw["spec"]["steps"][1]["value"] = {"param": "account_no"}
    with pytest.raises(ValidationError, match="account_no"):
        Capability.model_validate(raw)


def test_steps_must_produce_declared_outputs(raw: dict[str, Any]) -> None:
    raw["spec"]["steps"][-1]["output"] = "undeclared"
    with pytest.raises(ValidationError, match="undeclared"):
        Capability.model_validate(raw)


def test_step_ids_are_unique(raw: dict[str, Any]) -> None:
    raw["spec"]["steps"][1]["id"] = raw["spec"]["steps"][0]["id"]
    with pytest.raises(ValidationError, match="duplicate"):
        Capability.model_validate(raw)


def test_lint_flags_sensitive_literals(raw: dict[str, Any]) -> None:
    raw["spec"]["steps"][1]["value"] = {"literal": "123-45-6789"}
    cap = Capability.model_validate(raw)
    findings = lint(cap, redactor=Redactor(fingerprint_key=b"k"))
    assert any(f.level == "error" and "sensitive" in f.message for f in findings)


def test_clean_artifact_lints_clean(capability: Capability) -> None:
    errors = [f for f in lint(capability, redactor=Redactor(fingerprint_key=b"k")) if f.level == "error"]
    assert errors == []
