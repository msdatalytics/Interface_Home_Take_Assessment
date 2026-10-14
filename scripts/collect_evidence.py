"""Regenerate ``evidence/``: discovery runs, replays, error replays, recoveries and handoffs.

Every scenario is executed through the real CLI against a running mock core (``make mock``),
exactly as a user would; its console transcript and run directory (structured event log,
screenshots, masked DOM on failure, result contract) are copied into ``evidence/`` and indexed
in ``evidence/README.md``.

    uv run python scripts/collect_evidence.py --discover            # LLM discovery first (needs a key)
    uv run python scripts/collect_evidence.py                       # replays of the committed artifacts
    uv run python scripts/collect_evidence.py --only handoff        # scenarios whose id matches
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.request
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml

REPO = Path(__file__).resolve().parents[1]
RUNS = REPO / "runs"
EVIDENCE = REPO / "evidence"
BIN = Path(sys.executable).parent
PORTS = {"cu-alpha": 8401, "cu-beta": 8402}
CONSOLE = "http://127.0.0.1:8765"

BALANCE = "corebank.member.get_savings_balance"
OPEN_SHARE = "corebank.member.open_share_account"
SHARE = [
    "-p",
    "member_id=100591",
    "-p",
    "share_type=Holiday Club",
    "-p",
    "nickname=Ski Trip",
    "-p",
    "initial_deposit=75",
]
_ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")


@dataclass(frozen=True)
class Fault:
    tenant: str
    kind: str
    path: str
    count: int = 1
    seconds: float = 3.0


@dataclass
class Scenario:
    id: str
    title: str
    shows: str
    args: list[str]
    expect_exit: int
    faults: tuple[Fault, ...] = ()
    operator: list[str] | None = None
    record: dict[str, Any] = field(default_factory=dict)


def admin(tenant: str, method: str, path: str, body: dict[str, Any] | None = None) -> Any:
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(
        f"http://127.0.0.1:{PORTS[tenant]}{path}",
        data=data,
        method=method,
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=5) as response:  # noqa: S310 - local mock only
        return json.loads(response.read() or b"null")


def fresh_core() -> None:
    for tenant in PORTS:
        admin(tenant, "POST", "/__admin/reset")
        admin(tenant, "DELETE", "/__admin/faults")


def arm(fault: Fault) -> None:
    body = {"kind": fault.kind, "path_contains": fault.path, "count": fault.count, "seconds": fault.seconds}
    admin(fault.tenant, "POST", "/__admin/faults", body)


def cua(*args: str) -> list[str]:
    bounded = ["--escalation-timeout", "180"] if args[0] in ("discover", "replay") else []
    return [str(BIN / "cua"), *args, *bounded]


def run(scenario: Scenario, section: str) -> None:
    fresh_core()
    for fault in scenario.faults:
        arm(fault)
    before = set(RUNS.iterdir()) if RUNS.exists() else set()
    env = {**os.environ, "NO_COLOR": "1", "TERM": "dumb", "COLUMNS": "110"}
    operator = None
    if scenario.operator is not None:
        operator = subprocess.Popen(  # noqa: S603 - fixed local script
            [sys.executable, "-u", str(REPO / "scripts" / "operator_bot.py"), "--console", CONSOLE, *scenario.operator],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
    started = time.monotonic()
    proc = subprocess.run(scenario.args, cwd=REPO, env=env, capture_output=True, text=True, check=False)  # noqa: S603
    elapsed = time.monotonic() - started
    operator_log = ""
    if operator is not None:
        operator.terminate()  # it waits for up to --count interventions; the run is over
        try:
            operator_log, _ = operator.communicate(timeout=10)
        except subprocess.TimeoutExpired:
            operator.kill()
            operator_log, _ = operator.communicate()

    target = EVIDENCE / section / scenario.id
    shutil.rmtree(target, ignore_errors=True)
    target.mkdir(parents=True)
    new_runs = sorted(set(RUNS.iterdir()) - before) if RUNS.exists() else []
    for run_dir in new_runs:
        destination = target if len(new_runs) == 1 else target / run_dir.name
        shutil.copytree(run_dir, destination, dirs_exist_ok=True)
    command = " ".join(_display(a) for a in scenario.args)
    transcript = [f"$ {command}", _ANSI.sub("", proc.stdout + proc.stderr).rstrip(), f"[exit {proc.returncode}]"]
    if operator_log:
        transcript += ["", "--- simulated operator (scripts/operator_bot.py) ---", operator_log.rstrip()]
    (target / "console.txt").write_text("\n".join(transcript) + "\n")

    result = _result(target)
    scenario.record = {
        "id": scenario.id,
        "section": section,
        "title": scenario.title,
        "shows": scenario.shows,
        "command": command,
        "faults": [f"{f.kind} on {f.path} x{f.count}" for f in scenario.faults],
        "exit": proc.returncode,
        "expected_exit": scenario.expect_exit,
        "ok": proc.returncode == scenario.expect_exit,
        "seconds": round(elapsed, 1),
        "status": result.get("status") or _discovery_status(target),
        "code": (result.get("outcome") or {}).get("code") or (result.get("failure") or {}).get("code") or "",
        "runs": [r.name for r in new_runs],
    }
    mark = "ok " if scenario.record["ok"] else "UNEXPECTED"
    outcome = f"{scenario.record['status']} {scenario.record['code']}".strip()
    print(f"[{mark}] {section}/{scenario.id}: exit={proc.returncode} {outcome}")


def _display(arg: str) -> str:
    arg = arg.replace(str(BIN) + "/", "").replace(str(REPO) + "/", "")
    return f'"{arg}"' if " " in arg else arg


def _result(target: Path) -> dict[str, Any]:
    files = sorted(target.rglob("result.json"))
    if not files:
        return {}
    data: dict[str, Any] = json.loads(files[-1].read_text())
    return data


def _discovery_status(target: Path) -> str:
    text = (target / "console.txt").read_text()
    match = re.search(r"^─*\s*(COMPLETED|GAVE_UP|FAILED|ABORTED|ESCALATED)\b", text, re.MULTILINE)
    return match.group(1).lower() if match else ""


# ------------------------------------------------------------------------------ derived artifacts


def artifact(name: str) -> Path:
    return REPO / "capabilities" / "meridian_corebank" / f"{name}.yaml"


def drifted_copy(work: Path) -> Path:
    """The balance capability with its Search button 'renamed' in the primary locator (UI drift)."""
    doc = yaml.safe_load((artifact(BALANCE)).read_text())
    step = next(s for s in doc["spec"]["steps"] if s["action"] == "click" and "search" in s["id"])
    step["target"]["locators"][0] = {"strategy": "role", "role": "button", "name": "Find"}
    doc["metadata"].pop("digest", None)
    path = work / "get_savings_balance.drifted.yaml"
    path.write_text(yaml.safe_dump(doc, sort_keys=False, allow_unicode=True))
    return path


def approved_copy(work: Path) -> Path:
    path = work / "open_share_account.approved.yaml"
    shutil.copy(artifact(OPEN_SHARE), path)
    subprocess.run(  # noqa: S603
        cua("approve", str(path), "--by", "reviewer.sam", "--note", "reviewed steps, target and amounts"),
        cwd=REPO,
        check=True,
        capture_output=True,
    )
    return path


def tampered_copy(work: Path, approved: Path) -> Path:
    doc = yaml.safe_load(approved.read_text())
    doc["spec"]["steps"][0]["timeout_ms"] = 9999
    path = work / "open_share_account.tampered.yaml"
    path.write_text(yaml.safe_dump(doc, sort_keys=False, allow_unicode=True))
    return path


# ------------------------------------------------------------------------------ scenarios


def discovery_scenarios(provider: list[str]) -> list[Scenario]:
    return [
        Scenario(
            "get_savings_balance",
            "LLM discovery: savings balance lookup",
            "observe→decide→act loop, verified ranked locators, derived checkpoints, extraction, no PII in artifact",
            cua("discover", "requests/get_savings_balance.yaml", "-t", "cu-alpha", *provider),
            0,
        ),
        Scenario(
            "open_share_account",
            "LLM discovery: open a share sub-account (irreversible)",
            "irreversible Submit detected and paused for live human approval before it was clicked",
            cua("discover", "requests/open_share_account.yaml", "-t", "cu-alpha", *provider),
            0,
            operator=[
                "--kind",
                "approval",
                "--resolution",
                "approve",
                "--count",
                "3",
                "--timeout",
                "1500",
                "--note",
                "reviewed: test member, $250 cash",
            ],
        ),
    ]


def replay_scenarios(work: Path) -> list[Scenario]:
    balance = ["replay", BALANCE]
    approved = approved_copy(work)
    return [
        Scenario(
            "01-success",
            "Replay: success",
            "typed output, no LLM, confidential output fingerprinted at rest",
            cua(*balance, "-p", "member_id=100234"),
            0,
        ),
        Scenario(
            "02-stability",
            "Replay x10: stability report",
            "identical output fingerprint across 10 runs",
            cua(*balance, "-p", "member_id=100234", "--repeat", "10"),
            0,
        ),
        Scenario(
            "03-tenant-beta",
            "Replay on cu-beta (other tenant)",
            "vocabulary map + step-id override reuse the same artifact on a relabelled tenant",
            cua(*balance, "-t", "cu-beta", "-p", "member_id=200145"),
            0,
        ),
        Scenario(
            "04-outcome-not-found",
            "Business outcome: record not found",
            "status business_outcome, exit 2",
            cua(*balance, "-p", "member_id=999999"),
            2,
        ),
        Scenario(
            "05-outcome-validation",
            "Business outcome: app validation message",
            "app message captured into the outcome",
            cua(*balance, "-p", "member_id=123"),
            2,
        ),
        Scenario(
            "06-outcome-access-denied",
            "Business outcome: restricted member",
            "SEC-403 page mapped to access_denied",
            cua(*balance, "-p", "member_id=100777"),
            2,
        ),
        Scenario(
            "07-input-invalid",
            "Hard failure: invalid input",
            "typed contract rejects input before the app is touched",
            cua(*balance, "-p", "member_id=12AB"),
            1,
        ),
        Scenario(
            "08-recover-maintenance",
            "Recovery: maintenance interstitial",
            "interstitial handler dismisses it, step resumes",
            cua(*balance, "-p", "member_id=100234"),
            0,
            (Fault("cu-alpha", "maintenance_notice", "mbr_detail"),),
        ),
        Scenario(
            "09-recover-server-error",
            "Recovery: transient server error",
            "bounded backoff + frame reload",
            cua(*balance, "-p", "member_id=100234"),
            0,
            (Fault("cu-alpha", "server_error", "mbr_detail"),),
        ),
        Scenario(
            "10-recover-session-expired",
            "Recovery: session expired mid-run",
            "re-login and safe restart (read-only so far)",
            cua(*balance, "-p", "member_id=100234"),
            0,
            (Fault("cu-alpha", "session_expired", "mbr_detail"),),
        ),
        Scenario(
            "11-recover-slow",
            "Recovery: slow application (3 s)",
            "waits on checkpoints, not sleeps",
            cua(*balance, "-p", "member_id=100234"),
            0,
            (Fault("cu-alpha", "slow", "mbr_search", seconds=3),),
        ),
        Scenario(
            "12-drift-locator-fallback",
            "Drift: primary locator broken",
            "falls back to next ranked locator, reports drift",
            cua("replay", str(drifted_copy(work)), "-p", "member_id=100234"),
            0,
        ),
        Scenario(
            "13-fail-persistent-error",
            "Hard failure: persistent server error",
            "APP_ERROR (retryable) with masked screenshot + DOM snapshot",
            cua(*balance, "-p", "member_id=100234"),
            1,
            (Fault("cu-alpha", "server_error", "mbr_detail", count=10),),
        ),
        Scenario(
            "14-fail-unexpected-modal",
            "Hard failure: unexpected modal",
            "fails fast with an actionable reason",
            cua(*balance, "-p", "member_id=100234"),
            1,
            (Fault("cu-alpha", "unknown_dialog", "mbr_search"),),
        ),
        Scenario(
            "15-handoff-unexpected-modal",
            "Handoff: operator clears the modal in the live session",
            "escalate → lease to human → human click captured → retry_step → automation resumes",
            cua(*balance, "-p", "member_id=100234", "--on-failure", "escalate"),
            0,
            (Fault("cu-alpha", "unknown_dialog", "mbr_search"),),
            operator=[
                "--kind",
                "failure",
                "--click",
                'button "Continue Session"',
                "--resolution",
                "retry_step",
                "--note",
                "dismissed duplicate-session prompt",
            ],
        ),
        Scenario(
            "16-irreversible-unapproved",
            "Irreversible: draft artifact refused",
            "APPROVAL_REQUIRED at preflight, the app is never touched",
            cua("replay", OPEN_SHARE, *SHARE),
            1,
        ),
        Scenario(
            "17-irreversible-jit-approval",
            "Irreversible: just-in-time operator approval",
            "pauses before Submit, operator approves, transaction commits",
            cua("replay", OPEN_SHARE, *SHARE, "--on-failure", "escalate"),
            0,
            operator=[
                "--kind",
                "approval",
                "--resolution",
                "approve",
                "--count",
                "3",
                "--timeout",
                "120",
                "--note",
                "verified with member by phone",
            ],
        ),
        Scenario(
            "18-irreversible-approved",
            "Irreversible: approved artifact runs unattended",
            "approval bound to the content digest",
            cua("replay", str(approved), *SHARE),
            0,
        ),
        Scenario(
            "19-irreversible-tampered",
            "Irreversible: edited after approval",
            "digest mismatch revokes the approval",
            cua("replay", str(tampered_copy(work, approved)), *SHARE),
            1,
        ),
        Scenario(
            "20-irreversible-outcome-unknown",
            "Irreversible: error right after Submit",
            "OUTCOME_UNKNOWN, not retried (never double-post)",
            cua("replay", str(approved), *SHARE),
            1,
            (Fault("cu-alpha", "server_error", "subacct_submit"),),
        ),
    ]


# ------------------------------------------------------------------------------ index


def write_index(records: list[dict[str, Any]]) -> None:
    index = EVIDENCE / "index.json"
    existing = {r["section"] + "/" + r["id"]: r for r in json.loads(index.read_text())} if index.exists() else {}
    existing.update({r["section"] + "/" + r["id"]: r for r in records})
    ordered = sorted(existing.values(), key=lambda r: (r["section"] != "discovery", r["section"], r["id"]))
    index.write_text(json.dumps(ordered, indent=2) + "\n")

    lines = [
        "# Evidence",
        "",
        "Generated by `scripts/collect_evidence.py` against the bundled mock core "
        f"({datetime.now(UTC):%Y-%m-%d %H:%M} UTC). Each folder holds the CLI transcript (`console.txt`) and the",
        "run directory: `events.jsonl` (structured, redacted, sequenced log), per-step screenshots, `result.json`",
        "(the result contract; confidential outputs as HMAC fingerprints), and on failure a masked screenshot,",
        "masked per-frame DOM snapshot and redacted semantic observation. Discovery runs also contain every",
        "model turn (`llm/turn-*.json`: the exact redacted prompt, tool call, usage) and the recorded `artifact.yaml`.",
        "",
        "Exit codes: `0` succeeded, `2` business outcome, `1` failed.",
        "",
        "| scenario | what it shows | result | exit |",
        "|---|---|---|---|",
    ]
    for r in ordered:
        result = " ".join(x for x in (r["status"], r["code"]) if x)
        flag = "" if r["ok"] else " (unexpected)"
        lines.append(
            f"| [{r['section']}/{r['id']}]({r['section']}/{r['id']}/console.txt) - {r['title']} "
            f"| {r['shows']} | `{result}` | {r['exit']}{flag} |"
        )
    lines += [
        "",
        "Faults were injected through the mock's admin API (`mock-corebank fault …`) where listed in",
        "`index.json`. The simulated operator (`scripts/operator_bot.py`) uses the same console HTTP API",
        "as the browser console UI.",
        "",
    ]
    (EVIDENCE / "README.md").write_text("\n".join(lines))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--discover", action="store_true", help="run LLM discovery first (overwrites capabilities/)")
    parser.add_argument("--provider", help="discovery provider (default: CUA_LLM_PROVIDER)")
    parser.add_argument("--skip-replays", action="store_true")
    parser.add_argument("--only", help="regex: only scenarios whose id matches")
    args = parser.parse_args()
    try:
        fresh_core()
    except OSError as exc:
        print(f"mock core not reachable ({exc}); start it with `make mock`", file=sys.stderr)
        return 1

    only = re.compile(args.only) if args.only else None
    records: list[dict[str, Any]] = []
    if args.discover:
        provider = ["--provider", args.provider] if args.provider else []
        for scenario in discovery_scenarios(provider):
            if only is None or only.search(scenario.id):
                run(scenario, "discovery")
                records.append(scenario.record)
    if not args.skip_replays:
        inputs = EVIDENCE / "inputs"
        shutil.rmtree(inputs, ignore_errors=True)
        inputs.mkdir(parents=True)
        for scenario in replay_scenarios(inputs):
            if only is None or only.search(scenario.id):
                run(scenario, "replay")
                records.append(scenario.record)
    fresh_core()
    write_index(records)
    failed = [r for r in records if not r["ok"]]
    print(f"{len(records) - len(failed)}/{len(records)} scenarios behaved as expected; index: evidence/README.md")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
