"""Use-case entry points shared by the CLI, the agent-facing catalog and tests."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from cua.artifact.capability import Capability
from cua.artifact.contract import tool_definition
from cua.artifact.vocabulary import Vocabulary
from cua.discovery.agent import DiscoveryAgent, DiscoveryContext, DiscoveryOutcome
from cua.discovery.llm import LLMClient
from cua.discovery.request import DiscoveryRequest
from cua.replay.engine import ReplayEngine, ReplayOptions
from cua.runtime.live import LiveOptions, live_session
from cua.runtime.result import RunResult
from cua.workspace import Workspace


async def replay(
    workspace: Workspace,
    capability: Capability,
    *,
    tenant: str,
    inputs: dict[str, Any],
    options: ReplayOptions | None = None,
    live: LiveOptions | None = None,
) -> RunResult:
    binding = workspace.bind(capability, tenant)
    async with live_session(
        workspace,
        tenant=tenant,
        app=capability.metadata.app,
        kind="replay",
        vocabulary=binding.vocabulary,
        options=live or LiveOptions(),
    ) as session:
        engine = ReplayEngine(
            binding,
            session.surface,
            session.recorder,
            session.vault,
            channel=session.channel,
            desk=session.desk,
            options=options,
        )
        return await engine.run(inputs)


async def discover(
    workspace: Workspace,
    request: DiscoveryRequest,
    *,
    tenant: str,
    llm: LLMClient,
    live: LiveOptions | None = None,
    artifact_dir: Path | None = None,
    send_screenshots: bool = False,
) -> DiscoveryOutcome:
    tenant_obj = workspace.tenant(tenant)
    app = workspace.tenant_app(tenant_obj, request.spec.app)
    profile = workspace.profile(request.spec.app)
    async with live_session(
        workspace,
        tenant=tenant,
        app=request.spec.app,
        kind="discovery",
        vocabulary=Vocabulary(app.vocabulary),
        options=live or LiveOptions(),
    ) as session:
        context = DiscoveryContext(
            tenant=tenant,
            profile=profile,
            policy=session.policy,
            artifact_dir=artifact_dir or workspace.capabilities_dir / request.spec.app,
        )
        agent = DiscoveryAgent(
            request,
            context,
            session.surface,
            llm,
            session.recorder,
            session.vault,
            channel=session.channel,
            desk=session.desk,
            send_screenshots=send_screenshots,
        )
        return await agent.run()


def catalog_tools(workspace: Workspace, *, approved_only: bool = False) -> list[dict[str, Any]]:
    """The agent-facing catalog: one function-calling tool per (latest) capability."""
    latest: dict[str, Capability] = {}
    for entry in workspace.catalog():
        latest[entry.capability.metadata.name] = entry.capability
    return [tool_definition(c) for c in latest.values() if not approved_only or c.is_approved()]
