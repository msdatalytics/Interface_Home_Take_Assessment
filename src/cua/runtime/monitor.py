"""Classifies the live state against known exceptional states (app profile handlers) and
business outcomes (capability contract + app profile). Evaluated continuously while
waiting for targets and checkpoints, so an exceptional state is recognised as soon as it
appears instead of after a timeout."""

from __future__ import annotations

from dataclasses import dataclass

from cua.artifact.capability import OutcomeSpec
from cua.artifact.profile import AppProfile, HandlerKind, StateHandler
from cua.surface.base import Surface, SurfaceState

_HANDLER_PRIORITY = {
    HandlerKind.SESSION_EXPIRED: 0,
    HandlerKind.FATAL: 1,
    HandlerKind.TRANSIENT_ERROR: 2,
    HandlerKind.INTERSTITIAL: 3,
}


@dataclass(frozen=True)
class Detection:
    handler: StateHandler | None = None
    outcome: OutcomeSpec | None = None
    message: str | None = None
    declared: bool = True

    @property
    def label(self) -> str:
        if self.handler is not None:
            return f"{self.handler.kind}:{self.handler.id}"
        assert self.outcome is not None
        return f"outcome:{self.outcome.code}"


class StateMonitor:
    def __init__(self, surface: Surface, profile: AppProfile, declared_outcomes: list[OutcomeSpec]) -> None:
        self.surface = surface
        self.handlers = sorted(profile.spec.handlers, key=lambda h: _HANDLER_PRIORITY[h.kind])
        declared_codes = {o.code for o in declared_outcomes}
        self.outcomes: list[tuple[OutcomeSpec, bool]] = [(o, True) for o in declared_outcomes] + [
            (o, False) for o in profile.spec.outcomes if o.code not in declared_codes
        ]

    async def detect(self, state: SurfaceState, *, include_outcomes: bool = True) -> Detection | None:
        for handler in self.handlers:
            result = await self.surface.evaluate(handler.detect, state)
            if result.matched:
                return Detection(handler=handler, message=result.message)
        if include_outcomes:
            for outcome, declared in self.outcomes:
                result = await self.surface.evaluate(outcome.detect, state)
                if result.matched:
                    return Detection(outcome=outcome, message=result.message, declared=declared)
        return None
