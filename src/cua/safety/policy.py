"""Guardrail policy: what the automation may touch, and how risky actions are treated.

Enforcement happens at three independent layers so a single bug cannot bypass it:

1. **Pre-action** - every step (LLM-proposed or replayed) is checked against the action
   allowlist and its risk class before it runs.
2. **Network** - the browser context routes every request through :meth:`Policy.check_url`;
   navigations/sub-resources outside the allowlist are aborted even if a click triggers them.
3. **Risk escalation** - risk is the *maximum* of what the recording declares and what the
   policy infers from the control's label, so a model (or an edited artifact) cannot
   downgrade an irreversible "Submit" to a harmless click.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum
from fnmatch import fnmatchcase
from typing import Literal
from urllib.parse import urlsplit

from pydantic import Field

from cua.artifact.common import API_VERSION, Risk, Strict


class ActionType(StrEnum):
    NAVIGATE = "navigate"
    CLICK = "click"
    FILL = "fill"
    SELECT = "select"
    PRESS = "press"
    EXTRACT = "extract"


class Mode(StrEnum):
    DISCOVERY = "discovery"
    REPLAY = "replay"


class RiskRule(StrEnum):
    ALLOW = "allow"
    REQUIRE_HUMAN_APPROVAL = "require_human_approval"
    """Pause and route an approval request to an operator for this specific action."""
    REQUIRE_APPROVED_ARTIFACT = "require_approved_artifact"
    """Allowed unattended only if the capability is approved and the approval matches its digest."""
    DENY = "deny"


class RiskRules(Strict):
    read: RiskRule = RiskRule.ALLOW
    write: RiskRule = RiskRule.ALLOW
    irreversible: RiskRule = RiskRule.REQUIRE_HUMAN_APPROVAL

    def for_risk(self, risk: Risk) -> RiskRule:
        rule: RiskRule = getattr(self, risk.value)
        return rule


class RedactionSpec(Strict):
    patterns: dict[str, str] = Field(default_factory=dict, description="name -> regex of values to mask anywhere.")
    sensitive_labels: list[str] = Field(
        default_factory=list,
        description="Regexes on field labels whose *values* are masked (e.g. 'SSN', 'Date of Birth').",
    )
    screenshot_to_llm: bool = Field(
        default=False,
        description="Send screenshots to the model. Off by default: pixels cannot be redacted reliably.",
    )


class PolicySpec(Strict):
    allowed_origins: list[str] = Field(
        description="Origins the automation may load. The token '{tenant}' expands to the tenant app origin."
    )
    allowed_paths: list[str] = Field(default_factory=lambda: ["/*"])
    denied_paths: list[str] = Field(default_factory=list)
    allowed_actions: list[ActionType] = Field(default_factory=lambda: list(ActionType))
    irreversible_keywords: list[str] = Field(default_factory=list)
    write_keywords: list[str] = Field(default_factory=list)
    discovery: RiskRules = Field(default_factory=RiskRules)
    replay: RiskRules = Field(default_factory=lambda: RiskRules(irreversible=RiskRule.REQUIRE_APPROVED_ARTIFACT))
    redaction: RedactionSpec = Field(default_factory=RedactionSpec)


class PolicyDocument(Strict):
    api_version: Literal["cua.dev/v1"] = Field(default=API_VERSION, alias="apiVersion")
    kind: Literal["Policy"] = "Policy"
    spec: PolicySpec


@dataclass(frozen=True)
class Decision:
    allowed: bool
    rule: RiskRule | None
    reason: str

    @property
    def needs_human(self) -> bool:
        return self.rule is RiskRule.REQUIRE_HUMAN_APPROVAL


class Policy:
    """A :class:`PolicySpec` bound to one tenant app origin."""

    def __init__(self, spec: PolicySpec, tenant_origin: str) -> None:
        self.spec = spec
        self.origins = {_origin(o.replace("{tenant}", tenant_origin)) for o in spec.allowed_origins}
        self._irreversible = _keyword_regex(spec.irreversible_keywords)
        self._write = _keyword_regex(spec.write_keywords)

    # ------------------------------------------------------------------ locations

    def check_url(self, url: str) -> Decision:
        parts = urlsplit(url)
        if parts.scheme in ("data", "about", "blob"):
            return Decision(True, None, "inline resource")
        if _origin(url) not in self.origins:
            return Decision(False, None, f"origin {_origin(url)} is not allowlisted")
        path = parts.path or "/"
        if any(fnmatchcase(path, p) for p in self.spec.denied_paths):
            return Decision(False, None, f"path {path} is explicitly denied")
        if not any(fnmatchcase(path, p) for p in self.spec.allowed_paths):
            return Decision(False, None, f"path {path} is not allowlisted")
        return Decision(True, None, "allowlisted")

    # ------------------------------------------------------------------ actions

    def classify(self, action: ActionType, label: str | None, declared: Risk = Risk.READ) -> Risk:
        """Risk = max(declared, inferred). Only clicks/presses can commit, so only they are inferred."""
        inferred = Risk.READ
        if action in (ActionType.CLICK, ActionType.PRESS) and label:
            if self._irreversible and self._irreversible.search(label):
                inferred = Risk.IRREVERSIBLE
            elif self._write and self._write.search(label):
                inferred = Risk.WRITE
        return Risk.highest(declared, inferred)

    def decide(self, mode: Mode, action: ActionType, risk: Risk, *, artifact_approved: bool = False) -> Decision:
        if action not in self.spec.allowed_actions:
            return Decision(False, RiskRule.DENY, f"action '{action}' is not in the allowlist")
        rules = self.spec.discovery if mode is Mode.DISCOVERY else self.spec.replay
        rule = rules.for_risk(risk)
        match rule:
            case RiskRule.ALLOW:
                return Decision(True, rule, f"{risk} actions are allowed in {mode}")
            case RiskRule.DENY:
                return Decision(False, rule, f"{risk} actions are denied in {mode}")
            case RiskRule.REQUIRE_HUMAN_APPROVAL:
                return Decision(False, rule, f"{risk} action requires operator approval in {mode}")
            case RiskRule.REQUIRE_APPROVED_ARTIFACT:
                if artifact_approved:
                    return Decision(True, rule, f"{risk} action allowed: capability is approved for this digest")
                return Decision(False, rule, f"{risk} action requires an approved capability (status/digest)")


def _origin(url: str) -> str:
    parts = urlsplit(url)
    return f"{parts.scheme}://{parts.netloc}".lower()


def _keyword_regex(words: list[str]) -> re.Pattern[str] | None:
    if not words:
        return None
    return re.compile(r"\b(" + "|".join(re.escape(w) for w in words) + r")\b", re.IGNORECASE)
