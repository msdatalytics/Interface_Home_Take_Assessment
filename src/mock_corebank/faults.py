"""Deterministic fault injection for the mock core banking app.

Faults are queued through the ``/__admin/faults`` endpoint (which is deliberately *not*
on the automation allowlist) and consumed by subsequent content-page requests. Each
fault fires ``count`` times, optionally only on paths containing ``path_contains``.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from enum import StrEnum

from pydantic import BaseModel, Field


class FaultKind(StrEnum):
    MAINTENANCE_NOTICE = "maintenance_notice"
    """A known interstitial the operator must acknowledge before continuing."""
    SESSION_EXPIRED = "session_expired"
    """The session is invalidated and the login screen is rendered inside the frame."""
    SLOW = "slow"
    """The response is delayed by ``seconds``."""
    SERVER_ERROR = "server_error"
    """A transient ASP.NET-style "yellow screen of death" runtime error."""
    UNKNOWN_DIALOG = "unknown_dialog"
    """A blocking modal overlay that no app profile knows how to handle."""


class FaultSpec(BaseModel):
    kind: FaultKind
    count: int = Field(default=1, ge=1, le=100)
    path_contains: str | None = None
    seconds: float = Field(default=3.0, ge=0, le=60)


@dataclass
class _Armed:
    spec: FaultSpec
    remaining: int


class FaultInjector:
    """Thread-safe queue of armed faults."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._armed: list[_Armed] = []

    def arm(self, spec: FaultSpec) -> None:
        with self._lock:
            self._armed.append(_Armed(spec=spec, remaining=spec.count))

    def clear(self) -> None:
        with self._lock:
            self._armed.clear()

    def snapshot(self) -> list[dict[str, object]]:
        with self._lock:
            return [{**a.spec.model_dump(), "remaining": a.remaining} for a in self._armed]

    def take(self, path: str) -> FaultSpec | None:
        """Consume and return the first armed fault that applies to ``path``."""
        with self._lock:
            for armed in self._armed:
                if armed.spec.path_contains and armed.spec.path_contains not in path:
                    continue
                armed.remaining -= 1
                if armed.remaining <= 0:
                    self._armed.remove(armed)
                return armed.spec
        return None
