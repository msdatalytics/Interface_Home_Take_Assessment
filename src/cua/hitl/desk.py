"""Intervention requests: detect -> route -> human takes the live session -> hand back.

The desk is the only component allowed to move the control lease. Automation calls
:meth:`InterventionDesk.request` and *awaits* it - the coroutine that was driving the
session is parked, so there is exactly one writer at any time. While the human holds the
lease, interactions on the live page are captured (redacted) and attached to the
intervention as the record of what the human did.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import secrets
import sys
import time
import urllib.request
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Protocol

from pydantic import BaseModel, Field

from cua.evidence.recorder import RunRecorder
from cua.hitl.control import ControlChannel, Controller, LeaseLostError
from cua.surface.base import Surface


class InterventionKind(StrEnum):
    STUCK = "stuck"  # discovery agent cannot make progress / asked for help
    APPROVAL = "approval"  # a risky/irreversible action needs a human decision
    FAILURE = "failure"  # replay hit a condition it cannot recover from


class InterventionStatus(StrEnum):
    OPEN = "open"
    CLAIMED = "claimed"
    RESOLVED = "resolved"
    EXPIRED = "expired"


class Resolution(StrEnum):
    RETRY_STEP = "retry_step"  # automation retries the step it was on (e.g. human cleared a blocker)
    STEP_DONE = "step_done"  # the human performed the step; automation verifies its checkpoint and moves on
    APPROVE = "approve"  # approval granted; automation performs the action
    REJECT = "reject"  # approval denied
    CONTINUE = "continue"  # discovery: hand control back to the agent with the operator's note
    ABORT = "abort"


class Intervention(BaseModel):
    id: str
    run_id: str
    kind: InterventionKind
    status: InterventionStatus = InterventionStatus.OPEN
    reason: str
    allowed: list[Resolution]
    context: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime
    operator: str | None = None
    claimed_at: datetime | None = None
    resolved_at: datetime | None = None
    resolution: Resolution | None = None
    note: str | None = None
    human_actions: list[dict[str, Any]] = Field(default_factory=list)


@dataclass(frozen=True)
class InterventionOutcome:
    intervention_id: str
    resolution: Resolution
    note: str | None
    operator: str | None
    human_actions: list[dict[str, Any]] = field(default_factory=list)
    waited_ms: int = 0
    timed_out: bool = False


class InterventionError(RuntimeError):
    pass


class Router(Protocol):
    async def notify(self, intervention: Intervention, url: str | None) -> None: ...


class ConsoleLogRouter:
    """Prints the request to stderr (the "pager" for local runs)."""

    async def notify(self, intervention: Intervention, url: str | None) -> None:
        where = f" -> {url}" if url else ""
        print(
            f"\n[intervention] {intervention.kind.upper()} {intervention.id}: {intervention.reason}{where}\n",
            file=sys.stderr,
            flush=True,
        )


class WebhookRouter:
    """POSTs a redacted summary to a chat/paging webhook (Slack, Teams, PagerDuty ...)."""

    def __init__(self, url: str) -> None:
        self.url = url

    async def notify(self, intervention: Intervention, url: str | None) -> None:
        body = json.dumps(
            {"id": intervention.id, "kind": intervention.kind, "reason": intervention.reason, "console_url": url}
        ).encode()
        if not self.url.startswith(("https://", "http://")):
            return
        request = urllib.request.Request(self.url, data=body, headers={"Content-Type": "application/json"})  # noqa: S310
        with contextlib.suppress(Exception):
            await asyncio.to_thread(urllib.request.urlopen, request, timeout=5)


class InterventionDesk:
    def __init__(
        self,
        *,
        recorder: RunRecorder,
        channel: ControlChannel,
        surface: Surface,
        routers: list[Router] | None = None,
        timeout_s: float = 900,
        console_url: str | None = None,
    ) -> None:
        self.recorder = recorder
        self.channel = channel
        self.surface = surface
        self.routers = routers if routers is not None else [ConsoleLogRouter()]
        self.timeout_s = timeout_s
        self.console_url = console_url
        self.interventions: dict[str, Intervention] = {}
        self._waiters: dict[str, asyncio.Future[InterventionOutcome]] = {}
        self._started: dict[str, float] = {}
        surface.human_event_listener = self._on_human_event

    # ------------------------------------------------------------------ automation side

    async def request(
        self,
        kind: InterventionKind,
        reason: str,
        *,
        context: dict[str, Any],
        allowed: list[Resolution],
    ) -> InterventionOutcome:
        iv_id = f"iv-{secrets.token_hex(4)}"
        evidence = await self._snapshot(iv_id)
        intervention = Intervention(
            id=iv_id,
            run_id=self.recorder.run_id,
            kind=kind,
            reason=self.recorder.redactor.text(reason),
            allowed=allowed,
            context=self.recorder.redactor.obj({**context, **evidence}),
            created_at=datetime.now(UTC),
        )
        self.interventions[iv_id] = intervention
        loop = asyncio.get_running_loop()
        future: asyncio.Future[InterventionOutcome] = loop.create_future()
        self._waiters[iv_id] = future
        self._started[iv_id] = time.monotonic()
        self._persist(intervention)
        url = f"{self.console_url}/interventions/{iv_id}" if self.console_url else None
        self.recorder.event(
            "intervention.requested",
            intervention=iv_id,
            kind=kind,
            reason=reason,
            allowed=[a.value for a in allowed],
            console_url=url,
            step=context.get("step_id"),
        )
        for router in self.routers:
            await router.notify(intervention, url)
        try:
            return await asyncio.wait_for(asyncio.shield(future), timeout=self.timeout_s)
        except TimeoutError:
            intervention.status = InterventionStatus.EXPIRED
            if self.channel.lease.holder is Controller.HUMAN:
                self.channel.to_automation(f"intervention {iv_id} expired")
            self._persist(intervention)
            self.recorder.event("intervention.expired", intervention=iv_id)
            return InterventionOutcome(
                iv_id,
                Resolution.ABORT,
                "no operator response",
                None,
                list(intervention.human_actions),
                self._waited(iv_id),
                timed_out=True,
            )
        finally:
            self._waiters.pop(iv_id, None)

    async def _snapshot(self, iv_id: str) -> dict[str, Any]:
        evidence: dict[str, Any] = {"location": self.surface.location}
        try:
            shot = await self.surface.screenshot(self.recorder.dir / "interventions" / f"{iv_id}.png")
            evidence["screenshot"] = self.recorder.relative(shot)
            observation = await self.surface.observe()
            evidence["observation"] = observation.render(self.recorder.redactor, self.surface.sensitive_label_patterns)
        except Exception as exc:  # evidence must never prevent escalation
            evidence["snapshot_error"] = str(exc)
        return evidence

    # ------------------------------------------------------------------ operator side

    def get(self, iv_id: str) -> Intervention:
        if iv_id not in self.interventions:
            raise InterventionError(f"unknown intervention {iv_id}")
        return self.interventions[iv_id]

    def open_interventions(self) -> list[Intervention]:
        return [
            i for i in self.interventions.values() if i.status in (InterventionStatus.OPEN, InterventionStatus.CLAIMED)
        ]

    def claim(self, iv_id: str, operator: str) -> Intervention:
        """Operator takes control of the live session (the same browser context automation used)."""
        iv = self.get(iv_id)
        if iv.status is InterventionStatus.CLAIMED and iv.operator == operator:
            return iv
        if iv.status is not InterventionStatus.OPEN:
            raise InterventionError(f"intervention {iv_id} is {iv.status}")
        try:
            lease = self.channel.to_human(operator, iv.reason, iv_id)
        except LeaseLostError as exc:
            raise InterventionError(str(exc)) from exc
        iv.status = InterventionStatus.CLAIMED
        iv.operator = operator
        iv.claimed_at = datetime.now(UTC)
        self._persist(iv)
        self.recorder.event("control.transferred", to="human", operator=operator, epoch=lease.epoch, intervention=iv_id)
        return iv

    def resolve(self, iv_id: str, operator: str, resolution: Resolution, note: str | None = None) -> Intervention:
        iv = self.get(iv_id)
        if iv.status not in (InterventionStatus.OPEN, InterventionStatus.CLAIMED):
            raise InterventionError(f"intervention {iv_id} is already {iv.status}")
        if resolution not in iv.allowed:
            raise InterventionError(
                f"resolution '{resolution}' not allowed here (allowed: {[a.value for a in iv.allowed]})"
            )
        if iv.status is InterventionStatus.CLAIMED and iv.operator != operator:
            raise InterventionError(f"intervention {iv_id} is claimed by '{iv.operator}'")
        if resolution is Resolution.STEP_DONE and iv.status is not InterventionStatus.CLAIMED:
            raise InterventionError("take control (claim) before declaring a step done manually")
        iv.status = InterventionStatus.RESOLVED
        iv.operator = iv.operator or operator
        iv.resolution = resolution
        iv.note = self.recorder.redactor.text(note) if note else None
        iv.resolved_at = datetime.now(UTC)
        if self.channel.lease.holder is Controller.HUMAN:
            lease = self.channel.to_automation(f"{resolution} by {operator}")
            self.recorder.event(
                "control.transferred", to="automation", operator=operator, epoch=lease.epoch, intervention=iv_id
            )
        self._persist(iv)
        self.recorder.event(
            "intervention.resolved",
            intervention=iv_id,
            operator=operator,
            resolution=resolution,
            note=iv.note,
            human_actions=len(iv.human_actions),
        )
        waiter = self._waiters.get(iv_id)
        if waiter is not None and not waiter.done():
            waiter.set_result(
                InterventionOutcome(
                    iv_id, resolution, iv.note, iv.operator, list(iv.human_actions), self._waited(iv_id)
                )
            )
        return iv

    def record_operator_input(self, operator: str, action: dict[str, Any]) -> None:
        """Console-originated input (in addition to in-page capture)."""
        self.channel.assert_operator(operator)
        self.recorder.event("human.input", operator=operator, **action)

    def _on_human_event(self, payload: dict[str, Any]) -> None:
        lease = self.channel.lease
        if lease.holder is not Controller.HUMAN or lease.intervention_id is None:
            return  # events caused by automation itself
        iv = self.interventions.get(lease.intervention_id)
        if iv is None:
            return
        action = self.recorder.redactor.obj({k: v for k, v in payload.items() if v not in (None, [], "")})
        labels = [*(payload.get("labels") or []), payload.get("valueLabel") or ""]
        patterns = self.surface.sensitive_label_patterns
        if payload.get("sensitive") or any(p.search(label) for p in patterns for label in labels if label):
            action["value"] = "[REDACTED]"
        action["at"] = datetime.now(UTC).isoformat(timespec="milliseconds")
        iv.human_actions.append(action)
        self._persist(iv)
        self.recorder.event("human.action", intervention=iv.id, operator=lease.operator, **action)

    # ------------------------------------------------------------------ helpers

    def _waited(self, iv_id: str) -> int:
        return int((time.monotonic() - self._started.get(iv_id, time.monotonic())) * 1000)

    def _persist(self, iv: Intervention) -> None:
        self.recorder.write_json(f"interventions/{iv.id}.json", iv.model_dump(mode="json"))
