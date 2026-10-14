"""Review-time checks for capability artifacts (``cua validate``)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from cua.artifact.capability import Capability
from cua.artifact.common import Risk, Status
from cua.artifact.io import to_yaml
from cua.artifact.steps import FillStep, SelectStep
from cua.artifact.targets import CssLocator
from cua.artifact.values import ParamValue
from cua.safety.policy import ActionType, Policy
from cua.safety.redaction import Redactor

Level = Literal["error", "warning", "info"]


@dataclass(frozen=True)
class Finding:
    level: Level
    where: str
    message: str


def lint(capability: Capability, *, policy: Policy | None = None, redactor: Redactor | None = None) -> list[Finding]:
    findings: list[Finding] = []
    meta = capability.metadata
    digest = capability.compute_digest()
    if meta.digest is None:
        findings.append(Finding("warning", "metadata.digest", "missing; run `cua approve` or re-record"))
    elif meta.digest != digest:
        findings.append(
            Finding(
                "error", "metadata.digest", "spec changed since the digest was computed; bump the version and re-digest"
            )
        )
    if meta.status is Status.APPROVED and not capability.is_approved():
        findings.append(
            Finding("error", "metadata.approvals", "status is approved but no approval matches the current digest")
        )

    used_inputs: set[str] = set()
    for step in capability.spec.steps:
        where = f"steps.{step.id}"
        target = getattr(step, "target", None)
        if target is not None:
            if all(isinstance(loc, CssLocator) for loc in target.locators):
                findings.append(Finding("warning", where, "only a CSS locator: brittle across versions/tenants"))
            elif len(target.locators) == 1:
                findings.append(Finding("info", where, "single locator strategy - no fallback"))
        if step.source == "human":
            findings.append(Finding("warning", where, "recorded from operator actions: locators were not verified"))
        if isinstance(step, FillStep | SelectStep):
            value = step.value if isinstance(step, FillStep) else step.option
            if isinstance(value, ParamValue):
                used_inputs.add(value.param)
        if policy is not None and target is not None:
            label = " ".join(
                str(getattr(loc, f))
                for loc in target.locators
                for f in ("name", "label", "text")
                if getattr(loc, f, None)
            )
            effective = policy.classify(ActionType(step.action), f"{target.description} {label}", step.risk)
            if effective is not step.risk:
                findings.append(
                    Finding(
                        "warning",
                        where,
                        f"declared {step.risk} but policy classifies it as "
                        f"{effective}; it will be treated as {effective}",
                    )
                )
        if step.risk is Risk.IRREVERSIBLE and meta.status is not Status.APPROVED:
            findings.append(Finding("info", where, "irreversible: unattended replay requires an approved capability"))
    for unused in sorted({i.name for i in capability.spec.inputs} - used_inputs):
        findings.append(Finding("warning", f"inputs.{unused}", "declared but never used by a step"))
    if redactor is not None:
        text = to_yaml(capability)
        if redactor.text(text) != text:
            findings.append(Finding("error", "artifact", "contains data matching sensitive-data patterns"))
    return findings
