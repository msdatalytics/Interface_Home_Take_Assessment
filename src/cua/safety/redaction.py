"""Redaction of regulated data before anything is persisted, logged, or sent to a model.

Two complementary mechanisms:

* **Known values** - secrets fetched from the vault and caller inputs classified ``pii``/
  ``secret`` are registered at runtime and replaced wherever they appear (exact match).
  This catches data no regex could recognise (a member number is just six digits).
* **Patterns** - SSNs, card numbers (Luhn-checked), e-mails, phone numbers, bearer
  tokens, long account-like numbers - for sensitive data the run never declared.

Confidential *outputs* (e.g. balances) are never written in clear: persisted results carry
a keyed fingerprint instead, so two runs can be compared for determinism without storing
the value.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import re
import secrets
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

DEFAULT_PATTERNS: dict[str, str] = {
    "ssn": r"\b\d{3}-\d{2}-\d{4}\b",
    "card": r"\b(?:\d[ -]?){13,19}\b",
    "email": r"\b[\w.+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}\b",
    "phone": r"(?<!\d)(?:\+1[ .-]?)?\(?\d{3}\)?[ .-]\d{3}[ .-]\d{4}(?!\d)",
    "bearer": r"(?i)\bbearer\s+[a-z0-9._~+/=-]{10,}",
    "jwt": r"\beyJ[\w-]{8,}\.[\w-]{8,}\.[\w-]{8,}\b",
    "account_number": r"(?<![\w$.,-])\d{9,17}(?![\w.,])",
}

SENSITIVE_KEY = re.compile(r"(?i)(password|passwd|pwd|secret|token|authorization|cookie|ssn|api[_-]?key)")


def _luhn_ok(digits: str) -> bool:
    nums = [int(c) for c in digits if c.isdigit()]
    if not 13 <= len(nums) <= 19:
        return False
    total = 0
    for i, digit in enumerate(reversed(nums)):
        doubled = digit * 2 if i % 2 else digit
        total += doubled - 9 if doubled > 9 else doubled
    return total % 10 == 0


class Redactor:
    def __init__(self, patterns: Mapping[str, str] | None = None, *, fingerprint_key: bytes | None = None) -> None:
        merged = {**DEFAULT_PATTERNS, **(patterns or {})}
        self._patterns = [(name, re.compile(rx)) for name, rx in merged.items()]
        self._known: dict[str, str] = {}
        self._known_rx: re.Pattern[str] | None = None
        self._fp_key = fingerprint_key or load_fingerprint_key()

    # ------------------------------------------------------------------ registration

    def register(self, value: str, label: str) -> None:
        """Mask ``value`` as ``[label]`` everywhere from now on. Very short values are ignored
        (masking ``"S0"`` everywhere would destroy evidence without protecting anything)."""
        value = value.strip()
        if len(value) < 4 or value in self._known:
            return
        self._known[value] = label
        ordered = sorted(self._known, key=len, reverse=True)
        self._known_rx = re.compile("|".join(re.escape(v) for v in ordered))

    def register_many(self, items: Iterable[tuple[str, str]]) -> None:
        for value, label in items:
            self.register(value, label)

    # ------------------------------------------------------------------ application

    def text(self, text: str) -> str:
        if not text:
            return text
        if self._known_rx is not None:
            text = self._known_rx.sub(lambda m: f"[{self._known[m.group(0)]}]", text)
        for name, rx in self._patterns:
            if name == "card":
                text = rx.sub(lambda m: "[REDACTED:card]" if _luhn_ok(m.group(0)) else m.group(0), text)
            else:
                text = rx.sub(f"[REDACTED:{name}]", text)
        return text

    def obj(self, value: Any, *, _key: str | None = None) -> Any:
        """Recursively redact a JSON-like structure. Values under sensitive-looking keys are dropped."""
        if _key is not None and SENSITIVE_KEY.search(_key) and isinstance(value, str | bytes):
            return "[REDACTED]"
        if isinstance(value, str):
            return self.text(value)
        if isinstance(value, Mapping):
            return {k: self.obj(v, _key=str(k)) for k, v in value.items()}
        if isinstance(value, list | tuple):
            return [self.obj(v) for v in value]
        return value

    def fingerprint(self, value: Any) -> str:
        """Keyed, non-reversible fingerprint (HMAC-SHA256) for comparing values across runs."""
        digest = hmac.new(self._fp_key, str(value).encode(), hashlib.sha256).hexdigest()
        return f"hmac:{digest[:16]}"

    @property
    def known_values(self) -> list[str]:
        return list(self._known)


def load_fingerprint_key() -> bytes:
    """Per-installation key: ``$CUA_FINGERPRINT_KEY`` or a git-ignored ``.cua/fingerprint.key``."""
    env = os.environ.get("CUA_FINGERPRINT_KEY")
    if env:
        return env.encode()
    path = Path(os.environ.get("CUA_STATE_DIR", ".cua")) / "fingerprint.key"
    try:
        return path.read_bytes()
    except FileNotFoundError:
        path.parent.mkdir(parents=True, exist_ok=True)
        key = secrets.token_bytes(32)
        path.write_bytes(key)
        path.chmod(0o600)
        return key
