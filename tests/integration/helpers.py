"""Test-side operator: drives the operator console over HTTP exactly like a person would."""

from __future__ import annotations

import asyncio
import json
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

from cua.artifact.capability import Approval, Capability
from cua.artifact.common import Status


@dataclass
class Operator:
    port: int
    resolution: str
    kind: str | None = None
    clicks: tuple[str, ...] = ()
    note: str = "handled by test operator"
    name: str = "ops.test"
    handled: list[dict[str, Any]] = field(default_factory=list)

    async def run(self, count: int = 1, deadline_s: float = 60) -> None:
        base = f"http://127.0.0.1:{self.port}"
        async with httpx.AsyncClient(base_url=base, timeout=30) as client:
            loop = asyncio.get_running_loop()
            deadline = loop.time() + deadline_s
            while len(self.handled) < count and loop.time() < deadline:
                try:
                    state = (await client.get("/api/state")).json()
                except httpx.TransportError:
                    await asyncio.sleep(0.2)
                    continue
                pending = [
                    iv
                    for iv in state["interventions"]
                    if iv["status"] == "open" and (self.kind is None or iv["kind"] == self.kind)
                ]
                if not pending:
                    await asyncio.sleep(0.2)
                    continue
                iv = pending[0]
                (
                    await client.post(f"/api/interventions/{iv['id']}/claim", json={"operator": self.name})
                ).raise_for_status()
                for element in self.clicks:
                    screen = (await client.get("/api/session/observation", params={"operator": self.name})).json()
                    ref = _find(screen["screen"], element)
                    body = {"operator": self.name, "kind": "click_ref", "ref": ref}
                    (await client.post("/api/session/input", json=body)).raise_for_status()
                    await asyncio.sleep(0.6)  # let in-page capture flush
                body = {"operator": self.name, "resolution": self.resolution, "note": self.note}
                (await client.post(f"/api/interventions/{iv['id']}/resolve", json=body)).raise_for_status()
                self.handled.append(iv)


def _find(screen: str, element: str) -> str:
    rx = re.compile(r"\[(e\d+)\] " + element)
    for line in screen.splitlines():
        if match := rx.search(line):
            return match.group(1)
    raise AssertionError(f"{element!r} not on screen:\n{screen}")


def approved(capability: Capability) -> Capability:
    """An approved copy, as ``cua approve`` would write it (kept in memory: tests stay independent)."""
    digest = capability.compute_digest()
    return capability.model_copy(
        update={
            "metadata": capability.metadata.model_copy(
                update={
                    "status": Status.APPROVED,
                    "digest": digest,
                    "approvals": [Approval(by="test.reviewer", at=datetime.now(UTC), digest=digest)],
                }
            )
        }
    )


def events(run_dir: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in (run_dir / "events.jsonl").read_text().splitlines()]


def evidence_text(run_dir: Path) -> str:
    """All textual evidence of a run (logs, results, observations, DOM snapshots)."""
    return "\n".join(
        p.read_text(errors="replace")
        for p in run_dir.rglob("*")
        if p.is_file() and p.suffix != ".png" and p.suffix != ".zip"
    )
