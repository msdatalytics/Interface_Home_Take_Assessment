"""Checkpoint / detector conditions evaluated against the live surface."""

from __future__ import annotations

import re
from typing import Annotated, Literal

from pydantic import Field, field_validator, model_validator

from cua.artifact.common import Strict
from cua.artifact.targets import FrameRef, Target


class TextVisible(Strict):
    """Visible text (exact substring, whitespace-normalised, case-insensitive) or a regex.

    A regex may define a named group ``message`` which is surfaced as the human-readable
    message of a business outcome (e.g. the validation error text)."""

    kind: Literal["text_visible"] = "text_visible"
    text: str | None = None
    pattern: str | None = None
    frame: FrameRef | None = None

    @model_validator(mode="after")
    def _one_of(self) -> TextVisible:
        if (self.text is None) == (self.pattern is None):
            raise ValueError("text_visible needs exactly one of 'text' or 'pattern'")
        return self

    @field_validator("pattern")
    @classmethod
    def _compiles(cls, value: str | None) -> str | None:
        if value is not None:
            re.compile(value)
        return value

    def describe(self) -> str:
        where = f" in {self.frame.describe()}" if self.frame else ""
        what = f'text "{self.text}"' if self.text is not None else f"text /{self.pattern}/"
        return f"{what} visible{where}"


class ElementVisible(Strict):
    kind: Literal["element_visible"] = "element_visible"
    target: Target

    def describe(self) -> str:
        return f"{self.target.describe()} visible"


class UrlMatches(Strict):
    """Frame URL path (query ignored - legacy apps put volatile session tokens there) matches a glob."""

    kind: Literal["url_matches"] = "url_matches"
    path: str
    frame: FrameRef | None = None

    def describe(self) -> str:
        where = f" of {self.frame.describe()}" if self.frame else ""
        return f"url path{where} matches {self.path}"


class AllOf(Strict):
    kind: Literal["all_of"] = "all_of"
    conditions: list[Condition] = Field(min_length=1)

    def describe(self) -> str:
        return " AND ".join(c.describe() for c in self.conditions)


class AnyOf(Strict):
    kind: Literal["any_of"] = "any_of"
    conditions: list[Condition] = Field(min_length=1)

    def describe(self) -> str:
        return " OR ".join(c.describe() for c in self.conditions)


Condition = Annotated[
    TextVisible | ElementVisible | UrlMatches | AllOf | AnyOf,
    Field(discriminator="kind"),
]

AllOf.model_rebuild()
AnyOf.model_rebuild()
