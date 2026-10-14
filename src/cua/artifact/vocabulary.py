"""Tenant vocabulary: canonical UI term -> the term a specific tenant's installation shows."""

from __future__ import annotations

from collections.abc import Mapping

from cua.textutil import normalize_label


class Vocabulary:
    def __init__(self, mapping: Mapping[str, str] | None = None) -> None:
        self._map = {normalize_label(k): v for k, v in (mapping or {}).items()}

    def translate(self, text: str) -> str:
        return self._map.get(normalize_label(text), text)

    def __bool__(self) -> bool:
        return bool(self._map)

    def __len__(self) -> int:
        return len(self._map)
