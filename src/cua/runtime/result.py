"""The replay result contract returned to the calling agent.

Three mutually exclusive statuses - the distinction the caller actually needs:

* ``succeeded``        - goal reached, checkpoint verified, typed ``outputs`` returned.
* ``business_outcome`` - the application gave a legitimate answer that is not success
                          (``record_not_found``, ``access_denied``, ``validation_error`` ...).
                          Not an error of the automation; the caller decides what to do.
* ``failed``           - the automation could not complete. ``failure.category`` says *whose*
                          problem it is and ``failure.retryable`` whether retrying can help.

Recoverable conditions (dismissed interstitials, retried transient errors, re-login after
session expiry) do not change the status; they are reported in ``recoveries`` so operators
can see how hard the run had to work.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from cua.artifact.capability import Capability
from cua.artifact.common import Sensitivity
from cua.safety.redaction import Redactor


class RunStatus(StrEnum):
    SUCCEEDED = "succeeded"
    BUSINESS_OUTCOME = "business_outcome"
    FAILED = "failed"


class FailureCategory(StrEnum):
    CALLER = "caller"  # bad inputs, unapproved capability - fix the request
    POLICY = "policy"  # guardrail refused - by design
    APPLICATION = "application"  # the app errored / was unavailable
    AUTOMATION = "automation"  # the flow no longer matches the app (drift) - fix the artifact
    OPERATOR = "operator"  # a human aborted or never responded
    INTERNAL = "internal"  # bug in this system


class FailureCode(StrEnum):
    INPUT_INVALID = "INPUT_INVALID"
    APPROVAL_REQUIRED = "APPROVAL_REQUIRED"
    POLICY_DENIED = "POLICY_DENIED"
    VERSION_UNSUPPORTED = "VERSION_UNSUPPORTED"
    APP_UNREACHABLE = "APP_UNREACHABLE"
    SESSION_FAILED = "SESSION_FAILED"
    TARGET_NOT_FOUND = "TARGET_NOT_FOUND"
    TARGET_AMBIGUOUS = "TARGET_AMBIGUOUS"
    ACTION_FAILED = "ACTION_FAILED"
    CHECKPOINT_FAILED = "CHECKPOINT_FAILED"
    OUTPUT_INVALID = "OUTPUT_INVALID"
    APP_ERROR = "APP_ERROR"
    OUTCOME_UNKNOWN = "OUTCOME_UNKNOWN"
    RECOVERY_EXHAUSTED = "RECOVERY_EXHAUSTED"
    OPERATOR_ABORTED = "OPERATOR_ABORTED"
    ESCALATION_TIMEOUT = "ESCALATION_TIMEOUT"
    INTERNAL_ERROR = "INTERNAL_ERROR"


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class LocatorAttemptRecord(_Model):
    strategy: str
    description: str
    matches: int
    error: str | None = None


class Failure(_Model):
    code: FailureCode
    category: FailureCategory
    message: str
    retryable: bool = False
    step_id: str | None = None
    step_index: int | None = None
    step_title: str | None = None
    expected: str | None = None
    observed: str | None = None
    locator_attempts: list[LocatorAttemptRecord] = Field(default_factory=list)
    evidence: dict[str, Any] = Field(default_factory=dict)


class BusinessOutcome(_Model):
    code: str
    message: str | None = None
    description: str | None = None
    step_id: str | None = None
    declared: bool = Field(default=True, description="False if the outcome is not in the capability contract.")


class RecoveryRecord(_Model):
    handler: str
    kind: str
    step_id: str | None
    attempt: int
    detail: str


class DriftRecord(_Model):
    kind: str
    step_id: str | None = None
    detail: str


class InterventionRecord(_Model):
    id: str
    kind: str
    reason: str
    operator: str | None
    resolution: str | None
    human_actions: int = 0
    waited_ms: int = 0


class StepRecord(_Model):
    id: str
    title: str
    status: str
    risk: str
    strategy: str | None = None
    duration_ms: int = 0
    screenshot: str | None = None


class RunResult(_Model):
    run_id: str
    capability: str
    digest: str | None
    tenant: str
    tenant_overrides: list[str] = Field(default_factory=list)
    status: RunStatus
    outputs: dict[str, Any] = Field(default_factory=dict)
    outcome: BusinessOutcome | None = None
    failure: Failure | None = None
    recoveries: list[RecoveryRecord] = Field(default_factory=list)
    drift: list[DriftRecord] = Field(default_factory=list)
    interventions: list[InterventionRecord] = Field(default_factory=list)
    steps: list[StepRecord] = Field(default_factory=list)
    human_assisted: bool = False
    app_version: str | None = None
    started_at: datetime
    duration_ms: int = 0
    evidence_dir: str | None = None

    def for_persistence(self, capability: Capability, redactor: Redactor) -> dict[str, Any]:
        """Outputs whose sensitivity forbids clear-text persistence are replaced by keyed fingerprints."""
        data = self.model_dump(mode="json", exclude_none=True)
        outputs: dict[str, Any] = {}
        for name, value in self.outputs.items():
            spec = capability.output(name)
            sensitivity = spec.sensitivity if spec else Sensitivity.CONFIDENTIAL
            outputs[name] = (
                value
                if sensitivity.persist_in_clear
                else {
                    "redacted": True,
                    "sensitivity": sensitivity.value,
                    "fingerprint": redactor.fingerprint(value),
                }
            )
        data["outputs"] = outputs
        return data
