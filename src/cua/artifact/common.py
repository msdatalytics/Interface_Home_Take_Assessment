"""Shared enums and the base model configuration for every artifact document."""

from __future__ import annotations

from enum import StrEnum
from typing import Final, Literal

from pydantic import BaseModel, ConfigDict

API_VERSION: Final[Literal["cua.dev/v1"]] = "cua.dev/v1"


class Strict(BaseModel):
    """Base for artifact documents: unknown keys are errors, instances are immutable."""

    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True)


class Risk(StrEnum):
    """Side-effect class of a step. Ordered: ``read < write < irreversible``.

    * ``read``         - no server-side state change (navigation, search, typing into a form).
    * ``write``        - changes state but is reversible / not yet committed (e.g. "Continue" to a review page).
    * ``irreversible`` - commits a business transaction (submit, post, transfer, close, delete).
    """

    READ = "read"
    WRITE = "write"
    IRREVERSIBLE = "irreversible"

    @property
    def rank(self) -> int:
        return _RISK_RANK[self]

    @classmethod
    def highest(cls, *risks: Risk) -> Risk:
        return max(risks, key=lambda r: r.rank, default=cls.READ)


_RISK_RANK = {Risk.READ: 0, Risk.WRITE: 1, Risk.IRREVERSIBLE: 2}


class Sensitivity(StrEnum):
    """Data classification driving redaction in logs, evidence and LLM prompts."""

    PUBLIC = "public"
    INTERNAL = "internal"
    CONFIDENTIAL = "confidential"  # e.g. balances: fingerprinted, never persisted in clear
    PII = "pii"  # e.g. member numbers, names: masked in logs and LLM observations
    SECRET = "secret"  # noqa: S105 - a classification label, not a credential: never leave the vault / surface boundary

    @property
    def persist_in_clear(self) -> bool:
        return self in (Sensitivity.PUBLIC, Sensitivity.INTERNAL)


class ValueType(StrEnum):
    STRING = "string"
    INTEGER = "integer"
    DECIMAL = "decimal"
    MONEY = "money"
    DATE = "date"
    BOOLEAN = "boolean"
    ENUM = "enum"


class Status(StrEnum):
    """Artifact lifecycle. Only ``approved`` artifacts may run irreversible steps unattended."""

    DRAFT = "draft"
    APPROVED = "approved"
    DEPRECATED = "deprecated"
