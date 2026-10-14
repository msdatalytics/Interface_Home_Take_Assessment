"""Secret resolution. Artifacts reference secrets by vault key; values only ever exist
inside the process memory of the surface adapter and are registered with the redactor
the moment they are read, so they cannot appear in logs, evidence or LLM prompts."""

from __future__ import annotations

import os
from collections.abc import Mapping
from typing import Protocol

from cua.safety.redaction import Redactor


class SecretUnavailableError(LookupError):
    pass


class SecretVault(Protocol):
    def get(self, key: str) -> str: ...


class EnvVault:
    """Maps vault keys to environment variables (per tenant). A production deployment would
    swap this for HashiCorp Vault / AWS Secrets Manager behind the same protocol."""

    def __init__(self, mapping: Mapping[str, str], redactor: Redactor) -> None:
        self._mapping = dict(mapping)
        self._redactor = redactor

    def get(self, key: str) -> str:
        env_name = self._mapping.get(key)
        if env_name is None:
            raise SecretUnavailableError(f"secret '{key}' is not mapped for this tenant")
        value = os.environ.get(env_name)
        if not value:
            raise SecretUnavailableError(f"secret '{key}' is not set (expected ${env_name})")
        self._redactor.register(value, f"secret:{key}")
        return value

    def preload(self) -> None:
        """Register every mapped secret with the redactor up-front (defence in depth)."""
        for key in self._mapping:
            try:
                self.get(key)
            except SecretUnavailableError:
                continue
