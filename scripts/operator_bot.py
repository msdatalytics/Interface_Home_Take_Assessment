"""Simulated human operator for the operator console API (demos, CI and evidence runs).

It does exactly what a person does in the console UI, over the same HTTP API: wait for an
intervention, claim it (taking the control lease), optionally interact with the *same live
browser session*, then resolve it (handing control back).

    python scripts/operator_bot.py --resolution approve --note "reviewed: amounts match"
    python scripts/operator_bot.py --click "button \\"Continue Session\\"" --resolution retry_step
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
import urllib.error
import urllib.request
from typing import Any


class Console:
    def __init__(self, base: str, operator: str) -> None:
        self.base = base.rstrip("/")
        self.operator = operator

    def _call(self, method: str, path: str, body: dict[str, Any] | None = None) -> Any:
        data = json.dumps(body).encode() if body is not None else None
        request = urllib.request.Request(  # noqa: S310 - local console URL
            self.base + path, data=data, method=method, headers={"Content-Type": "application/json"}
        )
        with urllib.request.urlopen(request, timeout=30) as response:  # noqa: S310 - local console URL
            return json.loads(response.read())

    def state(self) -> dict[str, Any]:
        state: dict[str, Any] = self._call("GET", "/api/state")
        return state

    def claim(self, iv_id: str) -> None:
        self._call("POST", f"/api/interventions/{iv_id}/claim", {"operator": self.operator})

    def screen(self) -> str:
        return str(self._call("GET", f"/api/session/observation?operator={self.operator}")["screen"])

    def click_ref(self, ref: str) -> None:
        self._call("POST", "/api/session/input", {"operator": self.operator, "kind": "click_ref", "ref": ref})

    def type_text(self, text: str) -> None:
        self._call("POST", "/api/session/input", {"operator": self.operator, "kind": "type", "text": text})

    def resolve(self, iv_id: str, resolution: str, note: str | None) -> None:
        self._call(
            "POST",
            f"/api/interventions/{iv_id}/resolve",
            {"operator": self.operator, "resolution": resolution, "note": note},
        )


def find_ref(screen: str, element: str) -> str:
    pattern = re.compile(r"\[(e\d+)\] " + element)
    for line in screen.splitlines():
        if match := pattern.search(line):
            return match.group(1)
    raise SystemExit(f"operator: no element matching {element!r} on screen:\n{screen}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--console", default="http://127.0.0.1:8765")
    parser.add_argument("--operator", default="ops.jane")
    parser.add_argument("--kind", help="only handle interventions of this kind (approval|stuck|failure)")
    parser.add_argument("--click", action="append", default=[], help="regex of an element to click, in order")
    parser.add_argument("--resolution", required=True)
    parser.add_argument("--note")
    parser.add_argument("--count", type=int, default=1, help="number of interventions to handle")
    parser.add_argument("--think", type=float, default=1.0, help="seconds to 'look at' the screen")
    parser.add_argument("--timeout", type=float, default=180)
    args = parser.parse_args()

    console = Console(args.console, args.operator)
    handled: set[str] = set()
    deadline = time.monotonic() + args.timeout
    while len(handled) < args.count:
        if time.monotonic() > deadline:
            print("operator: timed out waiting for interventions", file=sys.stderr)
            return 1
        try:
            interventions = console.state()["interventions"]
        except (urllib.error.URLError, ConnectionError):
            time.sleep(0.5)
            continue
        pending = [
            iv
            for iv in interventions
            if iv["status"] == "open" and iv["id"] not in handled and (not args.kind or iv["kind"] == args.kind)
        ]
        if not pending:
            time.sleep(0.5)
            continue
        iv = pending[-1]
        print(f"operator: {iv['kind']} {iv['id']}: {iv['reason']}")
        console.claim(iv["id"])
        time.sleep(args.think)
        for element in args.click:
            ref = find_ref(console.screen(), element)
            print(f"operator: click {ref} ({element})")
            console.click_ref(ref)
            time.sleep(args.think)
        console.resolve(iv["id"], args.resolution, args.note)
        print(f"operator: resolved {iv['id']} -> {args.resolution}")
        handled.add(iv["id"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
