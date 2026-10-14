"""The seam between *how we perceive/act on a surface* and *the recorded flow*.

Discovery, replay and the operator console only talk to :class:`Surface`. A browser is one
implementation (:mod:`cua.surface.browser`); a Windows desktop app would implement the same
contract over UI Automation (roles/names/labels map directly), a VDI/Citrix session over
screenshots + OCR. Artifacts never contain anything implementation-specific except the
lowest-ranked ``css`` fallback, which other surfaces simply skip.
"""

from __future__ import annotations

import hashlib
import re
from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from cua.artifact.conditions import Condition
from cua.artifact.targets import FrameRef, Target
from cua.safety.redaction import Redactor

# ---------------------------------------------------------------------------- observation


@dataclass(frozen=True)
class ObservedElement:
    ref: str
    frame_index: int
    frame_name: str | None
    frame_path: str
    local_ref: str
    kind: Literal["control", "text"]
    role: str | None = None
    tag: str | None = None
    name: str | None = None
    labels: tuple[str, ...] = ()
    value: str | None = None
    options: tuple[str, ...] = ()
    disabled: bool = False
    checked: bool | None = None
    text: str | None = None
    row: int | None = None
    box: tuple[int, int, int, int] | None = None
    value_label: str | None = None
    column: str | None = None
    sensitive: bool = False

    @property
    def label(self) -> str | None:
        return self.labels[0] if self.labels else None

    @property
    def display_name(self) -> str:
        return self.name or self.label or self.text or ""


@dataclass(frozen=True)
class FrameView:
    index: int
    name: str | None
    url: str
    path: str
    title: str
    heading: str | None
    elements: tuple[ObservedElement, ...]
    truncated: bool = False


@dataclass(frozen=True)
class Observation:
    frames: tuple[FrameView, ...]
    elements: dict[str, ObservedElement] = field(default_factory=dict)

    @property
    def fingerprint(self) -> str:
        """Structural hash used for stuck/no-progress detection."""
        h = hashlib.sha256()
        for f in self.frames:
            h.update(f"{f.name}|{f.path}|{f.heading}".encode())
            for e in f.elements:
                h.update(f"{e.kind}|{e.role}|{e.display_name}|{e.value}".encode())
        return h.hexdigest()[:16]

    def frame_named(self, name: str) -> FrameView | None:
        return next((f for f in self.frames if f.name == name), None)

    def render(self, redactor: Redactor, sensitive_labels: list[re.Pattern[str]]) -> str:
        """Compact text rendering for the LLM and the operator console. Redacted."""
        return render_observation(self, redactor, sensitive_labels or [])

    def sensitive_refs(self, redactor: Redactor, sensitive_labels: list[re.Pattern[str]]) -> set[str]:
        """Elements whose content must be masked in screenshots."""
        out: set[str] = set()
        for e in self.elements.values():
            content = e.text if e.kind == "text" else e.value
            if (
                e.sensitive
                or _label_is_sensitive(e, sensitive_labels)
                or (content and redactor.text(content) != content)
            ):
                out.add(e.ref)
        return out


def _label_is_sensitive(e: ObservedElement, patterns: list[re.Pattern[str]]) -> bool:
    labels = [*e.labels, *(x for x in (e.value_label, e.column) if x)]
    return any(p.search(label) for p in patterns for label in labels)


def _q(s: str) -> str:
    return '"' + s.replace('"', "'") + '"'


def render_observation(obs: Observation, redactor: Redactor, sensitive_labels: list[re.Pattern[str]]) -> str:
    lines: list[str] = []
    for frame in obs.frames:
        header = f"## frame {_q(frame.name) if frame.name else '(top)'} path={frame.path}"
        if frame.heading:
            header += f" heading={_q(redactor.text(frame.heading))}"
        lines.append(header)
        groups: list[tuple[int | None, list[str]]] = []
        for e in frame.elements:
            piece = _render_element(e, redactor, sensitive_labels)
            if groups and e.row is not None and groups[-1][0] == e.row:
                groups[-1][1].append(piece)
            else:
                groups.append((e.row, [piece]))
        lines.extend("  " + " | ".join(pieces) for _, pieces in groups)
        if frame.truncated:
            lines.append("  … (truncated)")
    return "\n".join(lines)


_FORM_ROLES = {"textbox", "searchbox", "combobox", "listbox", "checkbox", "radio", "spinbutton"}


def _render_element(e: ObservedElement, redactor: Redactor, sensitive_labels: list[re.Pattern[str]]) -> str:
    masked = e.sensitive or _label_is_sensitive(e, sensitive_labels)
    if e.kind == "text":
        text = "[REDACTED]" if masked else redactor.text(e.text or "")
        return f"[{e.ref}] {_q(text)}"
    parts = [f"[{e.ref}] {e.role}"]
    if e.name:
        parts.append(_q(redactor.text(e.name)))
    if e.labels and e.role in _FORM_ROLES and (not e.name or e.labels[0] != e.name):
        parts.append(f"label={_q(redactor.text(e.labels[0]))}")
    if e.value is not None and e.role not in ("button", "link"):
        parts.append(f"value={_q('[REDACTED]' if masked else redactor.text(e.value))}")
    if e.options:
        parts.append("options=[" + ", ".join(_q(redactor.text(o)) for o in e.options) + "]")
    if e.checked is not None:
        parts.append("checked" if e.checked else "unchecked")
    if e.disabled:
        parts.append("disabled")
    return " ".join(parts)


# ---------------------------------------------------------------------------- resolution


@dataclass(frozen=True)
class LocatorAttempt:
    strategy: str
    description: str
    matches: int
    error: str | None = None


@dataclass(frozen=True)
class Resolution:
    """A target resolved to exactly one live element."""

    handle: Any
    locator_index: int
    strategy: str
    attempts: tuple[LocatorAttempt, ...]

    @property
    def used_fallback(self) -> bool:
        return self.locator_index > 0


@dataclass(frozen=True)
class ResolutionMiss:
    attempts: tuple[LocatorAttempt, ...]

    @property
    def ambiguous(self) -> bool:
        return any(a.matches > 1 for a in self.attempts) and not any(a.matches == 1 for a in self.attempts)

    def describe(self) -> str:
        return "; ".join(
            f"{a.description} -> {a.matches} match(es)" + (f" ({a.error})" if a.error else "") for a in self.attempts
        )


@dataclass(frozen=True)
class ConditionResult:
    matched: bool
    message: str | None = None


@dataclass(frozen=True)
class FrameState:
    name: str | None
    path: str
    text: str


@dataclass(frozen=True)
class SurfaceState:
    """Cheap per-poll snapshot used to evaluate text/url conditions consistently."""

    frames: tuple[FrameState, ...]

    def summary(self) -> str:
        """Where each frame is. Deliberately excludes page text: results travel to callers and
        logs, and page text may hold PII that only label-aware masking (evidence files) catches."""
        return "; ".join(f"{f.name or 'top'}={f.path}" for f in self.frames)


class ActionError(RuntimeError):
    """The surface refused or failed to perform an action (e.g. element obscured)."""


class BlockedByPolicyError(RuntimeError):
    pass


# ---------------------------------------------------------------------------- contract


class Surface(ABC):
    """Perceive and act on a live application session."""

    human_event_listener: Callable[[dict[str, Any]], None] | None = None
    """Receives operator interactions captured on the live surface (set by the intervention desk)."""
    sensitive_label_patterns: list[re.Pattern[str]] = []
    """Field labels whose values are masked in observations and screenshots."""
    base_url: str = ""

    # perception
    @abstractmethod
    async def observe(self) -> Observation: ...

    @abstractmethod
    async def state(self) -> SurfaceState: ...

    @abstractmethod
    async def evaluate(self, condition: Condition, state: SurfaceState) -> ConditionResult: ...

    @abstractmethod
    async def try_resolve(self, target: Target) -> Resolution | ResolutionMiss: ...

    @abstractmethod
    async def target_for(
        self, ref: str, *, purpose: Literal["act", "extract"], description: str | None = None
    ) -> Target:
        """Turn an element from the latest observation into a durable, verified :class:`Target`."""

    @abstractmethod
    async def resolve_ref(self, ref: str) -> Resolution:
        """Resolve an element of the latest observation (discovery/operator use only)."""

    # action
    @abstractmethod
    async def navigate(self, path: str, frame: FrameRef | None = None) -> None: ...

    @abstractmethod
    async def reload(self, frame: FrameRef | None) -> None: ...

    @abstractmethod
    async def click(self, resolution: Resolution) -> None: ...

    @abstractmethod
    async def fill(self, resolution: Resolution, value: str, *, secret: bool = False) -> None: ...

    @abstractmethod
    async def select(self, resolution: Resolution, option: str) -> str: ...

    @abstractmethod
    async def press(self, resolution: Resolution, key: str) -> None: ...

    @abstractmethod
    async def read_text(self, resolution: Resolution) -> str: ...

    @abstractmethod
    async def wait_settled(self, timeout_ms: int = 10_000) -> bool: ...

    # operator input during a handoff (only invoked while a human holds the control lease)
    @abstractmethod
    async def operator_click(self, x: float, y: float) -> None: ...

    @abstractmethod
    async def operator_click_ref(self, ref: str) -> None: ...

    @abstractmethod
    async def operator_type(self, text: str) -> None: ...

    @abstractmethod
    async def operator_key(self, key: str) -> None: ...

    @abstractmethod
    async def live_screenshot(self) -> bytes:
        """Unmasked live view for the operator holding control. Never persisted."""

    # evidence
    @abstractmethod
    async def screenshot(self, path: Path, *, mask_refs: set[str] | None = None) -> Path:
        """Persistable screenshot with sensitive content masked."""

    @abstractmethod
    async def dom_snapshot(self, directory: Path, redactor: Redactor) -> list[Path]: ...

    @property
    @abstractmethod
    def location(self) -> str: ...
