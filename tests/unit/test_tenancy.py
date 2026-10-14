from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

import pytest
import yaml

from cua.artifact.patch import PatchError, apply_patches
from cua.artifact.tenant import PatchOp
from cua.artifact.vocabulary import Vocabulary
from cua.workspace import Workspace, WorkspaceError
from tests.conftest import make_workspace

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "get_savings_balance.yaml"

DOC: dict[str, Any] = {"spec": {"steps": [{"id": "a", "n": 1}, {"id": "b", "n": 2}, {"id": "c", "n": 3}]}}


def ops(*items: dict[str, Any]) -> list[PatchOp]:
    return [PatchOp.model_validate(i) for i in items]


def test_patch_by_step_id_and_index() -> None:
    out = apply_patches(
        DOC,
        ops(
            {"op": "replace", "path": "/spec/steps/@b/n", "value": 20},
            {"op": "remove", "path": "/spec/steps/@a"},
            {"op": "add", "path": "/spec/steps/@c", "value": {"id": "b2"}},
            {"op": "add", "path": "/spec/steps/-", "value": {"id": "z"}},
        ),
    )
    assert [s["id"] for s in out["spec"]["steps"]] == ["b", "b2", "c", "z"]
    assert out["spec"]["steps"][0]["n"] == 20
    assert DOC["spec"]["steps"][0] == {"id": "a", "n": 1}, "input is not mutated"


@pytest.mark.parametrize(
    "op",
    [
        {"op": "remove", "path": "/spec/steps/@missing"},
        {"op": "replace", "path": "/spec/nope", "value": 1},
        {"op": "replace", "path": "/spec/steps/9", "value": 1},
    ],
)
def test_patch_errors_are_explicit(op: dict[str, Any]) -> None:
    with pytest.raises(PatchError):
        apply_patches(DOC, ops(op))


def test_vocabulary_normalises_labels() -> None:
    vocab = Vocabulary({"Member Number": "Account Number"})
    assert vocab.translate("Member Number:") == "Account Number"
    assert vocab.translate("  member   number ") == "Account Number"
    assert vocab.translate("Status") == "Status"


@pytest.fixture
def workspace(tmp_path: Path) -> Workspace:
    root = make_workspace(tmp_path / "ws")
    target = root / "capabilities" / "meridian_corebank"
    target.mkdir(parents=True)
    shutil.copy(FIXTURE, target / FIXTURE.name)
    return Workspace.load(root)


def test_binding_applies_tenant_overrides(workspace: Workspace) -> None:
    cap = workspace.find("corebank.member.get_savings_balance").capability
    alpha = workspace.bind(cap, "cu-alpha")
    beta = workspace.bind(cap, "cu-beta")
    assert alpha.capability == cap
    assert not alpha.applied_overrides
    assert [s.id for s in beta.capability.spec.steps] == [s.id for s in cap.spec.steps if s.id != "click_link_view"]
    assert beta.applied_overrides
    assert beta.vocabulary.translate("Member Number:") == "Account Number"


def test_override_cannot_change_the_contract(workspace: Workspace, tmp_path: Path) -> None:
    tenant_file = workspace.root / "config" / "tenants" / "cu-beta.yaml"
    doc = yaml.safe_load(tenant_file.read_text())
    doc["spec"]["apps"]["meridian_corebank"]["overrides"][0]["patches"].append(
        {"op": "replace", "path": "/spec/outputs/0/sensitivity", "value": "public"}
    )
    tenant_file.write_text(yaml.safe_dump(doc, sort_keys=False))
    ws = Workspace.load(workspace.root)
    with pytest.raises(WorkspaceError, match="contract"):
        ws.bind(ws.find("corebank.member.get_savings_balance").capability, "cu-beta")


def test_policy_is_bound_to_each_tenant_origin(workspace: Workspace) -> None:
    alpha = workspace.policy_for(workspace.tenant_app(workspace.tenant("cu-alpha"), "meridian_corebank"))
    beta_url = workspace.tenant_app(workspace.tenant("cu-beta"), "meridian_corebank").base_url
    assert not alpha.check_url(beta_url + "/cb/main.asp").allowed
