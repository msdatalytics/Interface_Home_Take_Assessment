"""The Capability artifact: a typed, versioned, reviewable, agent-invocable flow."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from typing import Annotated, Literal

from pydantic import Field, model_validator

from cua.artifact.common import API_VERSION, Risk, Sensitivity, Status, Strict, ValueType
from cua.artifact.conditions import Condition
from cua.artifact.steps import ExtractStep, FillStep, SelectStep, Step
from cua.artifact.values import ParamValue

Identifier = Annotated[str, Field(pattern=r"^[a-z][a-z0-9_]{0,63}$")]
CapabilityName = Annotated[str, Field(pattern=r"^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)+$")]
SemVer = Annotated[str, Field(pattern=r"^\d+\.\d+\.\d+$")]


class InputSpec(Strict):
    name: Identifier
    type: ValueType
    description: str
    required: bool = True
    sensitivity: Sensitivity = Sensitivity.INTERNAL
    pattern: str | None = Field(default=None, description="Regex the value must fully match.")
    enum: list[str] | None = None
    max_length: int | None = Field(default=None, ge=1)
    example: str | None = Field(default=None, description="Synthetic example only - never real data.")

    @model_validator(mode="after")
    def _enum_values(self) -> InputSpec:
        if self.type is ValueType.ENUM and not self.enum:
            raise ValueError(f"input '{self.name}': type enum requires 'enum' values")
        if self.example is not None and self.sensitivity in (Sensitivity.PII, Sensitivity.SECRET):
            raise ValueError(f"input '{self.name}': examples are not allowed for {self.sensitivity} inputs")
        return self


class OutputSpec(Strict):
    name: Identifier
    type: ValueType
    description: str
    sensitivity: Sensitivity = Sensitivity.INTERNAL
    required: bool = True


class OutcomeSpec(Strict):
    """A *business* outcome: a legitimate answer the caller must handle (not a crash)."""

    code: Identifier
    description: str
    detect: Condition
    retryable: bool = Field(default=False, description="Whether the caller may retry with the same inputs.")


class EntrySpec(Strict):
    requires_session: bool = True
    path: str | None = Field(
        default=None,
        description="Start page relative to base URL; defaults to the app profile's session entry path.",
    )


class Approval(Strict):
    by: str
    at: datetime
    digest: str = Field(description="Spec digest the approval is bound to; edits invalidate it.")
    note: str | None = None


class Provenance(Strict):
    method: Literal["llm_discovery", "authored", "imported"]
    run_id: str | None = None
    recorded_at: datetime
    recorded_on_tenant: str | None = None
    recorded_on_app_version: str | None = None
    model: str | None = None
    goal: str | None = Field(default=None, description="Natural-language goal (redacted).")
    human_assisted: bool = False


class CapabilityMetadata(Strict):
    name: CapabilityName
    version: SemVer
    title: str
    description: str
    app: str = Field(description="App profile (vendor product) this capability targets.")
    app_versions: str = Field(default="*", description="PEP 440-style specifier, e.g. '>=4.2,<5'.")
    status: Status = Status.DRAFT
    approvals: list[Approval] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)
    provenance: Provenance
    digest: str | None = Field(
        default=None, description="sha256 of the canonical spec (see Capability.compute_digest)."
    )


class CapabilitySpec(Strict):
    inputs: list[InputSpec] = Field(default_factory=list)
    outputs: list[OutputSpec] = Field(default_factory=list)
    outcomes: list[OutcomeSpec] = Field(default_factory=list)
    entry: EntrySpec = Field(default_factory=EntrySpec)
    steps: list[Step] = Field(min_length=1)
    success: Condition = Field(description="Final checkpoint proving the goal was reached.")


class Capability(Strict):
    api_version: Literal["cua.dev/v1"] = Field(default=API_VERSION, alias="apiVersion")
    kind: Literal["Capability"] = "Capability"
    metadata: CapabilityMetadata
    spec: CapabilitySpec

    # ----------------------------------------------------------------- integrity

    @model_validator(mode="after")
    def _referential_integrity(self) -> Capability:
        inputs = {i.name for i in self.spec.inputs}
        outputs = {o.name for o in self.spec.outputs}
        step_ids: set[str] = set()
        extracted: set[str] = set()
        for step in self.spec.steps:
            if step.id in step_ids:
                raise ValueError(f"duplicate step id '{step.id}'")
            step_ids.add(step.id)
            value = step.value if isinstance(step, FillStep) else step.option if isinstance(step, SelectStep) else None
            if isinstance(value, ParamValue) and value.param not in inputs:
                raise ValueError(f"step '{step.id}' references undeclared input '{value.param}'")
            if isinstance(step, ExtractStep):
                if step.output not in outputs:
                    raise ValueError(f"step '{step.id}' extracts undeclared output '{step.output}'")
                extracted.add(step.output)
        missing = {o.name for o in self.spec.outputs if o.required} - extracted
        if missing:
            raise ValueError(f"required outputs never extracted: {sorted(missing)}")
        return self

    def compute_digest(self) -> str:
        """Content address of everything that affects behaviour (spec + identity), excluding
        lifecycle metadata such as status/approvals so approving does not change the digest."""
        payload = {
            "name": self.metadata.name,
            "version": self.metadata.version,
            "app": self.metadata.app,
            "app_versions": self.metadata.app_versions,
            "spec": self.spec.model_dump(mode="json", by_alias=True, exclude_none=True),
        }
        canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        return "sha256:" + hashlib.sha256(canonical.encode()).hexdigest()

    def with_digest(self) -> Capability:
        return self.model_copy(update={"metadata": self.metadata.model_copy(update={"digest": self.compute_digest()})})

    # ----------------------------------------------------------------- derived facts

    @property
    def ref(self) -> str:
        return f"{self.metadata.name}@{self.metadata.version}"

    @property
    def max_risk(self) -> Risk:
        return Risk.highest(*(s.risk for s in self.spec.steps))

    def is_approved(self) -> bool:
        """Approved *and* the approval is bound to the current content digest."""
        return self.approval_state() == "approved"

    def approval_state(self) -> Literal["approved", "unapproved", "stale"]:
        """``stale``: marked approved, but the content changed after the approval was granted."""
        if self.metadata.status is not Status.APPROVED or not self.metadata.approvals:
            return "unapproved"
        digest = self.compute_digest()
        return "approved" if any(a.digest == digest for a in self.metadata.approvals) else "stale"

    def input(self, name: str) -> InputSpec | None:
        return next((i for i in self.spec.inputs if i.name == name), None)

    def output(self, name: str) -> OutputSpec | None:
        return next((o for o in self.spec.outputs if o.name == name), None)
