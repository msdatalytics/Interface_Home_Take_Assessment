"""The caller-facing contract of a capability: input validation, output parsing, and the
JSON-Schema tool definition an AI agent uses to discover and invoke it."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from cua.artifact.capability import Capability, InputSpec, OutputSpec
from cua.artifact.common import ValueType
from cua.textutil import collapse_ws


@dataclass(frozen=True)
class FieldError:
    field: str
    message: str


class ContractError(ValueError):
    def __init__(self, errors: list[FieldError]) -> None:
        self.errors = errors
        super().__init__("; ".join(f"{e.field}: {e.message}" for e in errors))


# ---------------------------------------------------------------------------- inputs


def validate_inputs(capability: Capability, raw: dict[str, Any]) -> dict[str, str]:
    """Validate and normalise caller-supplied inputs into the strings typed into the UI.

    Fails *before* touching the surface - a malformed request must never reach the app."""
    errors: list[FieldError] = []
    values: dict[str, str] = {}
    declared = {i.name for i in capability.spec.inputs}
    errors.extend(FieldError(name, "unknown input") for name in sorted(set(raw) - declared))
    for spec in capability.spec.inputs:
        if raw.get(spec.name) in (None, ""):
            if spec.required:
                errors.append(FieldError(spec.name, "is required"))
            continue
        try:
            values[spec.name] = _coerce_input(spec, raw[spec.name])
        except ValueError as exc:
            errors.append(FieldError(spec.name, str(exc)))
    if errors:
        raise ContractError(errors)
    return values


def _coerce_input(spec: InputSpec, value: Any) -> str:
    text = str(value).strip()
    match spec.type:
        case ValueType.INTEGER:
            if not re.fullmatch(r"-?\d+", text):
                raise ValueError("must be an integer")
        case ValueType.DECIMAL | ValueType.MONEY:
            try:
                number = Decimal(text.replace(",", "").removeprefix("$"))
            except InvalidOperation:
                raise ValueError("must be a decimal number") from None
            if not number.is_finite():
                raise ValueError("must be a finite number")
            text = f"{number:.2f}" if spec.type is ValueType.MONEY else str(number)
        case ValueType.DATE:
            text = _parse_date(text).isoformat()
        case ValueType.BOOLEAN:
            text = str(_parse_bool(text)).lower()
        case ValueType.ENUM:
            if text not in (spec.enum or []):
                raise ValueError(f"must be one of {spec.enum}")
        case ValueType.STRING:
            pass
    if spec.max_length is not None and len(text) > spec.max_length:
        raise ValueError(f"must be at most {spec.max_length} characters")
    if spec.pattern is not None and not re.fullmatch(spec.pattern, text):
        raise ValueError(f"must match /{spec.pattern}/")
    return text


# ---------------------------------------------------------------------------- outputs

_MONEY = re.compile(r"^\(?-?\s*\$?\s*-?[\d,]*\d(?:\.\d+)?\)?$")


def parse_output(spec: OutputSpec, raw: str) -> Any:
    """Parse text read from the surface into the declared JSON-safe type.

    Decimals are returned as strings to avoid binary floating point for money."""
    text = collapse_ws(raw)
    match spec.type:
        case ValueType.MONEY | ValueType.DECIMAL:
            if spec.type is ValueType.MONEY and not _MONEY.match(text):
                raise ValueError(f"not a money amount: {text!r}")
            negative = text.startswith("(") or "-" in text
            digits = re.sub(r"[^\d.]", "", text)
            try:
                number = Decimal(digits)
            except InvalidOperation:
                raise ValueError(f"not a number: {text!r}") from None
            return str(-number if negative else number)
        case ValueType.INTEGER:
            digits = re.sub(r"[,\s]", "", text)
            if not re.fullmatch(r"-?\d+", digits):
                raise ValueError(f"not an integer: {text!r}")
            return int(digits)
        case ValueType.DATE:
            return _parse_date(text).isoformat()
        case ValueType.BOOLEAN:
            return _parse_bool(text)
        case ValueType.STRING | ValueType.ENUM:
            if not text:
                raise ValueError("empty text")
            return text


def _parse_date(text: str) -> date:
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%m/%d/%y", "%d-%b-%Y", "%b %d, %Y"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    raise ValueError(f"not a recognised date: {text!r}")


def _parse_bool(text: str) -> bool:
    lowered = text.casefold()
    if lowered in {"true", "yes", "y", "1", "on"}:
        return True
    if lowered in {"false", "no", "n", "0", "off"}:
        return False
    raise ValueError(f"not a boolean: {text!r}")


# ---------------------------------------------------------------------------- tool schema

_JSON_TYPES: dict[ValueType, dict[str, Any]] = {
    ValueType.STRING: {"type": "string"},
    ValueType.INTEGER: {"type": "integer"},
    ValueType.DECIMAL: {"type": "string", "format": "decimal"},
    ValueType.MONEY: {"type": "string", "format": "decimal", "description": "USD amount, e.g. '25.00'"},
    ValueType.DATE: {"type": "string", "format": "date"},
    ValueType.BOOLEAN: {"type": "boolean"},
    ValueType.ENUM: {"type": "string"},
}


def _schema_for(type_: ValueType, description: str, **extra: Any) -> dict[str, Any]:
    schema = dict(_JSON_TYPES[type_])
    schema["description"] = (description + (" " + schema["description"] if "description" in schema else "")).strip()
    schema.update({k: v for k, v in extra.items() if v is not None})
    return schema


def tool_definition(capability: Capability) -> dict[str, Any]:
    """A provider-neutral function/tool definition (JSON Schema) for agent invocation."""
    properties = {
        i.name: _schema_for(i.type, i.description, pattern=i.pattern, enum=i.enum, maxLength=i.max_length)
        for i in capability.spec.inputs
    }
    outcome_codes = [o.code for o in capability.spec.outcomes]
    description = (
        f"{capability.metadata.title}. {capability.metadata.description.strip()} "
        f"Returns status 'succeeded' with outputs {[o.name for o in capability.spec.outputs]}, "
        f"or status 'business_outcome' with one of {outcome_codes}, or status 'failed'."
    )
    return {
        "name": capability.metadata.name.replace(".", "__"),
        "description": description,
        "parameters": {
            "type": "object",
            "properties": properties,
            "required": [i.name for i in capability.spec.inputs if i.required],
            "additionalProperties": False,
        },
        "x-capability": {
            "ref": capability.ref,
            "digest": capability.metadata.digest,
            "status": capability.metadata.status.value,
            "max_risk": capability.max_risk.value,
            "returns": {
                o.name: _schema_for(o.type, o.description) | {"x-sensitivity": o.sensitivity.value}
                for o in capability.spec.outputs
            },
            "business_outcomes": {o.code: o.description for o in capability.spec.outcomes},
        },
    }
