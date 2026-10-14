"""Turns what happened during discovery into a Capability artifact.

What is recorded is *intent + verified targeting*, never the transcript: each step keeps
the agent's one-line rationale, the locators that were proven to resolve uniquely to the
acted-on element, parameter/secret references instead of values, and a post-condition
derived from how the screen actually changed.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Any

from cua.artifact.capability import Capability, CapabilityMetadata, CapabilitySpec, EntrySpec, Provenance
from cua.artifact.common import Risk
from cua.artifact.conditions import AllOf, Condition, TextVisible, UrlMatches
from cua.artifact.io import to_yaml
from cua.artifact.profile import AppProfile
from cua.artifact.steps import ClickStep, ExtractStep, FillStep, PressStep, SelectStep, Step
from cua.artifact.targets import FrameRef, LabelLocator, RoleLocator, Target, TextLocator
from cua.artifact.values import LiteralValue, ParamValue, ValueExpr
from cua.discovery.request import DiscoveryRequest
from cua.safety.redaction import Redactor
from cua.surface.base import Observation


class ArtifactLeakError(RuntimeError):
    """The compiled artifact would contain sensitive data. Refuse to write it."""


def _slug(text: str, limit: int = 40) -> str:
    slug = re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")
    return (slug or "step")[:limit].rstrip("_")


class FlowRecorder:
    def __init__(self, request: DiscoveryRequest, redactor: Redactor) -> None:
        self.request = request
        self.redactor = redactor
        self.steps: list[Step] = []

    def _id(self, action: str, description: str) -> str:
        base = _slug(f"{action}_{description}")
        if not re.match(r"^[a-z]", base):
            base = f"s_{base}"
        candidate, n = base, 2
        existing = {s.id for s in self.steps}
        while candidate in existing:
            candidate, n = f"{base}_{n}", n + 1
        return candidate

    def add(
        self,
        action: str,
        *,
        target: Target,
        title: str,
        rationale: str | None,
        risk: Risk,
        expect: Condition | None,
        source: str = "llm",
        value: ValueExpr | None = None,
        key: str | None = None,
        output: str | None = None,
    ) -> Step:
        last = self.steps[-1] if self.steps else None
        if (
            action in ("fill", "select")
            and last is not None
            and last.action == action
            and getattr(last, "target", None) == target
        ):
            self.steps.pop()  # the field was set again (e.g. after a dead end): keep only the final value
        common: dict[str, Any] = {
            "id": self._id(action, output or target.description),
            "title": title,
            "risk": risk,
            "expect": expect,
            "source": source,
            "rationale": self.redactor.text(rationale) if rationale else None,
            "target": target,
        }
        step: Step
        match action:
            case "click":
                step = ClickStep(**common)
            case "fill":
                assert value is not None
                step = FillStep(**common, value=value)
            case "select":
                assert value is not None
                step = SelectStep(**common, option=value)
            case "press":
                step = PressStep(**common, key=key or "Enter")
            case "extract":
                assert output is not None
                step = ExtractStep(**common, output=output)
            case _:
                raise ValueError(action)
        self.steps.append(step)
        return step

    def build(
        self,
        *,
        run_id: str,
        tenant: str,
        app_version: str | None,
        model: str,
        success: Condition,
        profile: AppProfile,
        human_assisted: bool,
    ) -> Capability:
        meta = self.request.metadata
        spec = self.request.spec
        capability = Capability(
            metadata=CapabilityMetadata(
                name=meta.name,
                version=meta.version,
                title=meta.title,
                description=meta.description,
                app=spec.app,
                app_versions=_compatible_range(app_version),
                tags=meta.tags,
                provenance=Provenance(
                    method="llm_discovery",
                    run_id=run_id,
                    recorded_at=datetime.now(UTC).replace(microsecond=0),
                    recorded_on_tenant=tenant,
                    recorded_on_app_version=app_version,
                    model=model,
                    goal=self.redactor.text(spec.goal),
                    human_assisted=human_assisted,
                ),
            ),
            spec=CapabilitySpec(
                inputs=spec.inputs,
                outputs=spec.outputs,
                outcomes=profile.spec.outcomes,
                entry=EntrySpec(),
                steps=self.steps,
                success=success,
            ),
        ).with_digest()
        assert_no_leak(capability, self.redactor)
        return capability


def assert_no_leak(capability: Capability, redactor: Redactor) -> None:
    """Final gate: the serialised artifact must be invariant under redaction."""
    text = to_yaml(capability)
    redacted = redactor.text(text)
    if redacted != text:
        diff = [b for a, b in zip(text.splitlines(), redacted.splitlines(), strict=False) if a != b][:3]
        raise ArtifactLeakError(f"artifact would persist sensitive data: {diff}")


def _compatible_range(version: str | None) -> str:
    if not version:
        return "*"
    major, minor, *_ = [*version.split("."), "0", "0"][:3]
    return f">={major}.{minor},<{int(major) + 1}"


def safe_heading(text: str | None, redactor: Redactor) -> str | None:
    if not text or len(text) > 60 or re.search(r"\d", text) or redactor.text(text) != text:
        return None
    return text


def derive_expectation(
    before: Observation,
    after: Observation,
    redactor: Redactor,
    sensitive_labels: list[re.Pattern[str]] | None = None,
) -> Condition | None:
    """Post-condition of an action, derived from what it changed:

    * for every named frame that navigated: its URL path, plus its heading when the heading is
      static text (no digits/sensitive data);
    * for a same-page postback (e.g. a search that renders results in place): the first static
      text that appeared, preferring table headers ("Member #") over data cells.
    """
    previous = {f.name: f for f in before.frames}
    conditions: list[Condition] = []
    for frame in after.frames:
        if not frame.name:
            continue
        old = previous.get(frame.name)
        if old is not None and old.path == frame.path and old.heading == frame.heading:
            continue
        ref = FrameRef(name=frame.name)
        conditions.append(UrlMatches(path=frame.path, frame=ref))
        heading = safe_heading(frame.heading, redactor)
        if heading:
            conditions.append(TextVisible(text=heading, frame=ref))
    if not conditions:
        appeared = _appeared_text(before, after, redactor, sensitive_labels or [])
        if appeared is not None:
            conditions.append(appeared)
    if not conditions:
        return None
    return conditions[0] if len(conditions) == 1 else AllOf(conditions=conditions)


def _appeared_text(
    before: Observation, after: Observation, redactor: Redactor, sensitive_labels: list[re.Pattern[str]]
) -> TextVisible | None:
    seen = {(e.frame_name, e.text) for e in before.elements.values() if e.kind == "text"}
    sensitive = after.sensitive_refs(redactor, sensitive_labels)
    fresh = [
        e
        for e in after.elements.values()
        if e.kind == "text"
        and e.frame_name
        and e.ref not in sensitive
        and (e.frame_name, e.text) not in seen
        and safe_heading(e.text, redactor)
    ]
    fresh.sort(key=lambda e: 0 if e.tag == "th" else 1)
    if not fresh:
        return None
    return TextVisible(text=fresh[0].text or "", frame=FrameRef(name=fresh[0].frame_name))


def steps_from_human_actions(actions: list[dict[str, Any]], recorder: FlowRecorder) -> list[Step]:
    """Best-effort conversion of captured operator interactions into (unverified) steps.

    Locators come from the in-page descriptor captured at event time; they are marked
    ``source: human`` so a reviewer knows they were not verified by the recorder."""
    produced: list[Step] = []
    for action in actions:
        role = action.get("role")
        name = action.get("name")
        labels = action.get("labels") or []
        frame = FrameRef(name=action["frame"]) if action.get("frame") else None
        locators: list[Any] = []
        if role and name and role not in ("clickable",):
            locators.append(RoleLocator(role=role, name=name))
        if role in ("textbox", "combobox") and labels:
            locators.append(LabelLocator(label=labels[0], role=role))
        if not locators and action.get("text"):
            locators.append(TextLocator(text=action["text"]))
        if not locators:
            continue
        description = name or (labels[0] if labels else action.get("text") or "element")
        target = Target(description=f"{role or 'element'} {description}".strip(), frame=frame, locators=locators)
        event = action.get("event")
        if event == "click" and role in ("button", "link", "clickable"):
            produced.append(
                recorder.add(
                    "click",
                    target=target,
                    title=f"Click {description}",
                    rationale="performed by operator during handoff",
                    risk=Risk.READ,
                    expect=None,
                    source="human",
                )
            )
        elif event == "change" and role in ("textbox", "combobox"):
            value = str(action.get("value") or "")
            match = re.fullmatch(r"\[param:(\w+)\]", value)
            if match:
                expr: ValueExpr = ParamValue(param=match.group(1))
            elif value and redactor_safe(value, recorder.redactor):
                expr = LiteralValue(literal=value)
            else:
                continue
            kind = "select" if role == "combobox" else "fill"
            produced.append(
                recorder.add(
                    kind,
                    target=target,
                    title=f"Set {description}",
                    rationale="performed by operator during handoff",
                    risk=Risk.READ,
                    expect=None,
                    source="human",
                    value=expr,
                )
            )
    return produced


def redactor_safe(value: str, redactor: Redactor) -> bool:
    return "[" not in value and redactor.text(value) == value


__all__ = [
    "ArtifactLeakError",
    "FlowRecorder",
    "assert_no_leak",
    "derive_expectation",
    "safe_heading",
    "steps_from_human_actions",
]
