"""Playwright (Chromium) implementation of :class:`~cua.surface.base.Surface`.

Perception is semantic rather than pixel-based: an in-page observer produces an
accessibility-style list of controls and text across *all* frames (framesets included),
and actions are performed through locators the observer's elements were verified against.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import re
import time
from collections.abc import Callable
from fnmatch import fnmatchcase
from functools import cache
from importlib import resources
from pathlib import Path
from typing import Any, Literal
from urllib.parse import quote, urljoin, urlsplit

from playwright.async_api import (
    Browser,
    BrowserContext,
    Frame,
    Page,
    Playwright,
    Request,
    Route,
    async_playwright,
)
from playwright.async_api import Error as PlaywrightError
from playwright.async_api import Locator as PWLocator

from cua.artifact.conditions import AllOf, AnyOf, Condition, ElementVisible, TextVisible, UrlMatches
from cua.artifact.targets import (
    CssLocator,
    FrameRef,
    LabelLocator,
    LabelValueLocator,
    Locator,
    RoleLocator,
    TableCellLocator,
    Target,
    TextLocator,
)
from cua.artifact.vocabulary import Vocabulary
from cua.safety.policy import Policy
from cua.safety.redaction import DEFAULT_PATTERNS, Redactor
from cua.surface.base import (
    ActionError,
    ConditionResult,
    FrameState,
    FrameView,
    LocatorAttempt,
    Observation,
    ObservedElement,
    Resolution,
    ResolutionMiss,
    Surface,
    SurfaceState,
)
from cua.textutil import collapse_ws

EventSink = Callable[[str, dict[str, Any]], None]
HumanEventListener = Callable[[dict[str, Any]], None]

FORM_ROLES = {"textbox", "searchbox", "combobox", "listbox", "checkbox", "radio", "spinbutton"}
ARIA_ROLES = FORM_ROLES | {"button", "link", "menuitem", "tab", "option", "switch", "slider"}
TRACKED_RESOURCES = {"document", "xhr", "fetch"}


@cache
def _js(name: str) -> str:
    return resources.files("cua.surface").joinpath(f"js/{name}").read_text(encoding="utf-8")


def _with_common(name: str) -> str:
    return _js(name).replace("__COMMON__;", _js("common.js"))


def _engine_script(engine: str) -> str:
    return _with_common("engines.js").replace("__ENGINE__", engine)


def _body(payload: dict[str, str]) -> str:
    return quote(json.dumps(payload, separators=(",", ":")), safe="")


def _path(url: str) -> str:
    return urlsplit(url).path or "/"


def _explain(message: str) -> str:
    """First line of a Playwright error, plus the actionable cause buried in its call log."""
    first = _first_line(message)
    if "intercepts pointer events" in message:
        return f"{first} - the target is covered by another element (e.g. an unexpected modal dialog)"
    if "element is not enabled" in message:
        return f"{first} - the target is disabled"
    if "element is not visible" in message:
        return f"{first} - the target is not visible"
    return first


def _first_line(message: str, limit: int = 300) -> str:
    line = next((ln.strip() for ln in message.splitlines() if ln.strip()), message)
    return line[:limit]


class BrowserSurface(Surface):
    def __init__(
        self,
        page: Page,
        *,
        base_url: str,
        redactor: Redactor,
        vocabulary: Vocabulary,
        sensitive_labels: list[str],
        events: EventSink,
        action_timeout_ms: int = 5_000,
    ) -> None:
        self.page = page
        self.base_url = base_url.rstrip("/") + "/"
        self.redactor = redactor
        self.vocabulary = vocabulary
        self.sensitive_label_patterns = [re.compile(p, re.IGNORECASE) for p in sensitive_labels]
        self._sensitive_label_sources = sensitive_labels
        self._events = events
        self.action_timeout_ms = action_timeout_ms
        self.human_event_listener: HumanEventListener | None = None
        self._ref_frames: dict[str, Frame] = {}
        self._last_observation: Observation | None = None
        self._inflight: set[Request] = set()
        self._background: set[asyncio.Future[Any]] = set()
        self._last_activity = time.monotonic()
        page.on("request", self._on_request)
        page.on("requestfinished", self._on_request_done)
        page.on("requestfailed", self._on_request_done)
        page.on("framenavigated", lambda _f: self._touch())
        page.on("dialog", self._on_dialog)

    # ------------------------------------------------------------------ bookkeeping

    def _touch(self) -> None:
        self._last_activity = time.monotonic()

    def _on_request(self, request: Request) -> None:
        if request.resource_type in TRACKED_RESOURCES:
            self._inflight.add(request)
        self._touch()

    def _on_request_done(self, request: Request) -> None:
        self._inflight.discard(request)
        self._touch()

    def _on_dialog(self, dialog: Any) -> None:
        kind = dialog.type
        self._events("surface.dialog", {"dialog_type": kind, "message": self.redactor.text(dialog.message)})
        # Alerts carry information only; anything asking for a decision is declined conservatively.
        coro = dialog.accept() if kind == "alert" else dialog.dismiss()
        task = asyncio.ensure_future(coro)
        self._background.add(task)
        task.add_done_callback(self._background.discard)

    def on_human_event(self, payload: dict[str, Any]) -> None:
        if self.human_event_listener is not None:
            self.human_event_listener(payload)

    @property
    def location(self) -> str:
        frames = [f"{f.name or 'top'}:{_path(f.url)}" for f in self.page.frames if f.url != "about:blank"]
        return ", ".join(frames)

    # ------------------------------------------------------------------ perception

    async def observe(self) -> Observation:
        await self.wait_settled(timeout_ms=5_000)
        frames: list[FrameView] = []
        elements: dict[str, ObservedElement] = {}
        self._ref_frames = {}
        counter = 0
        for index, frame in enumerate(self.page.frames):
            raw = await self._observe_frame(frame, index)
            if raw is None:
                continue
            view_elements = []
            for item in raw["items"]:
                counter += 1
                ref = f"e{counter}"
                element = ObservedElement(
                    ref=ref,
                    frame_index=index,
                    frame_name=frame.name or None,
                    frame_path=_path(frame.url),
                    local_ref=item["ref"],
                    kind=item["kind"],
                    role=item.get("role"),
                    tag=item.get("tag"),
                    name=item.get("name") or None,
                    labels=tuple(item.get("labels") or ()),
                    value=item.get("value"),
                    options=tuple(item.get("options") or ()),
                    disabled=bool(item.get("disabled")),
                    checked=item.get("checked"),
                    text=item.get("text"),
                    row=(index * 100_000 + item["row"]) if item.get("row") is not None else None,
                    box=tuple(item["box"]) if item.get("box") else None,
                    value_label=item.get("valueLabel"),
                    column=item.get("column"),
                    sensitive=bool(item.get("sensitive")),
                )
                elements[ref] = element
                view_elements.append(element)
                self._ref_frames[ref] = frame
            heading = raw.get("heading") or {}
            frames.append(
                FrameView(
                    index=index,
                    name=frame.name or None,
                    url=frame.url,
                    path=_path(frame.url),
                    title=raw.get("title") or "",
                    heading=heading.get("text"),
                    elements=tuple(view_elements),
                    truncated=bool(raw.get("truncated")),
                )
            )
        self._last_observation = Observation(frames=tuple(frames), elements=elements)
        return self._last_observation

    async def _observe_frame(self, frame: Frame, index: int) -> dict[str, Any] | None:
        if frame.is_detached() or frame.url in ("", "about:blank"):
            return None
        for _ in range(3):
            try:
                result: dict[str, Any] = await frame.evaluate(
                    _with_common("observer.js"), {"maxItems": 400, "prefix": f"{index}-"}
                )
                return result
            except PlaywrightError:  # navigating / context destroyed - retry shortly
                await asyncio.sleep(0.2)
        return None

    async def state(self) -> SurfaceState:
        frames: list[FrameState] = []
        for frame in self.page.frames:
            if frame.is_detached():
                continue
            try:
                text = await frame.evaluate("() => document.body ? document.body.innerText : ''")
            except PlaywrightError:
                text = ""
            frames.append(FrameState(name=frame.name or None, path=_path(frame.url), text=collapse_ws(text or "")))
        return SurfaceState(frames=tuple(frames))

    def _frames_for(self, ref: FrameRef | None) -> list[Frame]:
        frames = [f for f in self.page.frames if not f.is_detached()]
        if ref is None:
            return frames
        if ref.name:
            return [f for f in frames if f.name == ref.name]
        assert ref.url_path is not None
        return [f for f in frames if fnmatchcase(_path(f.url), ref.url_path)]

    @staticmethod
    def _state_frames(state: SurfaceState, ref: FrameRef | None) -> list[FrameState]:
        if ref is None:
            return list(state.frames)
        if ref.name:
            return [f for f in state.frames if f.name == ref.name]
        assert ref.url_path is not None
        return [f for f in state.frames if fnmatchcase(f.path, ref.url_path)]

    async def evaluate(self, condition: Condition, state: SurfaceState) -> ConditionResult:
        match condition:
            case TextVisible(text=text, pattern=pattern, frame=frame):
                for fs in self._state_frames(state, frame):
                    if text is not None:
                        wanted = collapse_ws(self.vocabulary.translate(text)).casefold()
                        if wanted and wanted in fs.text.casefold():
                            return ConditionResult(True, None)
                    elif pattern is not None:
                        m = re.search(pattern, fs.text)
                        if m:
                            message = m.groupdict().get("message") or m.group(0)
                            return ConditionResult(True, collapse_ws(message))
                return ConditionResult(False)
            case UrlMatches(path=glob, frame=frame):
                matched = any(fnmatchcase(fs.path, glob) for fs in self._state_frames(state, frame))
                return ConditionResult(matched)
            case ElementVisible(target=target):
                res = await self.try_resolve(target)
                if isinstance(res, Resolution):
                    try:
                        return ConditionResult(await res.handle.is_visible())
                    except PlaywrightError:
                        return ConditionResult(False)
                return ConditionResult(False)
            case AllOf(conditions=conditions):
                message = None
                for c in conditions:
                    r = await self.evaluate(c, state)
                    if not r.matched:
                        return ConditionResult(False)
                    message = message or r.message
                return ConditionResult(True, message)
            case AnyOf(conditions=conditions):
                for c in conditions:
                    r = await self.evaluate(c, state)
                    if r.matched:
                        return r
                return ConditionResult(False)
        raise TypeError(f"unsupported condition {condition!r}")  # pragma: no cover

    # ------------------------------------------------------------------ resolution

    def _compile(self, frame: Frame, locator: Locator) -> PWLocator:
        t = self.vocabulary.translate
        match locator:
            case RoleLocator(role=role, name=name):
                pw = frame.get_by_role(role, name=t(name), exact=True)  # type: ignore[arg-type]
            case LabelLocator(label=label, role=role):
                pw = frame.locator("cua-label=" + _body({"label": t(label), "role": role}))
            case TableCellLocator(row=row, column=column):
                pw = frame.locator("cua-cell=" + _body({"row": t(row), "column": t(column)}))
            case LabelValueLocator(label=label):
                pw = frame.locator("cua-value=" + _body({"label": t(label)}))
            case TextLocator(text=text, role=role):
                pw = (
                    frame.get_by_role(role, name=t(text), exact=True)  # type: ignore[arg-type]
                    if role
                    else frame.get_by_text(t(text), exact=True)
                )
            case CssLocator(selector=selector):
                pw = frame.locator(selector)
            case _:  # pragma: no cover
                raise TypeError(locator)
        return pw.filter(visible=True)

    def _describe(self, locator: Locator) -> str:
        description = locator.describe()
        for value in (getattr(locator, f, None) for f in ("name", "label", "row", "column", "text")):
            if isinstance(value, str) and self.vocabulary.translate(value) != value:
                description += f" (tenant: {self.vocabulary.translate(value)!r})"
        return self.redactor.text(description)

    async def try_resolve(self, target: Target) -> Resolution | ResolutionMiss:
        frames = self._frames_for(target.frame)
        attempts: list[LocatorAttempt] = []
        for index, locator in enumerate(target.locators):
            total = 0
            hit: PWLocator | None = None
            error: str | None = None
            for frame in frames:
                try:
                    pw = self._compile(frame, locator)
                    count = await pw.count()
                except PlaywrightError as exc:
                    error = _first_line(str(exc), 160)
                    continue
                if count:
                    total += count
                    hit = pw if count == 1 else hit
            attempts.append(LocatorAttempt(locator.strategy, self._describe(locator), total, error))
            if total == 1 and hit is not None:
                return Resolution(hit, index, locator.strategy, tuple(attempts))
        if not frames:
            attempts.append(LocatorAttempt("frame", target.frame.describe() if target.frame else "any", 0, "no frame"))
        return ResolutionMiss(tuple(attempts))

    async def target_for(
        self, ref: str, *, purpose: Literal["act", "extract"], description: str | None = None
    ) -> Target:
        """Synthesize every locator strategy for an observed element and keep only those that
        resolve uniquely back to *that* element right now (verified, not guessed)."""
        element = self._require(ref)
        frame = self._ref_frames[ref]
        marker = f'[data-cua-ref="{element.local_ref}"]'
        info: dict[str, Any] = await frame.locator(marker).evaluate(
            "(el) => { " + _js("common.js") + " const c = el.closest('td,th,dd') || el;"
            " return { cell: __cua.cellCoordinates(c), valueLabel: __cua.valueLabel(c), css: __cua.cssPath(el) }; }"
        )
        frame_ref = FrameRef(name=frame.name) if frame.name else None
        candidates = self._candidates(element, info, purpose, self._is_sensitive_label)
        sensitive_texts = {element.text, element.name} if self._element_sensitive(element) else set()
        verified: list[Locator] = []
        for locator in candidates:
            if self._leaks(locator, sensitive_texts - {None}):
                continue
            res = await self.try_resolve(Target(description="probe", frame=frame_ref, locators=[locator]))
            if not isinstance(res, Resolution):
                continue
            try:
                same = await res.handle.evaluate(
                    "(el, ref) => el.getAttribute('data-cua-ref') === ref", element.local_ref
                )
            except PlaywrightError:
                same = False
            if same and locator not in verified:
                verified.append(locator)
        if not verified:
            raise ActionError(f"no stable locator could be verified for {ref}")
        verified.sort(key=lambda loc: -loc.STABILITY)
        return Target(description=description or self._default_description(element), frame=frame_ref, locators=verified)

    def _leaks(self, locator: Locator, sensitive_texts: set[str | None]) -> bool:
        """Never persist a locator that embeds sensitive data (e.g. a member number as row key,
        or the text of a cell under a sensitive column such as "Name")."""
        texts = [getattr(locator, f) for f in ("name", "label", "row", "column", "text") if getattr(locator, f, None)]
        return any(self.redactor.text(t) != t or t in sensitive_texts for t in texts)

    def _is_sensitive_label(self, label: str | None) -> bool:
        return bool(label) and any(p.search(label or "") for p in self.sensitive_label_patterns)

    def _element_sensitive(self, e: ObservedElement) -> bool:
        return e.sensitive or any(self._is_sensitive_label(x) for x in (*e.labels, e.value_label, e.column))

    def _default_description(self, e: ObservedElement) -> str:
        if e.kind == "control":
            return f"{e.role} {(e.name or e.label or '').rstrip(': ')}".strip()
        return "text" + (f" for {e.value_label}" if e.value_label else "")

    @staticmethod
    def _candidates(
        e: ObservedElement, info: dict[str, Any], purpose: str, sensitive_label: Callable[[str | None], bool]
    ) -> list[Locator]:
        out: list[Locator] = []
        if e.kind == "control":
            role = e.role or ""
            if e.name and role in ARIA_ROLES:
                out.append(RoleLocator(role=role, name=e.name))
            if role in FORM_ROLES:
                out.extend(LabelLocator(label=label, role=role) for label in e.labels[:2])
            if e.name and role in ("link", "clickable"):
                out.append(TextLocator(text=e.name))
        else:
            cell = info.get("cell") or {}
            if cell.get("column"):
                out.extend(
                    TableCellLocator(row=key["text"], column=cell["column"])
                    for key in cell.get("rowKeys", [])
                    if not sensitive_label(key.get("column"))
                )
            if info.get("valueLabel"):
                out.append(LabelValueLocator(label=info["valueLabel"]))
            if purpose == "act" and e.text:
                out.append(TextLocator(text=e.text))
        if info.get("css"):
            out.append(CssLocator(selector=info["css"]))
        return out

    def _require(self, ref: str) -> ObservedElement:
        obs = self._last_observation
        if obs is None or ref not in obs.elements:
            raise ActionError(f"unknown element ref '{ref}' - refs are only valid for the latest observation")
        return obs.elements[ref]

    async def resolve_ref(self, ref: str) -> Resolution:
        element = self._require(ref)
        frame = self._ref_frames[ref]
        handle = frame.locator(f'[data-cua-ref="{element.local_ref}"]')
        return Resolution(handle, 0, "ref", ())

    # ------------------------------------------------------------------ actions

    async def _guard(self, coro: Any, what: str) -> Any:
        try:
            return await coro
        except PlaywrightError as exc:
            raise ActionError(f"{what} failed: {self.redactor.text(_explain(str(exc)))}") from None
        finally:
            self._touch()  # start the settle quiet-window after the action, not before it

    async def navigate(self, path: str, frame: FrameRef | None = None) -> None:
        url = urljoin(self.base_url, path.lstrip("/"))
        if frame is None:
            await self._guard(self.page.goto(url, wait_until="load"), f"navigate {path}")
            return
        frames = self._frames_for(frame)
        if len(frames) != 1:
            raise ActionError(f"navigate: expected one {frame.describe()}, found {len(frames)}")
        await self._guard(frames[0].goto(url, wait_until="load"), f"navigate {path}")

    async def reload(self, frame: FrameRef | None) -> None:
        if frame is None:
            await self._guard(self.page.reload(wait_until="load"), "reload")
            return
        for f in self._frames_for(frame):
            await self._guard(f.goto(f.url, wait_until="load"), f"reload {frame.describe()}")

    async def click(self, resolution: Resolution) -> None:
        await self._guard(resolution.handle.click(timeout=self.action_timeout_ms), "click")

    async def fill(self, resolution: Resolution, value: str, *, secret: bool = False) -> None:
        handle: PWLocator = resolution.handle
        await self._guard(handle.fill(value, timeout=self.action_timeout_ms), "fill")
        actual = await self._guard(handle.input_value(), "read back")
        if actual != value:
            shown = "<secret>" if secret else self.redactor.text(actual)
            raise ActionError(f"fill did not stick (field shows {shown!r})")

    async def select(self, resolution: Resolution, option: str) -> str:
        handle: PWLocator = resolution.handle
        options: list[str] = await self._guard(
            handle.evaluate("(el) => Array.from(el.options || []).map((o) => o.text.trim())"), "list options"
        )
        wanted = collapse_ws(self.vocabulary.translate(option)).casefold()
        match = next((o for o in options if collapse_ws(o).casefold() == wanted), None)
        match = match or next((o for o in options if collapse_ws(o).casefold().startswith(wanted)), None)
        if match is None:
            raise ActionError(f"option {option!r} not available (have {[self.redactor.text(o) for o in options]})")
        await self._guard(handle.select_option(label=match, timeout=self.action_timeout_ms), "select")
        return match

    async def press(self, resolution: Resolution, key: str) -> None:
        await self._guard(resolution.handle.press(key, timeout=self.action_timeout_ms), "press")

    async def read_text(self, resolution: Resolution) -> str:
        text: str = await self._guard(resolution.handle.inner_text(timeout=self.action_timeout_ms), "read")
        return collapse_ws(text)

    async def wait_settled(self, timeout_ms: int = 10_000, quiet_ms: int = 300) -> bool:
        """Wait until no document/XHR request is in flight for ``quiet_ms`` and every frame has
        finished loading. Returns ``False`` on timeout (callers then rely on condition polling)."""
        deadline = time.monotonic() + timeout_ms / 1000
        while time.monotonic() < deadline:
            idle = not self._inflight and (time.monotonic() - self._last_activity) * 1000 >= quiet_ms
            if idle and await self._frames_loaded():
                return True
            await asyncio.sleep(0.05)
        return False

    async def _frames_loaded(self) -> bool:
        for frame in self.page.frames:
            if frame.is_detached():
                continue
            try:
                if await frame.evaluate("() => document.readyState") != "complete":
                    return False
            except PlaywrightError:
                return False
        return True

    # ------------------------------------------------------------------ operator input (handoff)

    async def operator_click(self, x: float, y: float) -> None:
        await self.page.mouse.click(x, y)

    async def operator_type(self, text: str) -> None:
        await self.page.keyboard.type(text, delay=15)

    async def operator_key(self, key: str) -> None:
        await self.page.keyboard.press(key)

    async def operator_click_ref(self, ref: str) -> None:
        await self.click(await self.resolve_ref(ref))

    async def live_screenshot(self) -> bytes:
        """Unmasked view for the authorised operator who holds control. Never persisted."""
        return await self.page.screenshot(type="png")

    # ------------------------------------------------------------------ evidence

    async def _mask(self, unmask: bool = False) -> None:
        opts = {
            "unmask": unmask,
            "known": self.redactor.known_values,
            "patterns": [DEFAULT_PATTERNS[k] for k in ("ssn", "email")],
            "labels": self._sensitive_label_sources,
        }
        for frame in self.page.frames:
            with contextlib.suppress(PlaywrightError):
                await frame.evaluate(_with_common("mask.js"), opts)

    async def screenshot(self, path: Path, *, mask_refs: set[str] | None = None) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        await self._mask()
        try:
            await self.page.screenshot(path=str(path), type="png")
        finally:
            await self._mask(unmask=True)
        return path

    async def dom_snapshot(self, directory: Path, redactor: Redactor) -> list[Path]:
        """Per-frame HTML with the same masking as screenshots (label-based + known values),
        applied to a detached clone so the live page is never altered, then pattern-redacted."""
        directory.mkdir(parents=True, exist_ok=True)
        written: list[Path] = []
        await self._mask()
        try:
            for index, frame in enumerate(self.page.frames):
                try:
                    html = await frame.evaluate(_MASKED_HTML_JS)
                except PlaywrightError:
                    continue
                name = re.sub(r"[^a-z0-9_-]", "_", (frame.name or "top").lower())
                out = directory / f"frame-{index}-{name}.html"
                out.write_text(f"<!-- {redactor.text(frame.url)} -->\n" + redactor.text(html), encoding="utf-8")
                written.append(out)
        finally:
            await self._mask(unmask=True)
        return written


_MASKED_HTML_JS = """() => {
  const clone = document.documentElement.cloneNode(true);
  clone.querySelectorAll("[data-cua-mask]").forEach((el) => {
    if (el.matches("input,textarea")) el.setAttribute("value", "[REDACTED]");
    else el.textContent = "[REDACTED]";
    el.style.filter = el.dataset.cuaMask === "none" ? "" : el.dataset.cuaMask;
    el.removeAttribute("data-cua-mask");
  });
  clone.querySelectorAll("[data-cua-ref]").forEach((el) => {
    el.removeAttribute("data-cua-ref");
    el.removeAttribute("data-cua-kind");
  });
  return "<!DOCTYPE html>\\n" + clone.outerHTML;
}"""


class BrowserSession:
    """Owns the Playwright lifecycle for one live session (one browser context + page)."""

    def __init__(self) -> None:
        self._pw: Playwright | None = None
        self._browser: Browser | None = None
        self.context: BrowserContext | None = None
        self.surface: BrowserSurface | None = None
        self._trace_path: Path | None = None

    @classmethod
    async def start(
        cls,
        *,
        base_url: str,
        policy: Policy,
        redactor: Redactor,
        vocabulary: Vocabulary,
        events: EventSink,
        headless: bool = True,
        trace_path: Path | None = None,
        slow_mo_ms: int = 0,
    ) -> BrowserSession:
        session = cls()
        session._pw = await async_playwright().start()
        for engine in ("cua-label", "cua-cell", "cua-value"):
            await session._pw.selectors.register(engine, script=_engine_script(engine))
        session._browser = await session._pw.chromium.launch(headless=headless, slow_mo=slow_mo_ms)
        context = await session._browser.new_context(viewport={"width": 1280, "height": 800}, base_url=base_url)
        session.context = context

        async def enforce(route: Route, request: Request) -> None:
            decision = policy.check_url(request.url)
            if decision.allowed:
                await route.continue_()
                return
            events(
                "policy.blocked_request",
                {"url": redactor.text(request.url), "reason": decision.reason, "resource": request.resource_type},
            )
            await route.abort("blockedbyclient")

        await context.route("**/*", enforce)
        await context.add_init_script(script=_with_common("human_capture.js"))
        page = await context.new_page()
        surface = BrowserSurface(
            page,
            base_url=base_url,
            redactor=redactor,
            vocabulary=vocabulary,
            sensitive_labels=policy.spec.redaction.sensitive_labels,
            events=events,
        )
        await context.expose_binding("__cuaHumanEvent", lambda _source, payload: surface.on_human_event(payload))
        if trace_path is not None:
            # Traces embed raw DOM snapshots (unredacted). Opt-in, synthetic data only.
            await context.tracing.start(screenshots=True, snapshots=True)
            session._trace_path = trace_path
        session.surface = surface
        return session

    async def close(self) -> None:
        with contextlib.suppress(Exception):
            if self.context is not None and self._trace_path is not None:
                await self.context.tracing.stop(path=str(self._trace_path))
        with contextlib.suppress(Exception):
            if self._browser is not None:
                await self._browser.close()
        with contextlib.suppress(Exception):
            if self._pw is not None:
                await self._pw.stop()
