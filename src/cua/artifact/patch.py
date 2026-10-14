"""Minimal RFC 6902 JSON-Patch (add/remove/replace) for tenant overrides.

Deliberately tiny: overrides should be small, explicit and reviewable. ``move``/``copy``/
``test`` are not supported because they make overrides harder to reason about.

One extension: a list token ``@<id>`` addresses the element whose ``id`` is ``<id>``
(e.g. ``/spec/steps/@click_link_view``), so overrides survive unrelated step insertions
instead of silently patching whatever now sits at a numeric index. For ``add`` it inserts
*before* that element."""

from __future__ import annotations

import copy
from typing import Any

from cua.artifact.tenant import PatchOp


class PatchError(ValueError):
    pass


def _tokens(path: str) -> list[str]:
    return [t.replace("~1", "/").replace("~0", "~") for t in path.lstrip("/").split("/")]


def _index(items: list[Any], token: str, path: str) -> int:
    if token.startswith("@"):
        for i, item in enumerate(items):
            if isinstance(item, dict) and item.get("id") == token[1:]:
                return i
        raise PatchError(f"{path}: no element with id {token[1:]!r}")
    try:
        return int(token)
    except ValueError:
        raise PatchError(f"{path}: {token!r} is not a list index or @id") from None


def _child(container: Any, token: str, path: str) -> Any:
    if isinstance(container, list):
        index = _index(container, token, path)
        if not 0 <= index < len(container):
            raise PatchError(f"{path}: no list index {token!r}")
        return container[index]
    if isinstance(container, dict):
        if token not in container:
            raise PatchError(f"{path}: no key {token!r}")
        return container[token]
    raise PatchError(f"{path}: cannot descend into {type(container).__name__}")


def apply_patches(document: dict[str, Any], ops: list[PatchOp]) -> dict[str, Any]:
    doc = copy.deepcopy(document)
    for op in ops:
        *parents, last = _tokens(op.path)
        parent: Any = doc
        for token in parents:
            parent = _child(parent, token, op.path)
        if isinstance(parent, list):
            index = len(parent) if last == "-" else _index(parent, last, op.path)
            if op.op == "add":
                parent.insert(index, copy.deepcopy(op.value))
            elif not 0 <= index < len(parent):
                raise PatchError(f"{op.path}: index out of range")
            elif op.op == "remove":
                parent.pop(index)
            else:
                parent[index] = copy.deepcopy(op.value)
        elif isinstance(parent, dict):
            if op.op in ("remove", "replace") and last not in parent:
                raise PatchError(f"{op.path}: no key {last!r}")
            if op.op == "remove":
                del parent[last]
            else:
                parent[last] = copy.deepcopy(op.value)
        else:
            raise PatchError(f"{op.path}: parent is not a container")
    return doc
