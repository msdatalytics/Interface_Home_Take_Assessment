"""Executes a single artifact step deterministically against a surface.

Every wait is a *race* between the state we expect (target resolvable / checkpoint true)
and every known exceptional state (profile handlers + business outcomes). Whichever
appears first wins: an exceptional state raises :class:`StateInterrupt` immediately, the
expected state lets the step proceed, and only if neither appears before the deadline is it
a hard failure - with the per-locator attempts and the observed state attached.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Literal, TypeVar
from urllib.parse import urljoin

from cua.artifact.capability import Capability
from cua.artifact.common import Sensitivity
from cua.artifact.conditions import Condition
from cua.artifact.contract import parse_output
from cua.artifact.steps import (
    ClickStep,
    ExtractStep,
    FillStep,
    NavigateStep,
    PressStep,
    SelectStep,
    Step,
)
from cua.artifact.values import LiteralValue, ParamValue, SecretValue, ValueExpr
from cua.evidence.recorder import RunRecorder
from cua.runtime.monitor import Detection, StateMonitor
from cua.runtime.result import Failure, FailureCategory, FailureCode, LocatorAttemptRecord
from cua.safety.policy import Policy
from cua.safety.secrets import SecretUnavailableError, SecretVault
from cua.surface.base import ActionError, Resolution, ResolutionMiss, Surface, SurfaceState

T = TypeVar("T")
Phase = Literal["before", "after"]
POLL_INTERVAL_S = 0.25


class StateInterrupt(Exception):
    def __init__(self, detection: Detection, phase: Phase) -> None:
        super().__init__(detection.label)
        self.detection = detection
        self.phase = phase


class StepFailedError(Exception):
    def __init__(self, failure: Failure) -> None:
        super().__init__(failure.message)
        self.failure = failure


@dataclass
class StepContext:
    inputs: dict[str, str]
    vault: SecretVault
    capability: Capability | None = None
    outputs: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class StepExecution:
    strategy: str | None
    locator_index: int | None
    attempts: tuple[LocatorAttemptRecord, ...]
    skipped: bool = False

    @property
    def used_fallback(self) -> bool:
        return bool(self.locator_index)


class _Deadline(Exception):
    def __init__(self, state: SurfaceState | None) -> None:
        self.state = state


class StepExecutor:
    def __init__(
        self,
        surface: Surface,
        recorder: RunRecorder,
        policy: Policy,
        *,
        monitor: StateMonitor | None,
        guard: Callable[[], None] = lambda: None,
    ) -> None:
        self.surface = surface
        self.recorder = recorder
        self.policy = policy
        self.monitor = monitor
        self.guard = guard

    # ------------------------------------------------------------------ public API

    async def execute(self, step: Step, ctx: StepContext, *, phase: Phase = "before") -> StepExecution:
        execution = StepExecution(None, None, ())
        if phase == "before":
            execution = await self._act(step, ctx)
            if execution.skipped:
                return execution
            await self.surface.wait_settled(timeout_ms=step.timeout_ms)
        await self.await_expectation(step)
        return execution

    async def await_expectation(self, step: Step) -> None:
        expect = step.expect
        try:
            if expect is None:
                await self._poll(lambda _s: _true(), timeout_ms=1, phase="after")
            else:
                await self.await_condition(expect, timeout_ms=step.timeout_ms, phase="after")
        except _Deadline as d:
            raise StepFailedError(
                self._failure(
                    FailureCode.CHECKPOINT_FAILED,
                    FailureCategory.AUTOMATION,
                    f"step '{step.id}' did not reach its expected state",
                    step,
                    expected=expect.describe() if expect else None,
                    observed=self._observed(d.state),
                )
            ) from None

    async def await_condition(self, condition: Condition, *, timeout_ms: int, phase: Phase = "after") -> None:
        async def probe(state: SurfaceState) -> bool | None:
            return True if (await self.surface.evaluate(condition, state)).matched else None

        await self._poll(probe, timeout_ms=timeout_ms, phase=phase)

    # ------------------------------------------------------------------ actions

    async def _act(self, step: Step, ctx: StepContext) -> StepExecution:
        if isinstance(step, NavigateStep):
            decision = self.policy.check_url(self.surface_base(step.path))
            if not decision.allowed:
                raise StepFailedError(
                    self._failure(FailureCode.POLICY_DENIED, FailureCategory.POLICY, decision.reason, step)
                )
            self.guard()
            await self._guarded_action(step, self.surface.navigate(step.path, step.frame))
            return StepExecution("navigate", None, ())

        value: str | None = None
        if isinstance(step, FillStep | SelectStep):
            expr = step.value if isinstance(step, FillStep) else step.option
            value = self._value(expr, ctx, step)
            if value is None:
                self.recorder.event("step.skipped", step=step.id, reason="optional input not supplied")
                return StepExecution(None, None, (), skipped=True)

        resolution = await self._resolve(step)
        attempts = tuple(LocatorAttemptRecord(**a.__dict__) for a in resolution.attempts)
        self.guard()
        match step:
            case ClickStep():
                await self._guarded_action(step, self.surface.click(resolution))
            case PressStep(key=key):
                await self._guarded_action(step, self.surface.press(resolution, key))
            case FillStep(value=expr):
                assert value is not None
                await self._guarded_action(
                    step, self.surface.fill(resolution, value, secret=isinstance(expr, SecretValue))
                )
            case SelectStep():
                assert value is not None
                await self._guarded_action(step, self.surface.select(resolution, value))
            case ExtractStep(output=name):
                await self._extract(step, name, resolution, ctx)
        return StepExecution(resolution.strategy, resolution.locator_index, attempts)

    def surface_base(self, path: str) -> str:
        return urljoin(self.surface.base_url.rstrip("/") + "/", path.lstrip("/"))

    async def _guarded_action(self, step: Step, action: Awaitable[Any]) -> None:
        try:
            await action
        except ActionError as exc:
            # The action may have failed *because* of an exceptional state (e.g. an overlay).
            state = await self.surface.state()
            if self.monitor is not None:
                detection = await self.monitor.detect(state)
                if detection is not None:
                    raise StateInterrupt(detection, "before") from None
            raise StepFailedError(
                self._failure(
                    FailureCode.ACTION_FAILED,
                    FailureCategory.AUTOMATION,
                    str(exc),
                    step,
                    expected=_target_description(step),
                    observed=self._observed(state),
                )
            ) from None

    async def _extract(self, step: ExtractStep, name: str, resolution: Resolution, ctx: StepContext) -> None:
        raw = await self.surface.read_text(resolution)
        spec = ctx.capability.output(name) if ctx.capability else None
        if spec is None:
            ctx.outputs[name] = raw
            return
        try:
            value = parse_output(spec, raw)
        except ValueError as exc:
            raise StepFailedError(
                self._failure(
                    FailureCode.OUTPUT_INVALID,
                    FailureCategory.AUTOMATION,
                    f"output '{name}' could not be parsed as {spec.type}",
                    step,
                    expected=f"{spec.type} value",
                    observed=self.recorder.redactor.text(str(exc)),
                )
            ) from None
        if not spec.sensitivity.persist_in_clear:
            self.recorder.redactor.register(raw, f"output:{name}")
            self.recorder.redactor.register(str(value), f"output:{name}")
        ctx.outputs[name] = value
        shown = value if spec.sensitivity.persist_in_clear else f"<{spec.sensitivity}>"
        self.recorder.event("output.extracted", step=step.id, output=name, value_type=spec.type, value=shown)

    def _value(self, expr: ValueExpr, ctx: StepContext, step: Step) -> str | None:
        match expr:
            case ParamValue(param=param):
                if param in ctx.inputs:
                    return ctx.inputs[param]
                spec = ctx.capability.input(param) if ctx.capability else None
                if spec is not None and not spec.required:
                    return None
                raise StepFailedError(
                    self._failure(
                        FailureCode.INPUT_INVALID, FailureCategory.CALLER, f"input '{param}' not supplied", step
                    )
                )
            case SecretValue(secret=key):
                try:
                    return ctx.vault.get(key)
                except SecretUnavailableError as exc:
                    raise StepFailedError(
                        self._failure(FailureCode.SESSION_FAILED, FailureCategory.INTERNAL, str(exc), step)
                    ) from None
            case LiteralValue(literal=literal):
                return literal
        raise TypeError(expr)  # pragma: no cover

    async def _resolve(self, step: Step) -> Resolution:
        target = getattr(step, "target", None)
        assert target is not None
        last_miss: list[ResolutionMiss] = []

        async def probe(_state: SurfaceState) -> Resolution | None:
            result = await self.surface.try_resolve(target)
            if isinstance(result, Resolution):
                return result
            last_miss[:] = [result]
            return None

        try:
            return await self._poll(probe, timeout_ms=step.timeout_ms, phase="before")
        except _Deadline as d:
            miss = last_miss[0] if last_miss else ResolutionMiss(())
            code = FailureCode.TARGET_AMBIGUOUS if miss.ambiguous else FailureCode.TARGET_NOT_FOUND
            raise StepFailedError(
                self._failure(
                    code,
                    FailureCategory.AUTOMATION,
                    f"{'ambiguous' if miss.ambiguous else 'could not find'} target: {target.describe()}",
                    step,
                    expected=target.describe(),
                    observed=self._observed(d.state),
                    attempts=[LocatorAttemptRecord(**a.__dict__) for a in miss.attempts],
                )
            ) from None

    # ------------------------------------------------------------------ polling core

    async def _poll(self, probe: Callable[[SurfaceState], Awaitable[T | None]], *, timeout_ms: int, phase: Phase) -> T:
        deadline = time.monotonic() + timeout_ms / 1000
        state: SurfaceState | None = None
        while True:
            state = await self.surface.state()
            if self.monitor is not None:
                detection = await self.monitor.detect(state)
                if detection is not None:
                    raise StateInterrupt(detection, phase)
            result = await probe(state)
            if result is not None:
                return result
            if time.monotonic() >= deadline:
                raise _Deadline(state)
            await asyncio.sleep(POLL_INTERVAL_S)

    # ------------------------------------------------------------------ failure helpers

    def _observed(self, state: SurfaceState | None) -> str | None:
        return state.summary() if state else None

    def _failure(
        self,
        code: FailureCode,
        category: FailureCategory,
        message: str,
        step: Step | None,
        *,
        expected: str | None = None,
        observed: str | None = None,
        attempts: list[LocatorAttemptRecord] | None = None,
    ) -> Failure:
        return Failure(
            code=code,
            category=category,
            message=self.recorder.redactor.text(message),
            step_id=step.id if step else None,
            step_title=step.title if step else None,
            expected=expected,
            observed=observed,
            locator_attempts=attempts or [],
        )


async def _true() -> bool:
    return True


def _target_description(step: Step) -> str | None:
    target = getattr(step, "target", None)
    return target.describe() if target is not None else None


SENSITIVE_INPUTS = (Sensitivity.PII, Sensitivity.SECRET, Sensitivity.CONFIDENTIAL)
