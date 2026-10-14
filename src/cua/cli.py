"""``cua`` command line.

Exit codes (scriptable): 0 = succeeded, 2 = business outcome, 1 = failed / invalid.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated

import typer
import yaml
from dotenv import load_dotenv
from pydantic import BaseModel, ValidationError
from rich.console import Console
from rich.markup import escape
from rich.table import Table

from cua.artifact.capability import Approval, Capability
from cua.artifact.common import Status
from cua.artifact.io import load_as, load_yaml, save, to_yaml
from cua.artifact.lint import lint
from cua.artifact.profile import AppProfile
from cua.artifact.tenant import Tenant
from cua.discovery.llm import LLMClient, LLMError, ScriptedClient, client_from_env
from cua.discovery.request import DiscoveryRequest
from cua.evidence.recorder import portable_path
from cua.replay.engine import ReplayOptions
from cua.runtime.live import LiveOptions
from cua.runtime.result import RunResult, RunStatus
from cua.safety.policy import PolicyDocument
from cua.safety.redaction import Redactor
from cua.workspace import Workspace, WorkspaceError

app = typer.Typer(
    add_completion=False,
    no_args_is_help=True,
    rich_markup_mode="rich",
    help="Computer-use automation: discover with an LLM, replay deterministically.",
)
console = Console(stderr=True)

_KINDS: dict[str, type[BaseModel]] = {
    "Capability": Capability,
    "AppProfile": AppProfile,
    "Tenant": Tenant,
    "Policy": PolicyDocument,
    "DiscoveryRequest": DiscoveryRequest,
}
out = Console()

WorkspaceOpt = Annotated[
    Path, typer.Option("--workspace", "-w", help="Repository root with config/ and capabilities/.")
]
TenantOpt = Annotated[str, typer.Option("--tenant", "-t", help="Tenant to run against.")]
HeadedOpt = Annotated[bool, typer.Option("--headed", help="Show the browser window (operators can use it directly).")]
ConsoleOpt = Annotated[bool, typer.Option("--console/--no-console", help="Start the operator console for handoffs.")]
PortOpt = Annotated[int, typer.Option("--console-port", help="Operator console port.")]
EscalationOpt = Annotated[
    float, typer.Option("--escalation-timeout", help="Seconds to wait for an operator before aborting.")
]


def _workspace(root: Path) -> Workspace:
    try:
        return Workspace.load(root.resolve())
    except (WorkspaceError, FileNotFoundError) as exc:
        console.print(f"[red]workspace error:[/] {exc}")
        raise typer.Exit(1) from exc


def _resolve_capability(ws: Workspace, ref: str) -> Capability:
    return _resolve_entry(ws, ref)[0]


def _resolve_entry(ws: Workspace, ref: str) -> tuple[Capability, Path]:
    """A capability by file path or by catalog ``name[@version]``."""
    path = Path(ref)
    if path.suffix in (".yaml", ".yml"):
        return _load_document(Capability, path), path
    name, _, version = ref.partition("@")
    try:
        entry = ws.find(name, version or None)
    except WorkspaceError as exc:
        console.print(f"[red]{exc}[/]")
        raise typer.Exit(1) from exc
    return entry.capability, entry.path


def _load_document[M: BaseModel](model: type[M], path: Path) -> M:
    try:
        return load_as(model, path)
    except FileNotFoundError:
        console.print(f"[red]no such file:[/] {path}")
    except ValidationError as exc:
        console.print(f"[red]{path} is not a valid {model.__name__}:[/]\n{exc}")
    except yaml.YAMLError as exc:
        console.print(f"[red]{path} is not valid YAML:[/] {exc}")
    raise typer.Exit(1)


def _parse_params(params: list[str]) -> dict[str, str]:
    values: dict[str, str] = {}
    for item in params:
        key, sep, value = item.partition("=")
        if not sep:
            raise typer.BadParameter(f"expected name=value, got {item!r}")
        values[key.strip()] = value
    return values


def _print_result(result: RunResult) -> None:
    colour = {RunStatus.SUCCEEDED: "green", RunStatus.BUSINESS_OUTCOME: "yellow", RunStatus.FAILED: "red"}
    console.rule(f"[bold {colour[result.status]}]{result.status.upper()}[/] {result.capability} on {result.tenant}")
    table = Table(show_header=False, box=None)
    _row(table, "run", result.run_id)
    _row(table, "duration", f"{result.duration_ms} ms")
    if result.app_version:
        _row(table, "app version", result.app_version)
    for name, value in result.outputs.items():
        _row(table, f"output.{name}", str(value))
    if result.outcome:
        _row(table, "outcome", f"{result.outcome.code}: {result.outcome.message or result.outcome.description}")
    if result.failure:
        f = result.failure
        _row(table, "failure", f"{f.code} ({f.category}, retryable={f.retryable})")
        _row(table, "message", f.message)
        if f.step_id:
            _row(table, "step", f"#{f.step_index} {f.step_id} - {f.step_title}")
        if f.expected:
            _row(table, "expected", f.expected)
        if f.observed:
            _row(table, "observed", f.observed[:400])
        for attempt in f.locator_attempts:
            _row(table, "  locator", f"{attempt.description} -> {attempt.matches}")
        for key, value in f.evidence.items():
            _row(table, f"  evidence.{key}", str(value))
    for rec in result.recoveries:
        _row(table, "recovery", f"{rec.handler} @ {rec.step_id} (attempt {rec.attempt}): {rec.detail}")
    for drift in result.drift:
        _row(table, "drift", f"{drift.kind} @ {drift.step_id}: {drift.detail[:160]}")
    for iv in result.interventions:
        _row(
            table,
            "intervention",
            f"{iv.id} {iv.kind} -> {iv.resolution} by {iv.operator} "
            f"({iv.human_actions} human actions, waited {iv.waited_ms} ms)",
        )
    _row(table, "evidence", result.evidence_dir or "")
    console.print(table)


def _row(table: Table, key: str, value: str) -> None:
    table.add_row(key, escape(value))


def _exit_code(status: RunStatus) -> int:
    return {RunStatus.SUCCEEDED: 0, RunStatus.BUSINESS_OUTCOME: 2, RunStatus.FAILED: 1}[status]


# --------------------------------------------------------------------------------------- commands


@app.command()
def discover(
    request_path: Annotated[Path, typer.Argument(help="DiscoveryRequest YAML (goal + contract).")],
    tenant: TenantOpt = "cu-alpha",
    workspace: WorkspaceOpt = Path(),
    provider: Annotated[
        str | None,
        typer.Option(
            help="ollama | anthropic | openai | scripted (offline: reference plan for the mock core). Default: env."
        ),
    ] = None,
    model: Annotated[str | None, typer.Option(help="Model id (default: CUA_LLM_MODEL or provider default)")] = None,
    headed: HeadedOpt = False,
    console_on: ConsoleOpt = True,
    console_port: PortOpt = 8765,
    escalation_timeout: EscalationOpt = 900,
    screenshots_to_llm: Annotated[bool, typer.Option(help="Also send (masked) screenshots to the model.")] = False,
    out_dir: Annotated[Path | None, typer.Option("--out", help="Where to write the capability YAML.")] = None,
) -> None:
    """Run the LLM agent on a goal against a live app and record a capability artifact."""
    from cua import service

    ws = _workspace(workspace)
    request = _load_document(DiscoveryRequest, request_path)
    try:
        llm = _scripted_client(request) if provider == "scripted" else client_from_env(provider, model)
    except LLMError as exc:
        console.print(f"[red]{exc}[/]")
        raise typer.Exit(1) from exc
    console.print(f"[bold]discovery[/] {request.metadata.name} on {tenant} with {llm.provider}:{llm.model}")
    live = LiveOptions(
        headless=not headed,
        console=console_on,
        console_port=console_port,
        intervention_timeout_s=escalation_timeout,
    )
    outcome = asyncio.run(
        service.discover(
            ws, request, tenant=tenant, llm=llm, live=live, artifact_dir=out_dir, send_screenshots=screenshots_to_llm
        )
    )
    colour = "green" if outcome.status == "completed" else "red"
    console.rule(f"[bold {colour}]{outcome.status.upper()}[/] after {outcome.turns} turns")
    console.print(f"reason:   {outcome.reason}\nrun:      {outcome.run_id}\nusage:    {outcome.usage}")
    if outcome.artifact_path:
        console.print(f"artifact: [bold]{portable_path(outcome.artifact_path)}[/]")
    raise typer.Exit(0 if outcome.status == "completed" else 1)


def _scripted_client(request: DiscoveryRequest) -> LLMClient:
    from mock_corebank.plans import PLANS

    plan = PLANS.get(request.metadata.name)
    if plan is None:
        raise LLMError(f"no scripted plan for '{request.metadata.name}' (available: {', '.join(PLANS)})")
    return ScriptedClient(plan, model="reference-plan")


@app.command()
def replay(
    capability: Annotated[str, typer.Argument(help="Capability YAML path or catalog name[@version].")],
    tenant: TenantOpt = "cu-alpha",
    param: Annotated[list[str] | None, typer.Option("--param", "-p", help="Input as name=value (repeatable).")] = None,
    workspace: WorkspaceOpt = Path(),
    on_failure: Annotated[str, typer.Option(help="fail | escalate (to a human via the console)")] = "fail",
    headed: HeadedOpt = False,
    console_on: Annotated[
        bool | None, typer.Option("--console/--no-console", help="Operator console (default: on when escalating).")
    ] = None,
    console_port: PortOpt = 8765,
    escalation_timeout: EscalationOpt = 900,
    repeat: Annotated[int, typer.Option(help="Replay N times and report stability.")] = 1,
    as_json: Annotated[bool, typer.Option("--json", help="Print the result contract as JSON on stdout.")] = False,
) -> None:
    """Deterministically replay a capability (no LLM) with typed inputs."""
    from cua import service

    if on_failure not in ("fail", "escalate"):
        raise typer.BadParameter("--on-failure must be 'fail' or 'escalate'")
    ws = _workspace(workspace)
    cap = _resolve_capability(ws, capability)
    inputs = _parse_params(param or [])
    options = ReplayOptions(on_failure=on_failure)  # type: ignore[arg-type]
    live = LiveOptions(
        headless=not headed,
        console=console_on if console_on is not None else on_failure == "escalate",
        console_port=console_port,
        intervention_timeout_s=escalation_timeout,
    )
    results: list[RunResult] = []
    for _ in range(max(1, repeat)):
        result = asyncio.run(service.replay(ws, cap, tenant=tenant, inputs=inputs, options=options, live=live))
        results.append(result)
        if as_json:
            out.print_json(json.dumps(result.model_dump(mode="json", exclude_none=True)))
        else:
            _print_result(result)
    if repeat > 1:
        _print_stability(results)
    raise typer.Exit(_exit_code(results[-1].status))


def _print_stability(results: list[RunResult]) -> None:
    statuses = Counter(r.status.value for r in results)
    redactor = Redactor()
    fingerprints = Counter(
        json.dumps({k: redactor.fingerprint(v) for k, v in sorted(r.outputs.items())})
        for r in results
        if r.status is RunStatus.SUCCEEDED
    )
    fallbacks = sum(1 for r in results for d in r.drift if d.kind == "locator_fallback")
    recoveries = sum(len(r.recoveries) for r in results)
    durations = sorted(r.duration_ms for r in results)
    console.rule("[bold]stability")
    console.print(
        f"runs={len(results)} statuses={dict(statuses)} distinct_outputs={len(fingerprints)} "
        f"locator_fallbacks={fallbacks} recoveries={recoveries} "
        f"p50={durations[len(durations) // 2]}ms max={durations[-1]}ms"
    )


@app.command()
def invoke(
    name: Annotated[str, typer.Argument(help="Capability tool name (dots or double underscores).")],
    args: Annotated[str, typer.Option("--args", help="JSON object of inputs.")] = "{}",
    tenant: TenantOpt = "cu-alpha",
    workspace: WorkspaceOpt = Path(),
) -> None:
    """Agent-facing invocation: look up a capability by tool name, call it with JSON args, print JSON."""
    from cua import service

    ws = _workspace(workspace)
    cap = _resolve_capability(ws, name.replace("__", "."))
    try:
        inputs = json.loads(args)
    except json.JSONDecodeError as exc:
        raise typer.BadParameter(f"--args must be JSON: {exc}") from exc
    result = asyncio.run(service.replay(ws, cap, tenant=tenant, inputs=inputs))
    out.print_json(json.dumps(result.model_dump(mode="json", exclude_none=True)))
    raise typer.Exit(_exit_code(result.status))


@app.command()
def catalog(
    workspace: WorkspaceOpt = Path(),
    tools: Annotated[bool, typer.Option("--tools", help="Emit function-calling tool definitions (JSON).")] = False,
    approved_only: Annotated[bool, typer.Option(help="Only capabilities approved for unattended use.")] = False,
) -> None:
    """List capabilities, or export them as an agent tool catalog."""
    from cua import service

    ws = _workspace(workspace)
    if tools:
        out.print_json(json.dumps(service.catalog_tools(ws, approved_only=approved_only)))
        return
    table = Table("capability", "status", "risk", "inputs -> outputs")
    for entry in ws.catalog():
        c = entry.capability
        table.add_row(
            c.ref,
            c.metadata.status.value,
            c.max_risk.value,
            f"{', '.join(i.name for i in c.spec.inputs)} -> {', '.join(o.name for o in c.spec.outputs)}",
        )
    out.print(table)


@app.command()
def validate(
    paths: Annotated[list[Path], typer.Argument(help="Documents or directories (searched for *.yaml) to validate.")],
    workspace: WorkspaceOpt = Path(),
) -> None:
    """Schema-validate and lint artifacts (capabilities, app profiles, tenants, policy)."""
    ws = _workspace(workspace)
    failed = False
    files = [f for p in paths for f in (sorted(p.rglob("*.yaml")) if p.is_dir() else [p])]
    for path in files:
        try:
            raw = load_yaml(path)
            kind = raw.get("kind") if isinstance(raw, dict) else None
            model = _KINDS.get(str(kind))
            if model is None:
                raise ValueError(f"unknown kind {kind!r}")
            doc = model.model_validate(raw)
        except Exception as exc:
            failed = True
            console.print(f"[red]✗ {path}[/]\n{exc}")
            continue
        findings = []
        if isinstance(doc, Capability):
            tenant = next(iter(ws.tenants.values()), None)
            policy = ws.policy_for(ws.tenant_app(tenant, doc.metadata.app)) if tenant else None
            findings = lint(doc, policy=policy, redactor=Redactor())
        errors = [f for f in findings if f.level == "error"]
        failed = failed or bool(errors)
        console.print(f"[{'red' if errors else 'green'}]{'✗' if errors else '✓'} {path}[/] ({kind})")
        for f in findings:
            colour = {"error": "red", "warning": "yellow", "info": "dim"}[f.level]
            console.print(f"  [{colour}]{f.level:7s}[/] {f.where}: {f.message}")
    raise typer.Exit(1 if failed else 0)


@app.command()
def approve(
    capability: Annotated[str, typer.Argument(help="Capability YAML path or catalog name[@version].")],
    by: Annotated[str, typer.Option(help="Approver identity.")],
    note: Annotated[str | None, typer.Option(help="Review note.")] = None,
    workspace: WorkspaceOpt = Path(),
) -> None:
    """Approve a reviewed capability for unattended replay (binds the approval to its digest)."""
    cap, path = _resolve_entry(_workspace(workspace), capability)
    errors = [f for f in lint(cap, redactor=Redactor()) if f.level == "error" and f.where != "metadata.approvals"]
    if errors:
        for f in errors:
            console.print(f"[red]{f.where}: {f.message}[/]")
        raise typer.Exit(1)
    digest = cap.compute_digest()
    approval = Approval(by=by, at=datetime.now(UTC).replace(microsecond=0), digest=digest, note=note)
    approved = cap.model_copy(
        update={
            "metadata": cap.metadata.model_copy(
                update={"status": Status.APPROVED, "approvals": [*cap.metadata.approvals, approval], "digest": digest}
            )
        }
    )
    save(approved, path, header=f"Approved by {by} for digest {digest[:19]}…")
    console.print(f"[green]approved[/] {approved.ref} ({digest})")


@app.command()
def show(
    capability: Annotated[str, typer.Argument(help="Capability YAML path or catalog name[@version].")],
    workspace: WorkspaceOpt = Path(),
) -> None:
    """Human-readable review summary of a capability: the view a reviewer approves."""
    cap, path = _resolve_entry(_workspace(workspace), capability)
    meta = cap.metadata
    out.rule(f"[bold]{escape(cap.ref)}[/] [{'green' if cap.is_approved() else 'yellow'}]{meta.status}[/]")
    out.print(f"{escape(meta.title)}\n[dim]{escape(meta.description)}[/]")
    out.print(f"[dim]file[/] {path}   [dim]app[/] {meta.app} {meta.app_versions}   [dim]max risk[/] {cap.max_risk}")
    if meta.provenance:
        prov = meta.provenance
        out.print(f"[dim]recorded[/] {prov.method} run {prov.run_id} on {prov.recorded_on_tenant} ({prov.model})")
    out.print(f"[dim]digest[/] {meta.digest}")
    out.print("\n[bold]contract[/]")
    for i in cap.spec.inputs:
        out.print(f"  in   {i.name}: {i.type} \\[{i.sensitivity}]  [dim]{escape(i.description)}[/]")
    for o in cap.spec.outputs:
        out.print(f"  out  {o.name}: {o.type} \\[{o.sensitivity}]  [dim]{escape(o.description)}[/]")
    for oc in cap.spec.outcomes:
        out.print(f"  outcome  {oc.code}  [dim]{escape(oc.description)}[/]")
    out.print("\n[bold]steps[/]")
    risk_colour = {"read": "dim", "write": "yellow", "irreversible": "bold red"}
    for n, step in enumerate(cap.spec.steps, 1):
        out.print(f"  {n:>2}. [{risk_colour[step.risk]}]{step.risk:<12}[/] {escape(step.title)}  [dim]({step.id})[/]")
        target = getattr(step, "target", None)
        if target is not None:
            where = escape(f" in {target.frame.describe()}") if target.frame else ""
            out.print(
                f"      [dim]target{where}:[/] "
                + escape("  >  ".join(_short(loc.describe()) for loc in target.locators))
            )
        value = getattr(step, "value", None) or getattr(step, "option", None)
        if value is not None:
            out.print(f"      [dim]value:[/] {escape(value.describe())}")
        if step.expect is not None:
            out.print(f"      [dim]expect:[/] {escape(step.expect.describe())}")
    out.print(f"\n[bold]success[/] {escape(cap.spec.success.describe())}")


def _short(text: str, limit: int = 48) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


@app.command()
def schema(out_dir: Annotated[Path, typer.Option("--out")] = Path("schemas")) -> None:
    """Export JSON Schemas for all artifact kinds (for editors, CI and reviewers)."""
    out_dir.mkdir(parents=True, exist_ok=True)
    for kind, model in _KINDS.items():
        name = re.sub(r"(?<!^)(?=[A-Z])", "-", kind).lower()
        path = out_dir / f"{name}.schema.json"
        path.write_text(json.dumps(model.model_json_schema(by_alias=True), indent=2) + "\n", encoding="utf-8")
        console.print(f"wrote {path}")


@app.command(hidden=True)
def dump(path: Path) -> None:
    """Re-serialise a capability (normalises formatting)."""
    out.print(to_yaml(_load_document(Capability, path)), end="")


def main() -> None:
    load_dotenv(os.environ.get("CUA_ENV_FILE", ".env"))
    app()
