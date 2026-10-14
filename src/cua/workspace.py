"""Loads configuration (policy, app profiles, tenants) and the capability catalog, and binds
a capability to a tenant (vocabulary + overrides + policy origin)."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from packaging.version import InvalidVersion, Version
from pydantic import ValidationError

from cua.artifact.capability import Capability
from cua.artifact.io import load_as, load_capability, load_profile, load_tenant
from cua.artifact.patch import PatchError, apply_patches
from cua.artifact.profile import AppProfile
from cua.artifact.tenant import Tenant, TenantApp
from cua.artifact.versions import satisfies
from cua.artifact.vocabulary import Vocabulary
from cua.safety.policy import Policy, PolicyDocument, PolicySpec


class WorkspaceError(LookupError):
    pass


@dataclass(frozen=True)
class Binding:
    """A capability specialised for one tenant's installation of its app."""

    capability: Capability
    base: Capability
    profile: AppProfile
    tenant: Tenant
    app: TenantApp
    vocabulary: Vocabulary
    policy: Policy
    applied_overrides: tuple[str, ...]

    @property
    def base_url(self) -> str:
        return self.app.base_url

    @property
    def approved(self) -> bool:
        """Approval is granted to the shared (base) capability digest. Tenant overrides are
        reviewed as tenant configuration, so they do not require re-approving the capability."""
        return self.base.is_approved()


@dataclass(frozen=True)
class CatalogEntry:
    path: Path
    capability: Capability


class Workspace:
    def __init__(self, root: Path, policy: PolicySpec, profiles: dict[str, AppProfile], tenants: dict[str, Tenant]):
        self.root = root
        self.policy_spec = policy
        self.profiles = profiles
        self.tenants = tenants

    @classmethod
    def load(cls, root: Path) -> Workspace:
        config = root / "config"
        if not config.is_dir():
            raise WorkspaceError(f"no config/ directory under {root}")
        policy = load_as(PolicyDocument, config / "policy.yaml").spec
        profiles = {p.metadata.name: p for p in map(load_profile, sorted((config / "apps").glob("*.yaml")))}
        tenants = {t.metadata.name: t for t in map(load_tenant, sorted((config / "tenants").glob("*.yaml")))}
        return cls(root, policy, profiles, tenants)

    @property
    def capabilities_dir(self) -> Path:
        return self.root / "capabilities"

    @property
    def runs_dir(self) -> Path:
        return self.root / "runs"

    def catalog(self) -> list[CatalogEntry]:
        entries = [CatalogEntry(p, load_capability(p)) for p in sorted(self.capabilities_dir.rglob("*.yaml"))]
        return sorted(entries, key=lambda e: (e.capability.metadata.name, _ver(e.capability.metadata.version)))

    def find(self, name: str, version: str | None = None) -> CatalogEntry:
        matches = [e for e in self.catalog() if e.capability.metadata.name == name.replace("__", ".")]
        if version:
            matches = [e for e in matches if e.capability.metadata.version == version]
        if not matches:
            raise WorkspaceError(f"capability '{name}'{'@' + version if version else ''} not found in catalog")
        return matches[-1]

    def tenant(self, name: str) -> Tenant:
        if name not in self.tenants:
            raise WorkspaceError(f"unknown tenant '{name}' (known: {sorted(self.tenants)})")
        return self.tenants[name]

    def profile(self, name: str) -> AppProfile:
        if name not in self.profiles:
            raise WorkspaceError(f"unknown app profile '{name}'")
        return self.profiles[name]

    def tenant_app(self, tenant: Tenant, profile_name: str) -> TenantApp:
        app = tenant.spec.apps.get(profile_name)
        if app is None:
            raise WorkspaceError(f"tenant '{tenant.metadata.name}' does not run app '{profile_name}'")
        return app

    def policy_for(self, app: TenantApp) -> Policy:
        parts = urlsplit(app.base_url)
        return Policy(self.policy_spec, f"{parts.scheme}://{parts.netloc}")

    def bind(self, capability: Capability, tenant_name: str) -> Binding:
        tenant = self.tenant(tenant_name)
        profile = self.profile(capability.metadata.app)
        app = self.tenant_app(tenant, profile.metadata.name)
        specialised = capability
        applied: list[str] = []
        overrides = [
            o
            for o in app.overrides
            if o.capability == capability.metadata.name and satisfies(capability.metadata.version, o.versions)
        ]
        if overrides:
            document = capability.model_dump(mode="json", by_alias=True, exclude_none=True)
            for override in overrides:
                try:
                    document = apply_patches(document, override.patches)
                except PatchError as exc:
                    raise WorkspaceError(f"tenant '{tenant_name}' override '{override.reason}': {exc}") from exc
                applied.append(override.reason)
            try:
                specialised = Capability.model_validate(document)
            except ValidationError as exc:
                raise WorkspaceError(f"tenant '{tenant_name}' override produces an invalid capability: {exc}") from exc
            if (specialised.spec.inputs, specialised.spec.outputs) != (capability.spec.inputs, capability.spec.outputs):
                raise WorkspaceError(
                    f"tenant '{tenant_name}' override changes the input/output contract of {capability.ref}; "
                    "overrides may only change *how* a capability runs, never its interface"
                )
        return Binding(
            capability=specialised,
            base=capability,
            profile=profile,
            tenant=tenant,
            app=app,
            vocabulary=Vocabulary(app.vocabulary),
            policy=self.policy_for(app),
            applied_overrides=tuple(applied),
        )


def _ver(v: str) -> Version:
    try:
        return Version(v)
    except InvalidVersion:
        return Version("0")
