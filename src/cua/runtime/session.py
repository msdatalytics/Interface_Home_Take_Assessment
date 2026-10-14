"""Establishes and re-establishes an authenticated application session using the app
profile's login sub-flow. Credentials are resolved from the vault inside the step
executor; they never reach a capability, a log line, or the LLM."""

from __future__ import annotations

from cua.artifact.profile import AppProfile
from cua.evidence.recorder import RunRecorder
from cua.runtime.executor import StepContext, StepExecutor, StepFailedError
from cua.safety.secrets import SecretVault
from cua.surface.base import ActionError, Surface


class SessionError(RuntimeError):
    def __init__(self, message: str, *, unreachable: bool = False) -> None:
        super().__init__(message)
        self.unreachable = unreachable


class SessionManager:
    def __init__(
        self,
        profile: AppProfile,
        surface: Surface,
        executor: StepExecutor,
        vault: SecretVault,
        recorder: RunRecorder,
    ) -> None:
        self.profile = profile
        self.surface = surface
        self.executor = executor
        self.vault = vault
        self.recorder = recorder

    async def ensure(self) -> None:
        """Land on the app's entry page with a live session (signing on if needed)."""
        spec = self.profile.spec.session
        try:
            await self.surface.navigate(spec.entry_path)
        except ActionError as exc:
            raise SessionError(f"application unreachable: {exc}", unreachable=True) from exc
        await self.surface.wait_settled()
        state = await self.surface.state()
        if (await self.surface.evaluate(spec.logged_out_when, state)).matched:
            await self.login()
        else:
            await self._await_logged_in()
            self.recorder.event("session.reused")

    async def login(self) -> None:
        spec = self.profile.spec.session
        self.recorder.event("session.login", steps=len(spec.login_steps))
        ctx = StepContext(inputs={}, vault=self.vault)
        try:
            for step in spec.login_steps:
                await self.executor.execute(step, ctx)
        except StepFailedError as exc:
            raise SessionError(f"sign-on failed at '{exc.failure.step_id}': {exc.failure.message}") from exc
        await self._await_logged_in()
        self.recorder.event("session.established")

    async def _await_logged_in(self) -> None:
        spec = self.profile.spec.session
        try:
            await self.executor.await_condition(spec.logged_in_when, timeout_ms=10_000)
        except Exception as exc:
            state = await self.surface.state()
            raise SessionError(
                f"not signed on after login: expected {spec.logged_in_when.describe()}; observed {state.summary()}"
            ) from exc
