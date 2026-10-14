"""LLM-driven discovery: observe -> decide -> act until the goal is met, recording a capability.

Safety properties of the loop:

* The model sees a *redacted* semantic view of the screen and never sees credentials or
  masked input values; it can only refer to inputs by name.
* Every proposed action is validated (ref exists, input declared, constants not sensitive),
  risk-classified and policy-checked *before* it touches the app; irreversible actions are
  routed to a human for approval.
* Actions are performed through the verified locators that will be stored in the artifact,
  so discovery exercises exactly what replay will execute.
* Known interstitials/transient errors are handled by the app profile between turns and are
  therefore *not* recorded as part of the flow.
* Stuck detection (repeated action, repeated errors, step budget) escalates to a human.
"""

from __future__ import annotations

import re
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from cua.artifact.capability import Capability
from cua.artifact.common import Risk
from cua.artifact.conditions import AllOf, TextVisible, UrlMatches
from cua.artifact.contract import parse_output
from cua.artifact.io import save
from cua.artifact.profile import AppProfile, HandlerKind
from cua.artifact.targets import FrameRef, Target
from cua.artifact.values import LiteralValue, ParamValue, ValueExpr
from cua.discovery.llm import LLMClient, LLMError, ToolCall
from cua.discovery.prompts import SYSTEM_PROMPT, build_tools, build_user_prompt
from cua.discovery.recorder import ArtifactLeakError, FlowRecorder, derive_expectation, steps_from_human_actions
from cua.discovery.request import DiscoveryRequest
from cua.evidence.recorder import RunRecorder, portable_path
from cua.hitl.control import ControlChannel
from cua.hitl.desk import InterventionDesk, InterventionKind, InterventionOutcome, Resolution
from cua.runtime.executor import StepContext, StepExecutor
from cua.runtime.monitor import StateMonitor
from cua.runtime.session import SessionManager
from cua.runtime.version import probe_version
from cua.safety.policy import ActionType, Mode, Policy
from cua.safety.secrets import SecretVault
from cua.surface.base import ActionError, Observation, ObservedElement, Surface
from cua.surface.base import Resolution as Resolved

Status = Literal["completed", "gave_up", "aborted", "failed"]


@dataclass
class DiscoveryOutcome:
    status: Status
    reason: str
    run_id: str
    turns: int
    capability: Capability | None = None
    artifact_path: Path | None = None
    usage: dict[str, int] = field(default_factory=dict)


class AgentActionError(RuntimeError):
    """The proposed action was invalid or failed; fed back to the model as an observation."""


class _Done(Exception):
    def __init__(self, outcome: DiscoveryOutcome) -> None:
        self.outcome = outcome


@dataclass(frozen=True)
class DiscoveryContext:
    tenant: str
    profile: AppProfile
    policy: Policy
    artifact_dir: Path


class DiscoveryAgent:
    def __init__(
        self,
        request: DiscoveryRequest,
        context: DiscoveryContext,
        surface: Surface,
        llm: LLMClient,
        recorder: RunRecorder,
        vault: SecretVault,
        *,
        channel: ControlChannel,
        desk: InterventionDesk | None,
        send_screenshots: bool = False,
    ) -> None:
        self.request = request
        self.ctx = context
        self.surface = surface
        self.llm = llm
        self.recorder = recorder
        self.redactor = recorder.redactor
        self.vault = vault
        self.channel = channel
        self.desk = desk
        self.send_screenshots = send_screenshots
        self.flow = FlowRecorder(request, self.redactor)
        self._epoch = channel.lease.epoch
        plain = StepExecutor(surface, recorder, context.policy, monitor=None, guard=self._guard)
        self.plain = plain
        self.monitor = StateMonitor(surface, context.profile, [])
        self.session = SessionManager(context.profile, surface, plain, vault, recorder)
        self.tools = build_tools(request)
        self.inputs = dict(request.spec.values)
        self.extracted: dict[str, Any] = {}
        self.history: list[str] = []
        self.notes: list[str] = []
        self.usage: dict[str, int] = {"input_tokens": 0, "output_tokens": 0}
        self.human_assisted = False
        self._signatures: deque[str] = deque(maxlen=3)
        self._errors = 0
        self._alerts: list[str] = []
        self._app_version: str | None = None
        self.max_steps = request.spec.max_steps

    # ------------------------------------------------------------------ run

    async def run(self) -> DiscoveryOutcome:
        spec = self.request.spec
        for item in spec.inputs:
            if item.name in self.inputs and not item.sensitivity.persist_in_clear:
                self.redactor.register(self.inputs[item.name], f"param:{item.name}")
        goal = spec.goal
        for name in self.inputs:
            goal = goal.replace("{" + name + "}", f"[param:{name}]")
        self.goal = goal
        self.recorder.event(
            "run.started",
            mode="discovery",
            capability=self.request.metadata.name,
            tenant=self.ctx.tenant,
            model=self.llm.model,
            provider=self.llm.provider,
            goal=goal,
            max_steps=self.max_steps,
        )
        turn = 0
        try:
            await self.session.ensure()
            self._app_version = await probe_version(self.surface, self.ctx.profile)
            self.recorder.event("app.version", detected=self._app_version)
            while True:
                turn += 1
                if turn > self.max_steps:
                    await self._stuck(f"step budget of {self.max_steps} actions exhausted", turn)
                    continue
                await self._handle_known_states()
                await self._turn(turn)
        except _Done as done:
            outcome = done.outcome
        except LLMError as exc:
            outcome = self._outcome("failed", f"LLM error: {exc}", turn)
        except Exception as exc:
            self.recorder.event("run.error", error=f"{type(exc).__name__}: {exc}")
            outcome = self._outcome("failed", f"{type(exc).__name__}: {exc}", turn)
        self.recorder.event(
            "run.finished",
            status=outcome.status,
            reason=outcome.reason,
            turns=outcome.turns,
            usage=outcome.usage,
            artifact=portable_path(outcome.artifact_path) if outcome.artifact_path else None,
        )
        self.recorder.write_json(
            "result.json",
            {
                "run_id": outcome.run_id,
                "mode": "discovery",
                "status": outcome.status,
                "reason": outcome.reason,
                "turns": outcome.turns,
                "usage": outcome.usage,
                "model": self.llm.model,
                "artifact": portable_path(outcome.artifact_path) if outcome.artifact_path else None,
                "steps_recorded": len(self.flow.steps),
                "human_assisted": self.human_assisted,
            },
        )
        return outcome

    def _outcome(self, status: Status, reason: str, turn: int, **extra: Any) -> DiscoveryOutcome:
        return DiscoveryOutcome(
            status=status, reason=reason, run_id=self.recorder.run_id, turns=turn, usage=dict(self.usage), **extra
        )

    async def _turn(self, turn: int) -> None:
        observation = await self.surface.observe()
        screen = observation.render(self.redactor, self.surface_sensitive_labels())
        prompt = build_user_prompt(
            self.request,
            goal=self.goal,
            turn=turn,
            max_steps=self.max_steps,
            extracted=set(self.extracted),
            history=self.history,
            notes=[*self.notes, *self._alerts],
            screen=screen,
        )
        image = None
        if self.send_screenshots:
            shot = await self.surface.screenshot(self.recorder.dir / "llm" / f"turn-{turn:03d}.png")
            image = shot.read_bytes()
        started = time.monotonic()
        reply = await self.llm.decide(SYSTEM_PROMPT, prompt, self.tools, image)
        for key, value in reply.usage.items():
            self.usage[key] = self.usage.get(key, 0) + value
        call = reply.tool_call
        self.recorder.write_json(
            f"llm/turn-{turn:03d}.json",
            {
                "turn": turn,
                "model": reply.model,
                "latency_ms": int((time.monotonic() - started) * 1000),
                "usage": reply.usage,
                "prompt": prompt,
                "reply_text": reply.text,
                "tool_call": {"name": call.name, "arguments": call.arguments} if call else None,
            },
        )
        self.recorder.event(
            "agent.decision",
            turn=turn,
            tool=call.name if call else None,
            arguments=call.arguments if call else None,
            thought=reply.text,
        )
        await self._capture(f"turn-{turn:03d}")
        if call is None:
            self.history.append(f"{turn}. (no tool call) - you must call exactly one tool")
            self._errors += 1
            return
        try:
            summary = await self._dispatch(call, observation, turn)
            self.history.append(f"{turn}. {summary}")
            self._errors = 0
        except AgentActionError as exc:
            message = self.redactor.text(str(exc))
            self.history.append(f"{turn}. {call.name} FAILED: {message}")
            self.recorder.event("agent.action_rejected", turn=turn, tool=call.name, error=message)
            self._errors += 1
        signature = f"{call.name}:{call.arguments.get('ref') and self._describe_ref(observation, call)}"
        self._signatures.append(signature + "|" + observation.fingerprint)
        if self._errors >= 3:
            await self._stuck("three consecutive actions failed", turn)
        elif len(self._signatures) == 3 and len(set(self._signatures)) == 1:
            await self._stuck("repeating the same action on an unchanged screen", turn)
        elif len(self._signatures) >= 2 and self._signatures[-1] == self._signatures[-2]:
            self.history.append(
                f"{turn}. NOTE: you repeated the previous action and the screen did not change. The CURRENT "
                "SCREEN already is its result - continue from what is on it (e.g. the form fields it shows)."
            )

    # ------------------------------------------------------------------ tool dispatch

    async def _dispatch(self, call: ToolCall, obs: Observation, turn: int) -> str:
        args = call.arguments
        match call.name:
            case "click":
                return await self._click(obs, args)
            case "fill" | "select_option":
                return await self._set_value(obs, args, select=call.name == "select_option")
            case "press_enter":
                return await self._press(obs, args)
            case "extract":
                return await self._extract(obs, args)
            case "finish":
                await self._finish(args, turn)
            case "request_human":
                await self._stuck(f"agent requested help: {args.get('reason', '')}", turn, requested=True)
                return "requested human help"
            case "give_up":
                raise _Done(self._outcome("gave_up", str(args.get("reason", "")), turn))
        raise AgentActionError(f"unknown tool '{call.name}'")

    def _element(self, obs: Observation, args: dict[str, Any]) -> ObservedElement:
        ref = str(args.get("ref", ""))
        if ref not in obs.elements:
            raise AgentActionError(f"ref '{ref}' is not on the current screen")
        return obs.elements[ref]

    async def _target(
        self, element: ObservedElement, purpose: Literal["act", "extract"], description: str | None = None
    ) -> tuple[Target, Resolved]:
        try:
            target = await self.surface.target_for(element.ref, purpose=purpose, description=description)
        except ActionError as exc:
            raise AgentActionError(str(exc)) from exc
        resolution = await self.surface.try_resolve(target)
        if not isinstance(resolution, Resolved):
            raise AgentActionError(f"target no longer resolvable: {resolution.describe()}")
        return target, resolution

    async def _click(self, obs: Observation, args: dict[str, Any]) -> str:
        element = self._element(obs, args)
        label = element.display_name
        declared = Risk.IRREVERSIBLE if args.get("commits_transaction") else Risk.READ
        risk = self.ctx.policy.classify(ActionType.CLICK, label, declared)
        target, resolution = await self._target(element, "act")
        decision = self.ctx.policy.decide(Mode.DISCOVERY, ActionType.CLICK, risk)
        title = f"Click {target.description}"
        if not decision.allowed:
            if not decision.needs_human or self.desk is None:
                raise AgentActionError(f"blocked by policy: {decision.reason}")
            outcome = await self._ask(
                InterventionKind.APPROVAL,
                f"Agent wants to click '{label}' ({risk}): {args.get('reason', '')}",
                [Resolution.APPROVE, Resolution.REJECT, Resolution.STEP_DONE, Resolution.ABORT],
                step_title=title,
            )
            if outcome.resolution is Resolution.STEP_DONE:
                after = await self.surface.observe()
                self.flow.add(
                    "click",
                    target=target,
                    title=title,
                    rationale=args.get("reason"),
                    risk=risk,
                    expect=derive_expectation(obs, after, self.redactor, self.surface.sensitive_label_patterns),
                    source="human",
                )
                return f'operator performed "{label}" manually ({self._changes(obs, after)})'
            if outcome.resolution is not Resolution.APPROVE:
                raise AgentActionError(f"operator {outcome.resolution.value}: {outcome.note or 'no reason given'}")
            resolution = await self._reresolve(target)
        self._guard()
        await self._act(self.surface.click(resolution))
        after = await self._settled_observation()
        if dead_end := await self._dead_end(f'click {element.role} "{label}"'):
            return dead_end
        self.flow.add(
            "click",
            target=target,
            title=title,
            rationale=args.get("reason"),
            risk=risk,
            expect=derive_expectation(obs, after, self.redactor, self.surface.sensitive_label_patterns),
        )
        return f'click {element.role} "{label}" -> {self._changes(obs, after)}'

    async def _set_value(self, obs: Observation, args: dict[str, Any], *, select: bool) -> str:
        element = self._element(obs, args)
        expr = self._value_expr(args, literal_key="option" if select else "text")
        value = self.inputs[expr.param] if isinstance(expr, ParamValue) else expr.literal  # type: ignore[union-attr]
        target, resolution = await self._target(element, "act")
        self._guard()
        if select:
            await self._act(self.surface.select(resolution, value))
        else:
            await self._act(self.surface.fill(resolution, value))
        await self.surface.wait_settled()
        verb = "select" if select else "fill"
        self.flow.add(
            verb,
            target=target,
            title=f"{'Choose' if select else 'Enter'} {target.description}",
            rationale=args.get("reason"),
            risk=Risk.READ,
            expect=None,
            value=expr,
        )
        return f"{verb} {target.description} with {expr.describe()}"

    def _value_expr(self, args: dict[str, Any], *, literal_key: str) -> ValueExpr:
        name = str(args.get("input") or "")
        if name in self.inputs:
            return ParamValue(param=name)
        literal = args.get(literal_key)
        if not literal:
            if name:
                raise AgentActionError(
                    f"'{name}' is not a declared input (declared: {', '.join(self.inputs)}); "
                    f"for a fixed value use `{literal_key}` instead"
                )
            raise AgentActionError(f"provide either `input` or `{literal_key}`")
        literal = str(literal)
        if self.redactor.text(literal) != literal or literal in self.inputs.values():
            raise AgentActionError("that constant looks like sensitive/caller data - use `input` instead")
        return LiteralValue(literal=literal)

    async def _press(self, obs: Observation, args: dict[str, Any]) -> str:
        element = self._element(obs, args)
        target, resolution = await self._target(element, "act")
        self._guard()
        await self._act(self.surface.press(resolution, "Enter"))
        after = await self._settled_observation()
        if dead_end := await self._dead_end(f"press Enter in {target.description}"):
            return dead_end
        self.flow.add(
            "press",
            target=target,
            title=f"Press Enter in {target.description}",
            rationale=args.get("reason"),
            risk=Risk.READ,
            key="Enter",
            expect=derive_expectation(obs, after, self.redactor, self.surface.sensitive_label_patterns),
        )
        return f"press Enter in {target.description} -> {self._changes(obs, after)}"

    async def _extract(self, obs: Observation, args: dict[str, Any]) -> str:
        name = str(args.get("output", ""))
        spec = next((o for o in self.request.spec.outputs if o.name == name), None)
        if spec is None:
            raise AgentActionError(f"'{name}' is not a declared output")
        if name in self.extracted:
            raise AgentActionError(f"'{name}' is already extracted - when every output is done, call `finish`")
        element = self._element(obs, args)
        target, resolution = await self._target(element, "extract", description=spec.description)
        raw = await self.surface.read_text(resolution)
        try:
            value = parse_output(spec, raw)
        except ValueError as exc:
            raise AgentActionError(f"{exc} - pick the element that contains exactly the {spec.type} value") from exc
        if not spec.sensitivity.persist_in_clear:
            self.redactor.register(raw, f"output:{name}")
        self.extracted[name] = value
        self.flow.add(
            "extract",
            target=target,
            title=f"Read {spec.name.replace('_', ' ')}",
            rationale=args.get("reason"),
            risk=Risk.READ,
            expect=None,
            output=name,
        )
        self.recorder.event(
            "output.extracted", output=name, value_type=spec.type, locators=[loc.describe() for loc in target.locators]
        )
        return f"extracted {name} ({spec.type}) via {target.locators[0].describe()}"

    async def _finish(self, args: dict[str, Any], turn: int) -> None:
        missing = [o.name for o in self.request.spec.outputs if o.required and o.name not in self.extracted]
        if missing:
            raise AgentActionError(f"extract {missing} before finishing")
        text = str(args.get("success_text", "")).strip()
        if not text or self.redactor.text(text) != text or len(text) > 80:
            raise AgentActionError("success_text must be short, static, non-sensitive text visible on screen")
        state = await self.surface.state()
        frame = next((f for f in state.frames if f.name and text.casefold() in f.text.casefold()), None)
        if frame is None:
            raise AgentActionError(f"'{text}' is not visible in any named frame")
        ref = FrameRef(name=frame.name)
        success = AllOf(conditions=[UrlMatches(path=frame.path, frame=ref), TextVisible(text=text, frame=ref)])
        if not self.flow.steps:
            raise AgentActionError("nothing was recorded")
        try:
            capability = self.flow.build(
                run_id=self.recorder.run_id,
                tenant=self.ctx.tenant,
                app_version=self._app_version,
                model=f"{self.llm.provider}:{self.llm.model}",
                success=success,
                profile=self.ctx.profile,
                human_assisted=self.human_assisted,
            )
        except ArtifactLeakError as exc:
            raise _Done(self._outcome("failed", str(exc), turn)) from exc
        header = (
            f"Capability recorded by LLM discovery run {self.recorder.run_id}.\n"
            "Status: draft - review targets/risk before `cua approve`."
        )
        path = self.ctx.artifact_dir / f"{capability.metadata.name}.yaml"
        save(capability, path, header=header)
        save(capability, self.recorder.dir / "artifact.yaml", header=header)
        self.recorder.event(
            "artifact.written",
            path=portable_path(path),
            digest=capability.metadata.digest,
            steps=len(capability.spec.steps),
        )
        raise _Done(
            self._outcome("completed", str(args.get("summary", "")), turn, capability=capability, artifact_path=path)
        )

    # ------------------------------------------------------------------ escalation

    async def _stuck(self, reason: str, turn: int, *, requested: bool = False) -> None:
        if self.desk is None:
            raise _Done(self._outcome("aborted", f"needs a human but no operator console is available: {reason}", turn))
        outcome = await self._ask(InterventionKind.STUCK, reason, [Resolution.CONTINUE, Resolution.ABORT])
        if outcome.resolution is not Resolution.CONTINUE:
            raise _Done(
                self._outcome("aborted", f"operator {outcome.resolution.value}: {outcome.note or reason}", turn)
            )
        self._signatures.clear()
        self._errors = 0
        if turn > self.max_steps:
            self.max_steps += 10
        produced = steps_from_human_actions(outcome.human_actions, self.flow)
        did = "; ".join(s.title for s in produced) or "no recorded actions"
        self.notes.append(
            f"A human operator took over ({reason}). They did: {did}. "
            f"Operator note: {outcome.note or '-'}. Continue from the CURRENT SCREEN."
        )
        if not requested:
            self.history.append(f"{turn}. escalated to human: {reason}")

    async def _ask(
        self, kind: InterventionKind, reason: str, allowed: list[Resolution], step_title: str | None = None
    ) -> InterventionOutcome:
        assert self.desk is not None
        outcome = await self.desk.request(
            kind,
            reason,
            allowed=allowed,
            context={
                "goal": self.goal,
                "capability": self.request.metadata.name,
                "step_title": step_title,
                "recorded_steps": [s.title for s in self.flow.steps],
                "history": self.history[-10:],
            },
        )
        self._epoch = self.channel.lease.epoch
        if outcome.human_actions or outcome.resolution in (Resolution.APPROVE, Resolution.STEP_DONE):
            self.human_assisted = True
        return outcome

    # ------------------------------------------------------------------ known states between turns

    async def _handle_known_states(self) -> None:
        self._alerts = []
        for _ in range(3):
            state = await self.surface.state()
            detection = await self.monitor.detect(state)
            if detection is None:
                return
            if detection.outcome is not None:
                message = self.redactor.text(detection.message or detection.outcome.description or "")
                self._alerts.append(f"The application currently shows '{detection.outcome.code}': {message}")
                return
            handler = detection.handler
            assert handler is not None
            self.recorder.event("state.detected", handler=handler.id, kind=handler.kind)
            if handler.kind is HandlerKind.INTERSTITIAL:
                for step in handler.steps:
                    await self.plain.execute(step, StepContext(inputs={}, vault=self.vault))
                self.notes.append(f"Known interstitial '{handler.id}' was dismissed automatically (not recorded).")
            elif handler.kind is HandlerKind.TRANSIENT_ERROR:
                await self.surface.reload(handler.reload_frame)
                await self.surface.wait_settled()
                self.notes.append(f"Transient error '{handler.id}' recovered by reload.")
            elif handler.kind is HandlerKind.SESSION_EXPIRED:
                await self.session.login()
                self.notes.append("The session expired and was re-established; you are back on the start page.")
            else:
                self.notes.append(f"Application error: {handler.description}")
                return

    # ------------------------------------------------------------------ helpers

    async def _dead_end(self, action: str) -> str | None:
        """An action that made the app report a business outcome is not part of the happy path."""
        detection = await self.monitor.detect(await self.surface.state())
        if detection is None or detection.outcome is None:
            return None
        message = self.redactor.text(detection.message or detection.outcome.description or "")
        self.recorder.event("agent.dead_end", action=action, outcome=detection.outcome.code, message=message)
        return (
            f"{action} -> the application reported '{detection.outcome.code}' ({message}). "
            "This action was NOT recorded: fix the cause, then try again"
        )

    def _guard(self) -> None:
        self.channel.assert_automation(self._epoch)

    async def _act(self, action: Any) -> None:
        try:
            await action
        except ActionError as exc:
            raise AgentActionError(str(exc)) from exc

    async def _reresolve(self, target: Target) -> Resolved:
        resolution = await self.surface.try_resolve(target)
        if not isinstance(resolution, Resolved):
            raise AgentActionError("target disappeared while waiting for approval")
        return resolution

    async def _settled_observation(self) -> Observation:
        await self.surface.wait_settled()
        return await self.surface.observe()

    async def _capture(self, label: str) -> None:
        try:
            await self.surface.screenshot(self.recorder.dir / "screens" / f"{label}.png")
        except Exception as exc:
            self.recorder.event("evidence.capture_failed", label=label, error=str(exc))

    def surface_sensitive_labels(self) -> list[re.Pattern[str]]:
        return self.surface.sensitive_label_patterns

    @staticmethod
    def _describe_ref(obs: Observation, call: ToolCall) -> str:
        element = obs.elements.get(str(call.arguments.get("ref")))
        return element.display_name if element else "?"

    def _changes(self, before: Observation, after: Observation) -> str:
        old = {f.name: (f.path, f.heading) for f in before.frames}
        changes = [
            f'{f.name}: {f.path} "{f.heading or ""}"'
            for f in after.frames
            if f.name and old.get(f.name) != (f.path, f.heading)
        ]
        if not changes:
            new = len({e.text for e in after.elements.values()} - {e.text for e in before.elements.values()})
            return f"same page, {new} new item(s) appeared" if new else "no visible change"
        return self.redactor.text("; ".join(changes))
