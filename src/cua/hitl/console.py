"""Minimal operator console: a real control-transfer surface over HTTP.

It runs in the same process and event loop as the automation, against the *same* browser
context, so the operator works on the exact live session the automation paused (cookies,
page state, half-filled forms). It is intentionally bare - a production console would
add SSO/RBAC, co-browsing via a CDP screencast or VNC, and a queue across runs - but the
control-transfer model (claim -> exclusive human lease -> resolve -> automation resumes,
fenced by epoch) is the real one.
"""

from __future__ import annotations

import asyncio
import contextlib
from importlib import resources
from typing import Any, Literal

import uvicorn
from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse, Response
from pydantic import BaseModel

from cua.hitl.control import ControlChannel, LeaseLostError
from cua.hitl.desk import InterventionDesk, InterventionError, Resolution
from cua.surface.base import ActionError, Surface


class ClaimRequest(BaseModel):
    operator: str


class ResolveRequest(BaseModel):
    operator: str
    resolution: Resolution
    note: str | None = None


class InputRequest(BaseModel):
    operator: str
    kind: Literal["click_xy", "click_ref", "type", "key"]
    x: float | None = None
    y: float | None = None
    ref: str | None = None
    text: str | None = None
    key: str | None = None


def create_console_app(desk: InterventionDesk, channel: ControlChannel, surface: Surface) -> FastAPI:
    app = FastAPI(title="cua operator console", docs_url=None, redoc_url=None)
    page = resources.files("cua.hitl").joinpath("console.html").read_text(encoding="utf-8")
    lock = asyncio.Lock()

    def lease_json() -> dict[str, Any]:
        lease = channel.lease
        return {
            "holder": lease.holder,
            "epoch": lease.epoch,
            "operator": lease.operator,
            "since": lease.since.isoformat(),
            "reason": lease.reason,
            "intervention": lease.intervention_id,
        }

    def require_operator(operator: str) -> None:
        try:
            channel.assert_operator(operator)
        except LeaseLostError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc

    @app.get("/", response_class=HTMLResponse)
    @app.get("/interventions/{iv_id}", response_class=HTMLResponse)
    async def index(iv_id: str | None = None) -> str:
        return page

    @app.get("/api/state")
    async def state() -> dict[str, Any]:
        return {
            "lease": lease_json(),
            "interventions": [iv.model_dump(mode="json") for iv in reversed(desk.interventions.values())],
        }

    @app.post("/api/interventions/{iv_id}/claim")
    async def claim(iv_id: str, body: ClaimRequest) -> dict[str, Any]:
        try:
            iv = desk.claim(iv_id, body.operator)
        except InterventionError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return {"intervention": iv.model_dump(mode="json"), "lease": lease_json()}

    @app.post("/api/interventions/{iv_id}/resolve")
    async def resolve(iv_id: str, body: ResolveRequest) -> dict[str, Any]:
        try:
            iv = desk.resolve(iv_id, body.operator, body.resolution, body.note)
        except InterventionError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return {"intervention": iv.model_dump(mode="json"), "lease": lease_json()}

    @app.get("/api/session/screen.png")
    async def screen(operator: str) -> Response:
        require_operator(operator)
        async with lock:
            data = await surface.live_screenshot()
        return Response(content=data, media_type="image/png", headers={"Cache-Control": "no-store"})

    @app.get("/api/session/observation")
    async def observation(operator: str) -> dict[str, Any]:
        require_operator(operator)
        async with lock:
            obs = await surface.observe()
        return {"screen": obs.render(desk.recorder.redactor, surface.sensitive_label_patterns)}

    @app.post("/api/session/input")
    async def operator_input(body: InputRequest) -> dict[str, Any]:
        require_operator(body.operator)
        async with lock:
            try:
                match body.kind:
                    case "click_xy" if body.x is not None and body.y is not None:
                        await surface.operator_click(body.x, body.y)
                    case "click_ref" if body.ref:
                        await surface.operator_click_ref(body.ref)
                    case "type" if body.text is not None:
                        await surface.operator_type(body.text)
                    case "key" if body.key:
                        await surface.operator_key(body.key)
                    case _:
                        raise HTTPException(status_code=422, detail="incomplete input request")
            except ActionError as exc:
                raise HTTPException(status_code=409, detail=str(exc)) from exc
            await surface.wait_settled(timeout_ms=5_000)
        detail = body.model_dump(exclude_none=True, exclude={"operator", "text"})
        if body.text is not None:
            detail["text_length"] = len(body.text)
        desk.record_operator_input(body.operator, detail)
        return {"ok": True, "location": surface.location}

    return app


class OperatorConsole:
    """Runs the console app on localhost inside the current event loop."""

    def __init__(self, app: FastAPI, *, host: str = "127.0.0.1", port: int = 8765) -> None:
        self.url = f"http://{host}:{port}"
        config = uvicorn.Config(app, host=host, port=port, log_level="warning", access_log=False)
        self._server = uvicorn.Server(config)
        self._task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        self._task = asyncio.create_task(self._server.serve())
        for _ in range(100):
            if self._server.started:
                return
            await asyncio.sleep(0.05)
        raise RuntimeError(f"operator console failed to start on {self.url}")

    async def stop(self) -> None:
        self._server.should_exit = True
        if self._task is not None:
            with contextlib.suppress(Exception):
                await asyncio.wait_for(self._task, timeout=5)
