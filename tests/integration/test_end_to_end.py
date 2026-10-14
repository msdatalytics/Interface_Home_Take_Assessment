"""End-to-end: scripted discovery -> deterministic replay, against the hostile mock core.

The LLM is replaced by reference plans (``mock_corebank.plans``); everything else - browser,
observation, locator synthesis/verification, policy, recording, replay, recovery, escalation
and the operator console - is the production code path.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from cua.artifact.capability import Capability
from cua.artifact.io import load_as
from cua.artifact.targets import CssLocator, LabelLocator, RoleLocator, TableCellLocator
from cua.discovery.llm import ScreenRef, ScriptedClient, ToolCall
from cua.discovery.request import DiscoveryRequest
from cua.replay.engine import ReplayOptions
from cua.runtime.live import LiveOptions
from cua.runtime.result import FailureCode, RunResult, RunStatus
from cua.service import discover, replay
from cua.workspace import Workspace
from mock_corebank.plans import PLANS
from tests.conftest import REPO, MockCore, free_port, make_workspace
from tests.integration.helpers import Operator, approved, events, evidence_text

pytestmark = pytest.mark.browser

BALANCE = "corebank.member.get_savings_balance"
OPEN_SHARE = "corebank.member.open_share_account"
PII = ("Jordan", "Rivera", "123-45-6789", "1984-", "Morgan T. Ellis", "1976-05-30")
SHARE_INPUTS = {"member_id": "100591", "share_type": "Holiday Club", "nickname": "Ski Trip", "initial_deposit": "75"}


def request(name: str) -> DiscoveryRequest:
    file = {BALANCE: "get_savings_balance.yaml", OPEN_SHARE: "open_share_account.yaml"}[name]
    return load_as(DiscoveryRequest, REPO / "requests" / file)


@pytest.fixture(scope="session")
async def ws(mock_core: MockCore, tmp_path_factory: pytest.TempPathFactory) -> Workspace:
    """A workspace whose capabilities were discovered (scripted) against the live mock."""
    workspace = Workspace.load(make_workspace(tmp_path_factory.mktemp("e2e") / "ws", mock_core.urls))
    outcome = await discover(workspace, request(BALANCE), tenant="cu-alpha", llm=ScriptedClient(PLANS[BALANCE]))
    assert outcome.status == "completed", outcome.reason

    port = free_port()
    operator = Operator(port, resolution="approve", kind="approval")
    discovery = discover(
        workspace,
        request(OPEN_SHARE),
        tenant="cu-alpha",
        llm=ScriptedClient(PLANS[OPEN_SHARE]),
        live=LiveOptions(console=True, console_port=port),
    )
    outcome, _ = await asyncio.gather(discovery, operator.run())
    assert outcome.status == "completed", outcome.reason
    assert len(operator.handled) == 1, "the irreversible Submit must have been routed for approval"
    return workspace


@pytest.fixture(autouse=True)
def _fresh_core(mock_core: MockCore) -> None:
    mock_core.reset()


def cap(ws: Workspace, name: str = BALANCE) -> Capability:
    return ws.find(name).capability


async def run(ws: Workspace, capability: Capability, tenant: str = "cu-alpha", **kwargs: Any) -> RunResult:
    return await replay(ws, capability, tenant=tenant, **kwargs)


# ------------------------------------------------------------------------------ discovery


def test_discovered_artifact_is_robust_and_clean(ws: Workspace) -> None:
    entry = ws.find(BALANCE)
    capability = entry.capability
    steps = {s.id: s for s in capability.spec.steps}
    fill = steps["fill_textbox_member_number"]
    assert isinstance(fill.target.locators[0], LabelLocator)  # type: ignore[union-attr]
    assert isinstance(fill.target.locators[-1], CssLocator)  # type: ignore[union-attr]
    extract = steps["extract_savings_balance"]
    cell = extract.target.locators[0]  # type: ignore[union-attr]
    assert isinstance(cell, TableCellLocator)
    assert (cell.row, cell.column) == ("Primary Savings", "Balance")
    assert all(s.expect is not None for s in capability.spec.steps if s.action == "click")
    assert capability.metadata.digest == capability.compute_digest()
    text = entry.path.read_text()
    assert "100234" not in text
    assert not any(p in text for p in PII)


def test_irreversible_step_is_classified(ws: Workspace) -> None:
    submit = next(s for s in cap(ws, OPEN_SHARE).spec.steps if s.id == "click_button_submit")
    assert submit.risk == "irreversible"
    assert isinstance(submit.target.locators[0], RoleLocator)  # type: ignore[union-attr]


def test_discovery_evidence_contains_no_pii(ws: Workspace) -> None:
    for run_dir in (ws.runs_dir).glob("*-discovery-*"):
        text = evidence_text(run_dir)
        assert not any(p in text for p in PII), run_dir
        assert list((run_dir / "llm").glob("turn-*.json")), "every model turn is logged"


# ------------------------------------------------------------------------------ replay: happy path


async def test_replay_returns_typed_outputs(ws: Workspace) -> None:
    result = await run(ws, cap(ws), inputs={"member_id": "100234"})
    assert result.status is RunStatus.SUCCEEDED, result.failure
    assert result.outputs == {"savings_balance": "2481.37"}
    assert not result.drift
    assert not result.recoveries
    persisted = Path(result.evidence_dir or "") / "result.json"
    assert "2481.37" not in persisted.read_text(), "confidential outputs are fingerprinted at rest"


async def test_cross_tenant_reuse_with_vocabulary_and_override(ws: Workspace) -> None:
    result = await run(ws, cap(ws), tenant="cu-beta", inputs={"member_id": "200145"})
    assert result.status is RunStatus.SUCCEEDED, result.failure
    assert result.outputs == {"savings_balance": "932.10"}
    assert result.tenant_overrides
    assert any(d.kind == "app_version" for d in result.drift) is False


# ------------------------------------------------------------------------------ business outcomes


@pytest.mark.parametrize(
    ("member", "code", "message"),
    [
        ("999999", "record_not_found", None),
        ("123", "validation_error", "Member Number must be 6 to 10 digits."),
        ("100777", "access_denied", None),
    ],
)
async def test_business_outcomes(ws: Workspace, member: str, code: str, message: str | None) -> None:
    result = await run(ws, cap(ws), inputs={"member_id": member})
    assert result.status is RunStatus.BUSINESS_OUTCOME
    assert result.outcome is not None
    assert result.outcome.code == code
    if message:
        assert result.outcome.message == message


async def test_invalid_input_is_rejected_before_the_app(ws: Workspace) -> None:
    result = await run(ws, cap(ws), inputs={"member_id": "12AB"})
    assert result.status is RunStatus.FAILED
    assert result.failure is not None
    assert result.failure.code is FailureCode.INPUT_INVALID
    assert not result.steps


# ------------------------------------------------------------------------------ recoverable conditions


@pytest.mark.parametrize(
    ("fault", "handler"),
    [
        ("maintenance_notice", "maintenance_notice"),
        ("server_error", "server_runtime_error"),
        ("session_expired", "session_expired"),
    ],
)
async def test_recoverable_conditions(ws: Workspace, mock_core: MockCore, fault: str, handler: str) -> None:
    mock_core.fault("cu-alpha", fault, path="mbr_detail")
    result = await run(ws, cap(ws), inputs={"member_id": "100234"})
    assert result.status is RunStatus.SUCCEEDED, result.failure
    assert result.outputs == {"savings_balance": "2481.37"}
    assert [r.handler for r in result.recoveries] == [handler]


async def test_slow_application_is_waited_for(ws: Workspace, mock_core: MockCore) -> None:
    mock_core.fault("cu-alpha", "slow", path="mbr_search", seconds=3)
    result = await run(ws, cap(ws), inputs={"member_id": "100234"})
    assert result.status is RunStatus.SUCCEEDED, result.failure


async def test_locator_drift_falls_back_and_is_reported(ws: Workspace) -> None:
    original = cap(ws)
    steps = list(original.spec.steps)
    index = next(i for i, s in enumerate(steps) if s.id == "click_button_search")
    target = steps[index].target  # type: ignore[union-attr]
    renamed = [RoleLocator(role="button", name="Find"), *target.locators[1:]]
    steps[index] = steps[index].model_copy(update={"target": target.model_copy(update={"locators": renamed})})
    drifted = original.model_copy(update={"spec": original.spec.model_copy(update={"steps": steps})})
    result = await run(ws, drifted, inputs={"member_id": "100234"})
    assert result.status is RunStatus.SUCCEEDED, result.failure
    assert [(d.kind, d.step_id) for d in result.drift] == [("locator_fallback", "click_button_search")]


# ------------------------------------------------------------------------------ hard failures


async def test_persistent_error_fails_with_masked_evidence(ws: Workspace, mock_core: MockCore) -> None:
    mock_core.fault("cu-alpha", "server_error", path="mbr_detail", count=10)
    result = await run(ws, cap(ws), inputs={"member_id": "100234"})
    assert result.status is RunStatus.FAILED
    failure = result.failure
    assert failure is not None
    assert failure.code is FailureCode.APP_ERROR
    assert failure.retryable
    run_dir = Path(result.evidence_dir or "")
    assert (run_dir / str(failure.evidence["screenshot"])).exists()
    assert all((run_dir / p).exists() for p in failure.evidence["dom"])


async def test_unexpected_modal_fails_fast_with_reason(ws: Workspace, mock_core: MockCore) -> None:
    mock_core.fault("cu-alpha", "unknown_dialog", path="mbr_search")
    result = await run(ws, cap(ws), inputs={"member_id": "100234"})
    assert result.status is RunStatus.FAILED
    assert result.failure is not None
    assert "covered by another element" in result.failure.message


async def test_failure_evidence_masks_label_sensitive_values(ws: Workspace) -> None:
    """Beta without its override stops on the member page; evidence must not leak name/DOB."""
    base = ws.find(BALANCE)
    stripped = Workspace.load(ws.root)
    stripped.tenants["cu-beta"] = stripped.tenants["cu-beta"].model_copy(
        update={
            "spec": stripped.tenants["cu-beta"].spec.model_copy(
                update={
                    "apps": {
                        k: v.model_copy(update={"overrides": []})
                        for k, v in stripped.tenants["cu-beta"].spec.apps.items()
                    }
                }
            )
        }
    )
    result = await replay(stripped, base.capability, tenant="cu-beta", inputs={"member_id": "200145"})
    assert result.status is RunStatus.FAILED
    assert result.failure is not None
    assert result.failure.code is FailureCode.CHECKPOINT_FAILED
    text = evidence_text(Path(result.evidence_dir or ""))
    assert "Morgan T. Ellis" not in text
    assert "1976-05-30" not in text
    assert "200145" not in text


# ------------------------------------------------------------------------------ irreversible actions


async def test_unapproved_irreversible_capability_never_touches_the_app(ws: Workspace, mock_core: MockCore) -> None:
    result = await run(ws, cap(ws, OPEN_SHARE), inputs=SHARE_INPUTS)
    assert result.failure is not None
    assert result.failure.code is FailureCode.APPROVAL_REQUIRED
    assert not result.steps
    assert mock_core.audit("cu-alpha") == []


async def test_just_in_time_approval(ws: Workspace, mock_core: MockCore) -> None:
    port = free_port()
    operator = Operator(port, resolution="approve", kind="approval")
    result, _ = await asyncio.gather(
        run(
            ws,
            cap(ws, OPEN_SHARE),
            inputs=SHARE_INPUTS,
            options=ReplayOptions(on_failure="escalate"),
            live=LiveOptions(console=True, console_port=port),
        ),
        operator.run(),
    )
    assert result.status is RunStatus.SUCCEEDED, result.failure
    assert [i.resolution for i in result.interventions] == ["approve"]
    assert len(mock_core.audit("cu-alpha")) == 1


async def test_approved_capability_runs_unattended_and_tampering_revokes_it(ws: Workspace, mock_core: MockCore) -> None:
    capability = approved(cap(ws, OPEN_SHARE))
    result = await run(ws, capability, inputs=SHARE_INPUTS)
    assert result.status is RunStatus.SUCCEEDED, result.failure
    assert str(result.outputs["confirmation_number"]).startswith("CB-")

    steps = list(capability.spec.steps)
    steps[0] = steps[0].model_copy(update={"timeout_ms": 9999})
    tampered = capability.model_copy(update={"spec": capability.spec.model_copy(update={"steps": steps})})
    result = await run(ws, tampered, inputs=SHARE_INPUTS)
    assert result.failure is not None
    assert result.failure.code is FailureCode.APPROVAL_REQUIRED
    assert "modified after it was approved" in result.failure.message
    assert len(mock_core.audit("cu-alpha")) == 1


async def test_error_after_commit_is_outcome_unknown_not_retried(ws: Workspace, mock_core: MockCore) -> None:
    mock_core.fault("cu-alpha", "server_error", path="subacct_submit")
    result = await run(ws, approved(cap(ws, OPEN_SHARE)), inputs=SHARE_INPUTS)
    assert result.failure is not None
    assert result.failure.code is FailureCode.OUTCOME_UNKNOWN
    assert result.failure.retryable is False


# ------------------------------------------------------------------------------ human handoff


async def test_operator_takes_over_the_live_session_and_hands_back(ws: Workspace, mock_core: MockCore) -> None:
    mock_core.fault("cu-alpha", "unknown_dialog", path="mbr_search")
    port = free_port()
    operator = Operator(port, resolution="retry_step", kind="failure", clicks=('button "Continue Session"',))
    result, _ = await asyncio.gather(
        run(
            ws,
            cap(ws),
            inputs={"member_id": "100234"},
            options=ReplayOptions(on_failure="escalate"),
            live=LiveOptions(console=True, console_port=port),
        ),
        operator.run(),
    )
    assert result.status is RunStatus.SUCCEEDED, result.failure
    assert result.human_assisted
    (intervention,) = result.interventions
    assert intervention.resolution == "retry_step"
    assert intervention.human_actions == 1
    kinds = [e["type"] for e in events(Path(result.evidence_dir or ""))]
    handoff = kinds.index("control.transferred")
    assert kinds[handoff:].count("control.transferred") == 2  # to human, then back to automation


async def test_stuck_discovery_is_rescued_by_a_human(mock_core: MockCore, tmp_path: Path) -> None:
    workspace = Workspace.load(make_workspace(tmp_path / "ws", mock_core.urls))
    plan = [
        ToolCall("request_human", {"reason": "cannot find the member search screen"}),
        *PLANS[BALANCE][1:],
    ]
    port = free_port()
    operator = Operator(port, resolution="continue", kind="stuck", clicks=('link "Member Inquiry"',))
    outcome, _ = await asyncio.gather(
        discover(
            workspace,
            request(BALANCE),
            tenant="cu-alpha",
            llm=ScriptedClient(plan),
            live=LiveOptions(console=True, console_port=port),
        ),
        operator.run(),
    )
    assert outcome.status == "completed", outcome.reason
    assert outcome.capability is not None
    first = outcome.capability.spec.steps[0]
    assert first.source == "human"
    assert "Member Inquiry" in first.title
    assert outcome.capability.metadata.provenance.human_assisted


async def test_dead_end_actions_are_not_recorded(mock_core: MockCore, tmp_path: Path) -> None:
    """Search before filling the field -> app validation message -> the agent recovers; the
    artifact holds only the happy path and the replay works."""
    workspace = Workspace.load(make_workspace(tmp_path / "ws", mock_core.urls))
    search = PLANS[BALANCE][2]
    plan = [PLANS[BALANCE][0], search, PLANS[BALANCE][1], search, *PLANS[BALANCE][3:]]
    llm = ScriptedClient(plan)
    outcome = await discover(workspace, request(BALANCE), tenant="cu-alpha", llm=llm)
    assert outcome.status == "completed", outcome.reason
    assert outcome.capability is not None
    assert [s.id for s in outcome.capability.spec.steps] == [
        "click_link_member_inquiry",
        "fill_textbox_member_number",
        "click_button_search",
        "click_link_view",
        "extract_savings_balance",
    ]
    assert "currently shows 'validation_error'" in llm.prompts[2]
    assert "currently shows" not in llm.prompts[4], "app messages are shown only while they are on screen"
    assert "NOT recorded" in llm.prompts[4]
    result = await replay(workspace, outcome.capability, tenant="cu-alpha", inputs={"member_id": "100234"})
    assert result.status is RunStatus.SUCCEEDED, result.failure


async def test_policy_blocks_denied_navigation(mock_core: MockCore, tmp_path: Path) -> None:
    workspace = Workspace.load(make_workspace(tmp_path / "ws", mock_core.urls))
    plan = [
        ToolCall("click", {"ref": ScreenRef(r'link "Sign Off"'), "reason": "try to leave"}),
        ToolCall("give_up", {"reason": "done"}),
    ]
    outcome = await discover(workspace, request(BALANCE), tenant="cu-alpha", llm=ScriptedClient(plan))
    assert outcome.status == "gave_up"
    run_dir = workspace.runs_dir / outcome.run_id
    blocked = [e for e in events(run_dir) if e["type"] == "policy.blocked_request"]
    assert blocked
    assert "logoff.asp" in blocked[0]["url"]
