"""YAML (de)serialisation tuned for human review: stable key order, no nulls, block strings."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel

from cua.artifact.capability import Capability
from cua.artifact.profile import AppProfile
from cua.artifact.tenant import Tenant

_LEADING = ("apiVersion", "kind", "metadata", "spec", "strategy", "id", "name", "code", "action", "title")
_TRAILING = ("expect", "timeout_ms", "source", "rationale")


class _Dumper(yaml.SafeDumper):
    def increase_indent(self, flow: bool = False, indentless: bool = False) -> None:
        super().increase_indent(flow, False)  # indent sequences under their key, like kubectl


def _str_representer(dumper: yaml.SafeDumper, data: str) -> yaml.ScalarNode:
    style = "|" if "\n" in data else None
    return dumper.represent_scalar("tag:yaml.org,2002:str", data, style=style)


_Dumper.add_representer(str, _str_representer)


def _ordered(node: Any) -> Any:
    if isinstance(node, dict):
        lead = [k for k in _LEADING if k in node]
        trail = [k for k in _TRAILING if k in node]
        rest = [k for k in node if k not in lead and k not in trail]
        return {k: _ordered(node[k]) for k in (*lead, *rest, *trail)}
    if isinstance(node, list):
        return [_ordered(v) for v in node]
    return node


def to_yaml(model: BaseModel, header: str | None = None) -> str:
    data = _ordered(model.model_dump(mode="json", by_alias=True, exclude_none=True))
    body = yaml.dump(data, Dumper=_Dumper, sort_keys=False, allow_unicode=True, width=160)
    if header:
        body = "".join(f"# {line}\n" if line else "#\n" for line in header.splitlines()) + body
    return body


def save(model: BaseModel, path: Path, header: str | None = None) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(to_yaml(model, header), encoding="utf-8")
    return path


def load_yaml(path: Path) -> Any:
    with path.open(encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def load_as[M: BaseModel](model: type[M], path: Path) -> M:
    return model.model_validate(load_yaml(path))


def load_capability(path: Path) -> Capability:
    return load_as(Capability, path)


def load_profile(path: Path) -> AppProfile:
    return load_as(AppProfile, path)


def load_tenant(path: Path) -> Tenant:
    return load_as(Tenant, path)
