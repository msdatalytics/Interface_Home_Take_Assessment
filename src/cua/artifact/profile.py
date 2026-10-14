"""App profile: vendor-product knowledge shared by every capability and every tenant.

Everything that is true of "Meridian CoreBank" regardless of which credit union runs it
lives here once - how to sign on, what a session-expiry looks like, which interstitials
can be safely dismissed, what a transient server error looks like, and which on-screen
messages are business outcomes. Capabilities stay focused on their flow; error handling
improvements land in one place and benefit every capability and tenant.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Literal

from pydantic import Field

from cua.artifact.capability import Identifier, OutcomeSpec, SemVer
from cua.artifact.common import API_VERSION, Strict
from cua.artifact.conditions import Condition
from cua.artifact.steps import Step
from cua.artifact.targets import FrameRef


class HandlerKind(StrEnum):
    INTERSTITIAL = "interstitial"
    """A known, safe-to-dismiss screen (notice, banner). Dismiss with ``steps`` and continue."""
    SESSION_EXPIRED = "session_expired"
    """Re-authenticate via the profile login flow, then restart the capability (if safe)."""
    TRANSIENT_ERROR = "transient_error"
    """Retry by reloading the frame (only for read steps), bounded by ``max_attempts``."""
    FATAL = "fatal"
    """Known hard failure: stop immediately with a clear message."""


class StateHandler(Strict):
    id: Identifier
    kind: HandlerKind
    description: str
    detect: Condition
    steps: list[Step] = Field(default_factory=list, description="Recovery actions (interstitials).")
    reload_frame: FrameRef | None = Field(default=None, description="Frame to reload (transient errors).")
    max_attempts: int = Field(default=2, ge=1, le=5)
    backoff_ms: int = Field(default=750, ge=0, le=30_000)


class SessionSpec(Strict):
    entry_path: str
    logged_out_when: Condition
    logged_in_when: Condition
    login_steps: list[Step] = Field(min_length=1)


class VersionProbe(Strict):
    frame: FrameRef | None = None
    pattern: str = Field(description="Regex with a named group 'version'.")


class AppProfileMetadata(Strict):
    name: Identifier
    version: SemVer
    vendor: str
    product: str
    description: str | None = None


class AppProfileSpec(Strict):
    session: SessionSpec
    handlers: list[StateHandler] = Field(default_factory=list)
    outcomes: list[OutcomeSpec] = Field(default_factory=list)
    version_probe: VersionProbe | None = None


class AppProfile(Strict):
    api_version: Literal["cua.dev/v1"] = Field(default=API_VERSION, alias="apiVersion")
    kind: Literal["AppProfile"] = "AppProfile"
    metadata: AppProfileMetadata
    spec: AppProfileSpec

    def outcome(self, code: str) -> OutcomeSpec | None:
        return next((o for o in self.spec.outcomes if o.code == code), None)
