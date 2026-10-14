"""Per-run evidence: an append-only, redacted JSONL event log plus richer artifacts
(masked screenshots, redacted DOM snapshots, LLM turns, results) in one directory."""

from __future__ import annotations

import json
import secrets
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from cua.safety.redaction import Redactor

Subscriber = Callable[[dict[str, Any]], None]


def new_run_id(kind: str) -> str:
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    return f"{stamp}-{kind}-{secrets.token_hex(3)}"


_RESERVED = frozenset({"seq", "ts", "run_id", "type"})


def portable_path(path: Path) -> str:
    """Relative to the working directory when inside it, so evidence never embeds a user's home path."""
    try:
        return str(path.resolve().relative_to(Path.cwd().resolve()))
    except ValueError:
        return str(path)


class RunRecorder:
    def __init__(self, root: Path, kind: str, redactor: Redactor, run_id: str | None = None) -> None:
        self.run_id = run_id or new_run_id(kind)
        self.kind = kind
        self.dir = root / self.run_id
        self.dir.mkdir(parents=True, exist_ok=True)
        self.redactor = redactor
        self._seq = 0
        self._log = (self.dir / "events.jsonl").open("a", encoding="utf-8")
        self._subscribers: list[Subscriber] = []

    def subscribe(self, fn: Subscriber) -> None:
        self._subscribers.append(fn)

    def emit(self, type_: str, data: dict[str, Any]) -> None:
        """Callback-shaped variant of :meth:`event` for components that only publish."""
        self.event(type_, **data)

    def event(self, type_: str, **data: Any) -> dict[str, Any]:
        if clash := _RESERVED.intersection(data):
            raise ValueError(f"event fields {sorted(clash)} are reserved")
        self._seq += 1
        record = {
            "seq": self._seq,
            "ts": datetime.now(UTC).isoformat(timespec="milliseconds"),
            "run_id": self.run_id,
            "type": type_,
            **self.redactor.obj(_jsonable(data)),
        }
        self._log.write(json.dumps(record, ensure_ascii=False) + "\n")
        self._log.flush()
        for fn in self._subscribers:
            fn(record)
        return record

    def next_name(self, stem: str, suffix: str) -> Path:
        safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in stem)[:60]
        return self.dir / f"{self._seq:04d}-{safe}{suffix}"

    def write_json(self, name: str, payload: Any, *, redact: bool = True) -> Path:
        path = self.dir / name
        path.parent.mkdir(parents=True, exist_ok=True)
        data = _jsonable(payload)
        if redact:
            data = self.redactor.obj(data)
        path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        return path

    def write_text(self, name: str, text: str, *, redact: bool = True) -> Path:
        path = self.dir / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(self.redactor.text(text) if redact else text, encoding="utf-8")
        return path

    def relative(self, path: Path) -> str:
        try:
            return str(path.relative_to(self.dir))
        except ValueError:
            return str(path)

    def close(self) -> None:
        if not self._log.closed:
            self._log.close()


def _jsonable(value: Any) -> Any:
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json", exclude_none=True, by_alias=True)
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, list | tuple | set):
        return [_jsonable(v) for v in value]
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if hasattr(value, "value") and isinstance(getattr(value, "value", None), str):  # enums
        return value.value
    return value
