from __future__ import annotations

from pathlib import Path

import pytest

from cua.artifact.capability import Capability, InputSpec, OutputSpec
from cua.artifact.common import Sensitivity, ValueType
from cua.artifact.contract import ContractError, parse_output, tool_definition, validate_inputs
from cua.artifact.io import load_capability

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "get_savings_balance.yaml"


@pytest.fixture
def capability() -> Capability:
    return load_capability(FIXTURE)


def with_inputs(cap: Capability, *inputs: InputSpec) -> Capability:
    return cap.model_copy(update={"spec": cap.spec.model_copy(update={"inputs": [*cap.spec.inputs, *inputs]})})


def test_valid_inputs_are_normalised(capability: Capability) -> None:
    assert validate_inputs(capability, {"member_id": " 100234 "}) == {"member_id": "100234"}


@pytest.mark.parametrize(
    ("raw", "message"),
    [
        ({}, "member_id: is required"),
        ({"member_id": "12AB"}, "must match"),
        ({"member_id": "1", "extra": "x"}, "extra: unknown input"),
    ],
)
def test_invalid_inputs_never_reach_the_app(capability: Capability, raw: dict[str, str], message: str) -> None:
    with pytest.raises(ContractError, match=message):
        validate_inputs(capability, raw)


def test_typed_coercion(capability: Capability) -> None:
    cap = with_inputs(
        capability,
        InputSpec(name="amount", type=ValueType.MONEY, description="a", required=False),
        InputSpec(name="kind", type=ValueType.ENUM, description="k", enum=["A", "B"], required=False),
        InputSpec(name="nick", type=ValueType.STRING, description="n", max_length=3, required=False),
        InputSpec(name="when", type=ValueType.DATE, description="d", required=False),
    )
    values = validate_inputs(cap, {"member_id": "1", "amount": "$1,250.5", "kind": "B", "when": "03/04/2025"})
    assert values["amount"] == "1250.50"
    assert values["when"] == "2025-03-04"
    with pytest.raises(ContractError, match="one of"):
        validate_inputs(cap, {"member_id": "1", "kind": "C"})
    with pytest.raises(ContractError, match="at most 3"):
        validate_inputs(cap, {"member_id": "1", "nick": "toolong"})
    with pytest.raises(ContractError, match="decimal"):
        validate_inputs(cap, {"member_id": "1", "amount": "lots"})


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("$2,481.37", "2481.37"), (" $ 0.00 ", "0.00"), ("($12.50)", "-12.50"), ("-$3.10", "-3.10")],
)
def test_money_outputs_are_exact_decimal_strings(raw: str, expected: str) -> None:
    spec = OutputSpec(name="b", type=ValueType.MONEY, description="b")
    assert parse_output(spec, raw) == expected


@pytest.mark.parametrize("raw", ["Active", "", "$", "12 apples"])
def test_non_money_text_is_rejected(raw: str) -> None:
    with pytest.raises(ValueError, match=r"not a money amount|empty"):
        parse_output(OutputSpec(name="b", type=ValueType.MONEY, description="b"), raw)


def test_tool_definition_exposes_contract_not_mechanics(capability: Capability) -> None:
    tool = tool_definition(capability)
    assert tool["name"] == "corebank__member__get_savings_balance"
    params = tool["parameters"]
    assert params["required"] == ["member_id"]
    assert params["additionalProperties"] is False
    assert params["properties"]["member_id"]["pattern"] == "^[0-9]+$"
    meta = tool["x-capability"]
    assert meta["returns"]["savings_balance"]["x-sensitivity"] == Sensitivity.CONFIDENTIAL
    assert "record_not_found" in meta["business_outcomes"]
    assert "steps" not in str(tool)
    assert "locator" not in str(tool)
