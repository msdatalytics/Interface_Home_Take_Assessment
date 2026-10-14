"""How a step identifies the control or text it acts on.

A :class:`Target` carries an *ordered* list of independent locator strategies. Every
strategy stored in an artifact was verified at record time to resolve to exactly the
element the agent (or human) acted on. Replay tries them in order and uses the first
that resolves *uniquely*; falling past the primary strategy is reported as a drift
signal rather than silently accepted.

Strategies are expressed in surface-neutral, human-perceivable terms (role, visible
label, table coordinates, text). Only ``css`` is DOM-specific, and it is always ranked
last because it is the least portable across tenants and versions. A desktop surface
would map ``role``/``label`` onto UI Automation / AX properties and ignore ``css``.
"""

from __future__ import annotations

from typing import Annotated, ClassVar, Literal

from pydantic import Field, model_validator

from cua.artifact.common import Strict


class FrameRef(Strict):
    """Identifies a frame/window region. ``None`` on a :class:`Target` means "search all frames"."""

    name: str | None = Field(default=None, description="Frame name attribute (e.g. 'main').")
    url_path: str | None = Field(default=None, description="fnmatch-style glob on the frame URL path.")

    @model_validator(mode="after")
    def _one_of(self) -> FrameRef:
        if not (self.name or self.url_path):
            raise ValueError("FrameRef needs 'name' or 'url_path'")
        return self

    def describe(self) -> str:
        return f"frame[name={self.name}]" if self.name else f"frame[path~{self.url_path}]"


class _Locator(Strict):
    STABILITY: ClassVar[int]
    """Higher = more robust to layout change and tenant variation."""

    def describe(self) -> str:  # pragma: no cover - overridden
        raise NotImplementedError


class RoleLocator(_Locator):
    """Accessibility role + accessible name. Survives layout/CSS changes; maps 1:1 to desktop
    accessibility APIs. Unavailable when the control has no accessible name (common in legacy)."""

    STABILITY: ClassVar[int] = 100
    strategy: Literal["role"] = "role"
    role: str
    name: str

    def describe(self) -> str:
        return f'{self.role} "{self.name}"'


class LabelLocator(_Locator):
    """A form control identified by the visible label a human reads next to it - whether or not
    the label is programmatically associated (``<label for>``, ``aria-labelledby``) or merely sits
    in the adjacent table cell, which is the norm in legacy server-rendered apps."""

    STABILITY: ClassVar[int] = 90
    strategy: Literal["label"] = "label"
    label: str
    role: str = Field(description="Control role: textbox | combobox | checkbox | radio | button")

    def describe(self) -> str:
        return f'{self.role} labelled "{self.label}"'


class TableCellLocator(_Locator):
    """A cell addressed by (row key text, column header text) - the way an operator reads a grid.
    Robust to row reordering and added columns."""

    STABILITY: ClassVar[int] = 85
    strategy: Literal["table_cell"] = "table_cell"
    row: str = Field(description="Text of any cell in the target row (the row key).")
    column: str = Field(description="Header text of the target column.")

    def describe(self) -> str:
        return f'cell[row="{self.row}", column="{self.column}"]'


class LabelValueLocator(_Locator):
    """The value displayed next to a label in a key/value layout (``Confirmation Number: CB-1``)."""

    STABILITY: ClassVar[int] = 80
    strategy: Literal["label_value"] = "label_value"
    label: str

    def describe(self) -> str:
        return f'value of "{self.label}"'


class TextLocator(_Locator):
    """Exact visible text, optionally constrained to a role (e.g. a link)."""

    STABILITY: ClassVar[int] = 60
    strategy: Literal["text"] = "text"
    text: str
    role: str | None = None

    def describe(self) -> str:
        return f'{self.role or "element"} with text "{self.text}"'


class CssLocator(_Locator):
    """Structural selector. Last resort: brittle across versions and tenant-specific (generated
    control names differ per installation). Kept so a recording is never left without a fallback."""

    STABILITY: ClassVar[int] = 10
    strategy: Literal["css"] = "css"
    selector: str

    def describe(self) -> str:
        return f"css `{self.selector}`"


Locator = Annotated[
    RoleLocator | LabelLocator | TableCellLocator | LabelValueLocator | TextLocator | CssLocator,
    Field(discriminator="strategy"),
]

TEXTUAL_FIELDS: dict[str, tuple[str, ...]] = {
    "role": ("name",),
    "label": ("label",),
    "table_cell": ("row", "column"),
    "label_value": ("label",),
    "text": ("text",),
    "css": (),
}
"""Locator fields holding UI vocabulary; used for tenant vocabulary translation."""


class Target(Strict):
    description: str = Field(description="What a human would call this control.")
    frame: FrameRef | None = None
    locators: list[Locator] = Field(min_length=1)

    def describe(self) -> str:
        where = f" in {self.frame.describe()}" if self.frame else ""
        return f"{self.description}{where}"

    @property
    def best_stability(self) -> int:
        return max(loc.STABILITY for loc in self.locators)
