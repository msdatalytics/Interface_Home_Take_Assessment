"""Shared fixtures.

Browser tests run the real stack - Playwright, the mock core (both tenants, on free ports),
the replay engine and the operator console - inside an isolated temporary workspace.
"""

from __future__ import annotations

import asyncio
import json
import shutil
import socket
import threading
import time
import urllib.request
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import uvicorn
import yaml

from mock_corebank.app import create_app
from mock_corebank.data import TENANTS

REPO = Path(__file__).resolve().parents[1]
USERNAME = "teller01"
PASSWORD = "Teller#2024!"


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


class MockCore:
    """Both mock tenants served from a background thread."""

    def __init__(self) -> None:
        self.urls: dict[str, str] = {}
        self._servers: list[uvicorn.Server] = []
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        for tenant_id, theme in TENANTS.items():
            port = free_port()
            app = create_app(theme, username=USERNAME, password=PASSWORD)
            config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning", access_log=False)
            self._servers.append(uvicorn.Server(config))
            self.urls[tenant_id] = f"http://127.0.0.1:{port}"

        def run() -> None:
            asyncio.run(self._serve())

        self._thread = threading.Thread(target=run, name="mock-core", daemon=True)
        self._thread.start()
        deadline = time.monotonic() + 15
        while not all(s.started for s in self._servers):
            if time.monotonic() > deadline:
                raise RuntimeError("mock core did not start")
            time.sleep(0.05)

    async def _serve(self) -> None:
        await asyncio.gather(*(s.serve() for s in self._servers))

    def stop(self) -> None:
        for server in self._servers:
            server.should_exit = True
        if self._thread:
            self._thread.join(timeout=10)

    def admin(self, tenant: str, method: str, path: str, body: dict[str, Any] | None = None) -> Any:
        data = json.dumps(body).encode() if body is not None else None
        request = urllib.request.Request(
            self.urls[tenant] + path, data=data, method=method, headers={"Content-Type": "application/json"}
        )
        with urllib.request.urlopen(request, timeout=10) as response:
            return json.loads(response.read())

    def fault(self, tenant: str, kind: str, *, path: str | None = None, count: int = 1, seconds: float = 3) -> None:
        self.admin(
            tenant, "POST", "/__admin/faults", {"kind": kind, "count": count, "path_contains": path, "seconds": seconds}
        )

    def reset(self) -> None:
        for tenant in self.urls:
            self.admin(tenant, "POST", "/__admin/reset")

    def audit(self, tenant: str) -> list[dict[str, Any]]:
        return list(self.admin(tenant, "GET", "/__admin/audit")["audit"])


@pytest.fixture(scope="session")
def mock_core() -> Iterator[MockCore]:
    core = MockCore()
    core.start()
    yield core
    core.stop()


@pytest.fixture(scope="session", autouse=True)
def _credentials() -> Iterator[None]:
    mp = pytest.MonkeyPatch()
    for tenant in ("CU_ALPHA", "CU_BETA"):
        mp.setenv(f"{tenant}_COREBANK_USERNAME", USERNAME)
        mp.setenv(f"{tenant}_COREBANK_PASSWORD", PASSWORD)
    mp.setenv("CUA_FINGERPRINT_KEY", "test-fingerprint-key")
    yield
    mp.undo()


def make_workspace(root: Path, urls: dict[str, str] | None = None) -> Path:
    """Copy the repository's config into ``root``, pointing tenants at the test servers."""
    shutil.copytree(REPO / "config", root / "config")
    (root / "capabilities").mkdir()
    for tenant_file in (root / "config" / "tenants").glob("*.yaml"):
        doc = yaml.safe_load(tenant_file.read_text())
        name = doc["metadata"]["name"]
        if urls and name in urls:
            doc["spec"]["apps"]["meridian_corebank"]["base_url"] = urls[name]
        tenant_file.write_text(yaml.safe_dump(doc, sort_keys=False))
    return root


@pytest.fixture
def workspace_dir(tmp_path: Path) -> Path:
    return make_workspace(tmp_path / "ws")
