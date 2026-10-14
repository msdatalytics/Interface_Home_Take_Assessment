"""Who is driving the live session right now.

A single :class:`ControlChannel` per live session holds a *lease* with a monotonically
increasing **epoch** (a fencing token). Automation captures the epoch when it starts and
re-checks it before every action; any transfer of control bumps the epoch, so a stale
automation coroutine can never act on a session a human now owns (and vice versa: operator
input is only accepted from the operator holding the current human lease).
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import UTC, datetime
from enum import StrEnum


class Controller(StrEnum):
    AUTOMATION = "automation"
    HUMAN = "human"


@dataclass(frozen=True)
class Lease:
    holder: Controller
    epoch: int
    since: datetime
    reason: str
    operator: str | None = None
    intervention_id: str | None = None


class LeaseLostError(RuntimeError):
    """Raised when automation tries to act without holding the current lease."""


class ControlChannel:
    def __init__(self) -> None:
        self._lease = Lease(Controller.AUTOMATION, 1, datetime.now(UTC), "session start")
        self._history: list[Lease] = [self._lease]

    @property
    def lease(self) -> Lease:
        return self._lease

    @property
    def history(self) -> list[Lease]:
        return list(self._history)

    def assert_automation(self, epoch: int) -> None:
        lease = self._lease
        if lease.holder is not Controller.AUTOMATION or lease.epoch != epoch:
            raise LeaseLostError(
                f"automation epoch {epoch} is stale: session held by {lease.holder} (epoch {lease.epoch})"
            )

    def assert_operator(self, operator: str) -> None:
        lease = self._lease
        if lease.holder is not Controller.HUMAN or lease.operator != operator:
            raise LeaseLostError(f"operator '{operator}' does not hold the session (held by {lease.holder})")

    def to_human(self, operator: str, reason: str, intervention_id: str) -> Lease:
        if self._lease.holder is Controller.HUMAN and self._lease.operator != operator:
            raise LeaseLostError(f"session already held by operator '{self._lease.operator}'")
        return self._transfer(Controller.HUMAN, reason, operator, intervention_id)

    def to_automation(self, reason: str) -> Lease:
        return self._transfer(Controller.AUTOMATION, reason, None, None)

    def _transfer(self, holder: Controller, reason: str, operator: str | None, intervention_id: str | None) -> Lease:
        self._lease = replace(
            self._lease,
            holder=holder,
            epoch=self._lease.epoch + 1,
            since=datetime.now(UTC),
            reason=reason,
            operator=operator,
            intervention_id=intervention_id,
        )
        self._history.append(self._lease)
        return self._lease
