"""Tenant configuration: one institution's installation of one or more vendor products.

Per-tenant variation is expressed as *data layered over* a shared capability rather than
as a forked recording:

1. ``vocabulary`` - canonical UI term -> tenant term (relabelled fields/buttons/columns).
   Applied to every locator and text condition at resolution time, so one entry fixes
   every capability that touches that label.
2. ``overrides`` - RFC 6902 JSON-Patch operations against a specific capability (and
   version range) for genuinely structural differences. Explicit, reviewable, minimal.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import Field

from cua.artifact.capability import Identifier
from cua.artifact.common import API_VERSION, Strict


class PatchOp(Strict):
    op: Literal["add", "remove", "replace"]
    path: str = Field(pattern=r"^(/[^/]*)+$")
    value: Any = None


class CapabilityOverride(Strict):
    capability: str
    versions: str = "*"
    reason: str
    patches: list[PatchOp] = Field(min_length=1)


class TenantApp(Strict):
    base_url: str
    expected_version: str | None = None
    vocabulary: dict[str, str] = Field(default_factory=dict)
    secrets: dict[str, str] = Field(
        default_factory=dict,
        description="Vault key used in artifacts -> environment variable holding the value.",
    )
    overrides: list[CapabilityOverride] = Field(default_factory=list)


class TenantMetadata(Strict):
    name: str = Field(pattern=r"^[a-z][a-z0-9-]{1,62}$")
    display_name: str


class TenantSpec(Strict):
    apps: dict[Identifier, TenantApp] = Field(description="Keyed by app profile name.")


class Tenant(Strict):
    api_version: Literal["cua.dev/v1"] = Field(default=API_VERSION, alias="apiVersion")
    kind: Literal["Tenant"] = "Tenant"
    metadata: TenantMetadata
    spec: TenantSpec
