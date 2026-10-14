"""Deterministic replay: the production execution path for a capability.

No model is consulted. Given a tenant-bound capability and caller inputs, the engine:

1. validates inputs against the contract (fails before touching the app),
2. pre-flights every step against policy (never starts a flow it is not allowed to finish),
3. establishes a session and checks the app version against the capability's range,
4. executes steps with verified locators, racing each wait against known exceptional states,
5. handles recoverable states (interstitials, transient errors, session expiry) explicitly,
   stops on business outcomes, and escalates or fails on anything else,
6. verifies the success checkpoint and returns typed outputs.
"""

from __future__ import annotations

import asyncio
import time
import traceback
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Literal

from cua.artifact.common import Risk
from cua.artifact.contract import ContractError, validate_inputs
from cua.artifact.profile import HandlerKind, StateHandler
from cua.artifact.steps import Step
from cua.artifact.versions import satisfies
from cua.evidence.recorder import RunRecorder, portable_path
from cua.hitl.control import ControlChannel, LeaseLostError
from cua.hitl.desk import InterventionDesk, InterventionKind, InterventionOutcome, Resolution
from cua.runtime.executor import SENSITIVE_INPUTS, StateInterrupt, StepContext, StepExecutor, StepFailedError
from cua.runtime.monitor import StateMonitor
from cua.runtime.result import (
    BusinessOutcome,
    DriftRecord,
    Failure,
    FailureCategory,
    FailureCode,
    InterventionRecord,
    RecoveryRecord,
    RunResult,
    RunStatus,
    StepRecord,
)
from cua.runtime.session import SessionError, SessionManager
from cua.runtime.version import probe_version
from cua.safety.policy import ActionType, Mode, RiskRule
from cua.safety.secrets import SecretVault
from cua.surface.base import Surface
from cua.workspace import Binding

_STALE = " (the artifact was modified after it was approved: its digest no longer matches the approval)"


@dataclass(frozen=True)
class ReplayOptions:
    on_failure: Literal["fail", "escalate"] = "fail"
    max_restarts: int = 1
    screenshots: Literal["all", "failure"] = "all"


@dataclass
class _PlannedStep:
    step: Step
    risk: Risk
    needs_approval: bool


@dataclass
class _RunState:
    started: float = field(default_factory=time.monotonic)
    started_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    steps: list[StepRecord] = field(default_factory=list)
    recoveries: list[RecoveryRecord] = field(default_factory=list)
    drift: list[DriftRecord] = field(default_factory=list)
    interventions: list[InterventionRecord] = field(default_factory=list)
    handler_attempts: dict[str, int] = field(default_factory=dict)
    committed: bool = False
    human_assisted: bool = False
    app_version: str | None = None


class _Finished(Exception):
    def __init__(self, result: RunResult) -> None:
        self.result = result


class ReplayEngine:
    def __init__(
        self,
        binding: Binding,
        surface: Surface,
        recorder: RunRecorder,
        vault: SecretVault,
        *,
        channel: ControlChannel | None = None,
        desk: InterventionDesk | None = None,
        options: ReplayOptions | None = None,
    ) -> None:
        self.binding = binding
        self.capability = binding.capability
        self.surface = surface
        self.recorder = recorder
        self.vault = vault
        self.channel = channel or ControlChannel()
        self.desk = desk
        self.options = options or ReplayOptions()
        self._epoch = self.channel.lease.epoch
        self.monitor = StateMonitor(surface, binding.profile, list(self.capability.spec.outcomes))
        self.executor = StepExecutor(surface, recorder, binding.policy, monitor=self.monitor, guard=self._guard)
        plain = StepExecutor(surface, recorder, binding.policy, monitor=None, guard=self._guard)
        self.plain_executor = plain
        self.session = SessionManager(binding.profile, surface, plain, vault, recorder)
        self._run = _RunState()

    # ------------------------------------------------------------------ entry point

    async def run(self, raw_inputs: dict[str, Any]) -> RunResult:
        cap = self.capability
        self.recorder.event(
            "run.started",
            mode="replay",
            capability=cap.ref,
            digest=cap.metadata.digest,
            tenant=self.binding.tenant.metadata.name,
            overrides=list(self.binding.applied_overrides),
            approved=self.binding.approved,
            on_failure=self.options.on_failure,
        )
        try:
            result = await self._run_guarded(raw_inputs)
        except _Finished as done:
            result = done.result
        except LeaseLostError as exc:
            result = self._failed(
                Failure(code=FailureCode.INTERNAL_ERROR, category=FailureCategory.INTERNAL, message=str(exc))
            )
        except Exception as exc:
            self.recorder.write_text("internal-error.txt", traceback.format_exc())
            result = self._failed(
                Failure(
                    code=FailureCode.INTERNAL_ERROR,
                    category=FailureCategory.INTERNAL,
                    message=f"{type(exc).__name__}: {exc}",
                    evidence={"traceback": "internal-error.txt"},
                )
            )
        self.recorder.write_json("result.json", result.for_persistence(cap, self.recorder.redactor))
        self.recorder.event(
            "run.finished",
            status=result.status,
            duration_ms=result.duration_ms,
            outcome=result.outcome.code if result.outcome else None,
            failure=result.failure.code if result.failure else None,
        )
        return result

    async def _run_guarded(self, raw_inputs: dict[str, Any]) -> RunResult:
        cap = self.capability
        try:
            inputs = validate_inputs(cap, raw_inputs)
        except ContractError as exc:
            return self._failed(
                Failure(
                    code=FailureCode.INPUT_INVALID, category=FailureCategory.CALLER, message=str(exc), retryable=False
                )
            )
        for spec in cap.spec.inputs:
            if spec.name in inputs and spec.sensitivity in SENSITIVE_INPUTS:
                self.recorder.redactor.register(inputs[spec.name], f"param:{spec.name}")
        self.recorder.event("inputs.validated", inputs=inputs)

        plan = self._preflight()
        try:
            await self.session.ensure()
        except SessionError as exc:
            return self._failed(
                Failure(
                    code=FailureCode.APP_UNREACHABLE if exc.unreachable else FailureCode.SESSION_FAILED,
                    category=FailureCategory.APPLICATION,
                    message=str(exc),
                    retryable=True,
                )
            )
        await self._check_version()
        await self._enter()

        ctx = StepContext(inputs=inputs, vault=self.vault, capability=cap)
        await self._execute_plan(plan, ctx)
        missing = [o.name for o in cap.spec.outputs if o.required and o.name not in ctx.outputs]
        if missing:
            return self._failed(
                Failure(
                    code=FailureCode.OUTPUT_INVALID,
                    category=FailureCategory.AUTOMATION,
                    message=f"required outputs missing: {missing}",
                )
            )
        return self._result(RunStatus.SUCCEEDED, outputs=ctx.outputs)

    # ------------------------------------------------------------------ planning / setup

    def _preflight(self) -> list[_PlannedStep]:
        policy = self.binding.policy
        plan: list[_PlannedStep] = []
        for step in self.capability.spec.steps:
            action = ActionType(step.action)
            label = _step_label(step)
            risk = policy.classify(action, label, step.risk)
            decision = policy.decide(Mode.REPLAY, action, risk, artifact_approved=self.binding.approved)
            needs_approval = False
            if not decision.allowed:
                if decision.rule in (RiskRule.REQUIRE_APPROVED_ARTIFACT, RiskRule.REQUIRE_HUMAN_APPROVAL):
                    if self.options.on_failure != "escalate" or self.desk is None:
                        raise _Finished(
                            self._failed(
                                Failure(
                                    code=FailureCode.APPROVAL_REQUIRED,
                                    category=FailureCategory.CALLER,
                                    message=f"step '{step.id}' ({step.title}) is {risk}: {decision.reason}"
                                    f"{_STALE if self.binding.base.approval_state() == 'stale' else ''}. "
                                    "Approve the capability (cua approve) or run with --on-failure escalate "
                                    "for a just-in-time operator approval.",
                                    step_id=step.id,
                                    step_title=step.title,
                                )
                            )
                        )
                    needs_approval = True
                else:
                    raise _Finished(
                        self._failed(
                            Failure(
                                code=FailureCode.POLICY_DENIED,
                                category=FailureCategory.POLICY,
                                message=f"step '{step.id}': {decision.reason}",
                                step_id=step.id,
                                step_title=step.title,
                            )
                        )
                    )
            if risk is not step.risk:
                self.recorder.event("policy.risk_escalated", step=step.id, declared=step.risk, effective=risk)
            plan.append(_PlannedStep(step, risk, needs_approval))
        self.recorder.event(
            "run.planned",
            steps=[
                {"id": p.step.id, "action": p.step.action, "risk": p.risk, "approval": p.needs_approval} for p in plan
            ],
        )
        return plan

    async def _check_version(self) -> None:
        if self.binding.profile.spec.version_probe is None:
            return
        version = await probe_version(self.surface, self.binding.profile)
        self._run.app_version = version
        self.recorder.event(
            "app.version",
            detected=version,
            expected=self.binding.app.expected_version,
            supported=self.capability.metadata.app_versions,
        )
        if version and self.binding.app.expected_version and version != self.binding.app.expected_version:
            self._run.drift.append(
                DriftRecord(
                    kind="app_version",
                    detail=f"tenant expects {self.binding.app.expected_version}, app reports {version}",
                )
            )
        if not satisfies(version, self.capability.metadata.app_versions):
            raise _Finished(
                self._failed(
                    Failure(
                        code=FailureCode.VERSION_UNSUPPORTED,
                        category=FailureCategory.AUTOMATION,
                        message=f"app version {version!r} is outside the capability's supported range "
                        f"{self.capability.metadata.app_versions!r}",
                        retryable=False,
                    )
                )
            )

    async def _enter(self) -> None:
        entry = self.capability.spec.entry.path
        if entry:
            await self.surface.navigate(entry)
            await self.surface.wait_settled()

    # ------------------------------------------------------------------ main loop

    async def _execute_plan(self, plan: list[_PlannedStep], ctx: StepContext) -> None:
        index = 0
        phase: Literal["before", "after"] = "before"
        restarts = 0
        approved_steps: set[str] = set()
        while index <= len(plan):
            final = index == len(plan)
            planned = None if final else plan[index]
            step = None if planned is None else planned.step
            started = time.monotonic()
            try:
                if step is None:
                    await self.executor.await_condition(self.capability.spec.success, timeout_ms=15_000, phase="after")
                    self.recorder.event("checkpoint.passed", condition=self.capability.spec.success.describe())
                    return
                assert planned is not None
                if planned.needs_approval and phase == "before" and step.id not in approved_steps:
                    outcome = await self._escalate(
                        InterventionKind.APPROVAL,
                        step,
                        f"Approval required: '{step.title}' is an irreversible action",
                        [Resolution.APPROVE, Resolution.REJECT, Resolution.STEP_DONE, Resolution.ABORT],
                    )
                    if outcome.resolution is Resolution.STEP_DONE:
                        await self._human_completed(step, index)
                        index += 1
                        continue
                    if outcome.resolution is not Resolution.APPROVE:
                        raise _Finished(
                            self._failed(
                                Failure(
                                    code=FailureCode.APPROVAL_REQUIRED,
                                    category=FailureCategory.OPERATOR,
                                    message=f"operator {outcome.resolution.value}: {outcome.note or 'no note'}",
                                    step_id=step.id,
                                    step_title=step.title,
                                )
                            )
                        )
                    approved_steps.add(step.id)
                self.recorder.event(
                    "step.started",
                    index=index,
                    step=step.id,
                    action=step.action,
                    title=step.title,
                    risk=planned.risk,
                    phase=phase,
                )
                execution = await self.executor.execute(step, ctx, phase=phase)
                if execution.used_fallback:
                    self._run.drift.append(
                        DriftRecord(
                            kind="locator_fallback",
                            step_id=step.id,
                            detail=f"primary locator failed; resolved via '{execution.strategy}' "
                            f"(attempts: {[a.model_dump() for a in execution.attempts]})",
                        )
                    )
                    self.recorder.event("drift.locator_fallback", step=step.id, strategy=execution.strategy)
                if planned.risk is not Risk.READ and not execution.skipped:
                    self._run.committed = True
                shot = await self._step_screenshot(step) if self.options.screenshots == "all" else None
                self._run.steps.append(
                    StepRecord(
                        id=step.id,
                        title=step.title,
                        status="skipped" if execution.skipped else "ok",
                        risk=planned.risk,
                        strategy=execution.strategy,
                        duration_ms=int((time.monotonic() - started) * 1000),
                        screenshot=shot,
                    )
                )
                self.recorder.event(
                    "step.completed", step=step.id, strategy=execution.strategy, fallback=execution.used_fallback
                )
                index += 1
                phase = "before"
            except StateInterrupt as interrupt:
                phase, index, restarts = await self._handle_interrupt(interrupt, step, index, phase, restarts)
            except StepFailedError as failed:
                failure = failed.failure.model_copy(update={"step_index": index})
                failure = await self._attach_evidence(failure)
                if self.options.on_failure == "escalate" and self.desk is not None and step is not None:
                    outcome = await self._escalate(
                        InterventionKind.FAILURE,
                        step,
                        f"{failure.code}: {failure.message}",
                        [Resolution.RETRY_STEP, Resolution.STEP_DONE, Resolution.ABORT],
                        failure=failure,
                    )
                    if outcome.resolution is Resolution.RETRY_STEP:
                        phase = "before"
                        continue
                    if outcome.resolution is Resolution.STEP_DONE:
                        await self._human_completed(step, index)
                        index += 1
                        phase = "before"
                        continue
                    code = FailureCode.ESCALATION_TIMEOUT if outcome.timed_out else FailureCode.OPERATOR_ABORTED
                    raise _Finished(
                        self._failed(
                            failure.model_copy(
                                update={
                                    "code": code,
                                    "category": FailureCategory.OPERATOR,
                                    "message": f"{failure.message} (escalated; operator: {outcome.resolution.value})",
                                }
                            )
                        )
                    ) from None
                raise _Finished(self._failed(failure)) from None

    # ------------------------------------------------------------------ exceptional states

    async def _handle_interrupt(
        self,
        interrupt: StateInterrupt,
        step: Step | None,
        index: int,
        phase: Literal["before", "after"],
        restarts: int,
    ) -> tuple[Literal["before", "after"], int, int]:
        detection = interrupt.detection
        step_id = step.id if step else "success_checkpoint"
        if detection.outcome is not None:
            await self._capture("outcome")
            raise _Finished(
                self._result(
                    RunStatus.BUSINESS_OUTCOME,
                    outcome=BusinessOutcome(
                        code=detection.outcome.code,
                        message=detection.message,
                        description=detection.outcome.description,
                        step_id=step_id,
                        declared=detection.declared,
                    ),
                )
            )
        handler = detection.handler
        assert handler is not None
        attempt = self._run.handler_attempts.get(handler.id, 0) + 1
        self._run.handler_attempts[handler.id] = attempt
        self.recorder.event(
            "state.detected",
            step=step_id,
            handler=handler.id,
            kind=handler.kind,
            phase=interrupt.phase,
            attempt=attempt,
            message=detection.message,
        )
        if attempt > handler.max_attempts:
            raise _Finished(
                self._failed(
                    await self._attach_evidence(
                        Failure(
                            code=FailureCode.APP_ERROR
                            if handler.kind is HandlerKind.TRANSIENT_ERROR
                            else FailureCode.RECOVERY_EXHAUSTED,
                            category=FailureCategory.APPLICATION,
                            retryable=handler.kind is HandlerKind.TRANSIENT_ERROR,
                            message=f"'{handler.id}' persisted after {handler.max_attempts} recovery attempt(s): "
                            f"{handler.description}",
                            step_id=step_id,
                            step_index=index,
                        )
                    )
                )
            )

        match handler.kind:
            case HandlerKind.FATAL:
                raise _Finished(
                    self._failed(
                        await self._attach_evidence(
                            Failure(
                                code=FailureCode.APP_ERROR,
                                category=FailureCategory.APPLICATION,
                                message=f"{handler.description}: {detection.message or ''}".strip(": "),
                                step_id=step_id,
                                step_index=index,
                            )
                        )
                    )
                )
            case HandlerKind.INTERSTITIAL:
                await self._run_handler_steps(handler)
                self._recovered(handler, step_id, attempt, "dismissed interstitial")
                return interrupt.phase, index, restarts
            case HandlerKind.TRANSIENT_ERROR:
                if interrupt.phase == "after" and step is not None and step.risk is not Risk.READ:
                    raise _Finished(
                        self._failed(
                            await self._attach_evidence(
                                Failure(
                                    code=FailureCode.OUTCOME_UNKNOWN,
                                    category=FailureCategory.APPLICATION,
                                    retryable=False,
                                    message="application error after a state-changing action - the transaction "
                                    "may or may not have been committed; verify before retrying",
                                    step_id=step_id,
                                    step_index=index,
                                )
                            )
                        )
                    )
                await _sleep_ms(handler.backoff_ms * attempt)
                await self.surface.reload(handler.reload_frame)
                await self.surface.wait_settled()
                where = handler.reload_frame.describe() if handler.reload_frame else "page"
                self._recovered(handler, step_id, attempt, f"reloaded {where}")
                return interrupt.phase, index, restarts
            case HandlerKind.SESSION_EXPIRED:
                if self._run.committed:
                    raise _Finished(
                        self._failed(
                            await self._attach_evidence(
                                Failure(
                                    code=FailureCode.SESSION_FAILED,
                                    category=FailureCategory.APPLICATION,
                                    retryable=False,
                                    message="session expired after a state-changing step; not restarting automatically",
                                    step_id=step_id,
                                    step_index=index,
                                )
                            )
                        )
                    )
                if restarts >= self.options.max_restarts:
                    raise _Finished(
                        self._failed(
                            Failure(
                                code=FailureCode.RECOVERY_EXHAUSTED,
                                category=FailureCategory.APPLICATION,
                                retryable=True,
                                message="session expired repeatedly",
                                step_id=step_id,
                                step_index=index,
                            )
                        )
                    )
                await self.session.login()
                await self._enter()
                self._recovered(handler, step_id, attempt, "re-authenticated; restarting capability from step 1")
                self._run.steps.clear()
                return "before", 0, restarts + 1
        raise AssertionError(handler.kind)  # pragma: no cover

    async def _run_handler_steps(self, handler: StateHandler) -> None:
        ctx = StepContext(inputs={}, vault=self.vault)
        for sub in handler.steps:
            await self.plain_executor.execute(sub, ctx)

    def _recovered(self, handler: StateHandler, step_id: str, attempt: int, detail: str) -> None:
        record = RecoveryRecord(handler=handler.id, kind=handler.kind, step_id=step_id, attempt=attempt, detail=detail)
        self._run.recoveries.append(record)
        self.recorder.event("recovery.applied", **record.model_dump())

    # ------------------------------------------------------------------ escalation

    async def _escalate(
        self,
        kind: InterventionKind,
        step: Step,
        reason: str,
        allowed: list[Resolution],
        failure: Failure | None = None,
    ) -> InterventionOutcome:
        assert self.desk is not None
        context = {
            "capability": self.capability.ref,
            "tenant": self.binding.tenant.metadata.name,
            "step_id": step.id,
            "step_title": step.title,
            "step_action": step.action,
            "target": _step_label(step),
            "expected": step.expect.describe() if step.expect else None,
            "failure": failure.model_dump(mode="json", exclude_none=True) if failure else None,
        }
        outcome = await self.desk.request(kind, reason, context=context, allowed=allowed)
        self._epoch = self.channel.lease.epoch  # control returned through the desk: re-fence
        self._run.interventions.append(
            InterventionRecord(
                id=outcome.intervention_id,
                kind=kind,
                reason=reason,
                operator=outcome.operator,
                resolution=outcome.resolution,
                human_actions=len(outcome.human_actions),
                waited_ms=outcome.waited_ms,
            )
        )
        if outcome.human_actions or outcome.resolution in (Resolution.STEP_DONE, Resolution.APPROVE):
            self._run.human_assisted = True
        return outcome

    async def _human_completed(self, step: Step, index: int) -> None:
        """The human says they performed the step - verify its checkpoint rather than trust it."""
        await self.surface.wait_settled()
        try:
            await self.executor.await_expectation(step)
        except StepFailedError as exc:
            raise _Finished(
                self._failed(
                    exc.failure.model_copy(
                        update={
                            "message": "operator marked step done but its checkpoint does not hold: "
                            + exc.failure.message,
                            "step_index": index,
                            "category": FailureCategory.OPERATOR,
                        }
                    )
                )
            ) from None
        if step.risk is not Risk.READ:
            self._run.committed = True
        self._run.steps.append(StepRecord(id=step.id, title=step.title, status="done_by_human", risk=step.risk))
        self.recorder.event("step.completed", step=step.id, by="human")

    # ------------------------------------------------------------------ evidence + results

    def _guard(self) -> None:
        self.channel.assert_automation(self._epoch)

    async def _step_screenshot(self, step: Step) -> str | None:
        try:
            path = await self.surface.screenshot(self.recorder.next_name(f"step-{step.id}", ".png"))
            return self.recorder.relative(path)
        except Exception:
            return None

    async def _capture(self, label: str) -> dict[str, Any]:
        evidence: dict[str, Any] = {}
        try:
            shot = await self.surface.screenshot(self.recorder.next_name(label, ".png"))
            evidence["screenshot"] = self.recorder.relative(shot)
            dom = await self.surface.dom_snapshot(self.recorder.dir / f"dom-{label}", self.recorder.redactor)
            evidence["dom"] = [self.recorder.relative(p) for p in dom]
            observation = await self.surface.observe()
            evidence["observation"] = self.recorder.relative(
                self.recorder.write_text(
                    f"observation-{label}.txt",
                    observation.render(self.recorder.redactor, self.surface.sensitive_label_patterns),
                )
            )
        except Exception as exc:
            evidence["capture_error"] = str(exc)
        return evidence

    async def _attach_evidence(self, failure: Failure) -> Failure:
        evidence = await self._capture(f"failure-{failure.step_id or 'run'}")
        return failure.model_copy(update={"evidence": {**failure.evidence, **evidence}})

    def _failed(self, failure: Failure) -> RunResult:
        self.recorder.event("run.failed", **failure.model_dump(mode="json", exclude_none=True))
        return self._result(RunStatus.FAILED, failure=failure)

    def _result(
        self,
        status: RunStatus,
        *,
        outputs: dict[str, Any] | None = None,
        outcome: BusinessOutcome | None = None,
        failure: Failure | None = None,
    ) -> RunResult:
        run = self._run
        return RunResult(
            run_id=self.recorder.run_id,
            capability=self.capability.ref,
            digest=self.capability.metadata.digest,
            tenant=self.binding.tenant.metadata.name,
            tenant_overrides=list(self.binding.applied_overrides),
            status=status,
            outputs=outputs or {},
            outcome=outcome,
            failure=failure,
            recoveries=run.recoveries,
            drift=run.drift,
            interventions=run.interventions,
            steps=run.steps,
            human_assisted=run.human_assisted,
            app_version=run.app_version,
            started_at=run.started_at,
            duration_ms=int((time.monotonic() - run.started) * 1000),
            evidence_dir=portable_path(self.recorder.dir),
        )


def _step_label(step: Step) -> str | None:
    target = getattr(step, "target", None)
    if target is None:
        return None
    names = [
        getattr(loc, f)
        for loc in target.locators
        for f in ("name", "label", "text")
        if isinstance(getattr(loc, f, None), str)
    ]
    return " / ".join(dict.fromkeys([target.description, *names]))


async def _sleep_ms(ms: int) -> None:
    await asyncio.sleep(ms / 1000)
