"""Version-range checks for app compatibility and tenant overrides."""

from __future__ import annotations

from packaging.specifiers import InvalidSpecifier, SpecifierSet
from packaging.version import InvalidVersion, Version


def satisfies(version: str | None, spec: str) -> bool:
    """``True`` if ``version`` is within ``spec`` (``"*"`` matches anything, including unknown)."""
    if spec.strip() in ("", "*"):
        return True
    if version is None:
        return False
    try:
        return Version(version) in SpecifierSet(spec)
    except (InvalidSpecifier, InvalidVersion):
        return False
