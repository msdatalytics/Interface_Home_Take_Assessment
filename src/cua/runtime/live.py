"""Wires one live session: redactor, vault, evidence recorder, browser surface, control
channel, intervention desk and (optionally) the operator console."""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass

from cua.artifact.vocabulary import Vocabulary
from cua.evidence.recorder import RunRecorder
from cua.hitl.console import OperatorConsole, create_console_app
from cua.hitl.control import ControlChannel
from cua.hitl.desk import ConsoleLogRouter, InterventionDesk, Router, WebhookRouter
from cua.safety.policy import Policy
from cua.safety.redaction import Redactor
from cua.safety.secrets import EnvVault
from cua.surface.browser import BrowserSession, BrowserSurface
from cua.workspace import Workspace


@dataclass(frozen=True)
class LiveOptions:
    headless: bool = True
    console: bool = False
    console_port: int = 8765
    trace: bool = False
    slow_mo_ms: int = 0
    intervention_timeout_s: float = 900


@dataclass
class LiveSession:
    recorder: RunRecorder
    surface: BrowserSurface
    vault: EnvVault
    channel: ControlChannel
    desk: InterventionDesk
    policy: Policy
    console: OperatorConsole | None


@asynccontextmanager
async def live_session(
    workspace: Workspace,
    *,
    tenant: str,
    app: str,
    kind: str,
    vocabulary: Vocabulary,
    options: LiveOptions,
) -> AsyncIterator[LiveSession]:
    tenant_app = workspace.tenant_app(workspace.tenant(tenant), app)
    policy = workspace.policy_for(tenant_app)
    redactor = Redactor(policy.spec.redaction.patterns)
    recorder = RunRecorder(workspace.runs_dir, kind, redactor)
    vault = EnvVault(tenant_app.secrets, redactor)
    vault.preload()
    browser: BrowserSession | None = None
    console: OperatorConsole | None = None
    try:
        browser = await BrowserSession.start(
            base_url=tenant_app.base_url,
            policy=policy,
            redactor=redactor,
            vocabulary=vocabulary,
            events=recorder.emit,
            headless=options.headless,
            trace_path=recorder.dir / "trace.zip" if options.trace else None,
            slow_mo_ms=options.slow_mo_ms,
        )
        assert browser.surface is not None
        surface = browser.surface
        channel = ControlChannel()
        routers: list[Router] = [ConsoleLogRouter()]
        if webhook := os.environ.get("CUA_INTERVENTION_WEBHOOK"):
            routers.append(WebhookRouter(webhook))
        desk = InterventionDesk(
            recorder=recorder,
            channel=channel,
            surface=surface,
            routers=routers,
            timeout_s=options.intervention_timeout_s,
        )
        if options.console:
            console = OperatorConsole(create_console_app(desk, channel, surface), port=options.console_port)
            await console.start()
            desk.console_url = console.url
            recorder.event("console.started", url=console.url)
        yield LiveSession(recorder, surface, vault, channel, desk, policy, console)
    finally:
        if console is not None:
            await console.stop()
        if browser is not None:
            await browser.close()
        recorder.close()
