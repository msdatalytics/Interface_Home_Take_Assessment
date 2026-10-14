"""Ordered, typed actions that make up a capability (and app-profile sub-flows such as login)."""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import Field

from cua.artifact.common import Risk, Strict
from cua.artifact.conditions import Condition
from cua.artifact.targets import FrameRef, Target
from cua.artifact.values import ValueExpr

StepId = Annotated[str, Field(pattern=r"^[a-z][a-z0-9_]{0,63}$")]


class _Step(Strict):
    id: StepId
    title: str = Field(description="Intent of the step in operator language.")
    risk: Risk = Risk.READ
    expect: Condition | None = Field(
        default=None,
        description="Post-condition checkpoint: the state this step must lead to.",
    )
    timeout_ms: int = Field(default=15_000, ge=500, le=120_000)
    source: Literal["llm", "human", "author"] = Field(
        default="llm", description="Who produced this step during recording."
    )
    rationale: str | None = Field(default=None, description="Why the step exists (from the discovery run).")


class NavigateStep(_Step):
    action: Literal["navigate"] = "navigate"
    path: str = Field(description="Path relative to the tenant's base URL.")
    frame: FrameRef | None = None


class ClickStep(_Step):
    action: Literal["click"] = "click"
    target: Target


class FillStep(_Step):
    action: Literal["fill"] = "fill"
    target: Target
    value: ValueExpr


class SelectStep(_Step):
    action: Literal["select"] = "select"
    target: Target
    option: ValueExpr = Field(description="Visible option label to choose.")


class PressStep(_Step):
    action: Literal["press"] = "press"
    target: Target
    key: str = Field(pattern=r"^(Enter|Tab|Escape|Space|ArrowDown|ArrowUp)$")


class ExtractStep(_Step):
    action: Literal["extract"] = "extract"
    target: Target
    output: str = Field(description="Declared output this step populates.")


Step = Annotated[
    NavigateStep | ClickStep | FillStep | SelectStep | PressStep | ExtractStep,
    Field(discriminator="action"),
]

TargetedStep = ClickStep | FillStep | SelectStep | PressStep | ExtractStep
