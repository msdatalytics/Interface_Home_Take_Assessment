# cua - computer-use automation for legacy back-office apps

An LLM agent **discovers** how to perform a business task on a real UI, records it as a typed,
versioned, reviewable **capability artifact**, and a **deterministic replay engine** (no LLM in
the loop) executes it thereafter - with a strict error taxonomy, recovery, guardrails, evidence,
and a human-in-the-loop handoff on the *same live session*.

The target is a deliberately hostile "legacy core banking" web app (`mock_corebank`): a
frameset shell, table layouts, labels that are not `<label>`s, ASP.NET-style control names and
postbacks, no test ids, session expiry, maintenance interstitials, runtime error pages and
modal dialogs - served for **two tenants** that run the same product configured differently.

```
DiscoveryRequest ──► LLM agent (observe → decide → act) ──► Capability YAML ──► review / approve
 (goal + contract)        │ redacted semantic screen            (steps, ranked             │
                          │ verified locators                    verified locators,        ▼
                          ▼ policy + approvals                   checkpoints, I/O)   Replay engine (no LLM)
                     operator console  ◄──────── escalation / handoff ───────────── result contract + evidence
```

Design write-up: **[REPORT.md](REPORT.md)** · Evidence: **[evidence/](evidence/README.md)**

## Setup

Requirements: Python 3.12+, [uv](https://docs.astral.sh/uv/), and network access to install Chromium once.

```bash
make setup          # uv sync + playwright install chromium + .env from .env.example
```

### LLM and keys

Only discovery uses an LLM; replay never does. The default is a **free local model** via
[Ollama](https://ollama.com) - no key, nothing leaves the machine:

```bash
brew install ollama && ollama serve        # or the desktop app
ollama pull qwen3:8b                       # ~5 GB; runs on a 16 GB laptop
```

Cloud models are faster and stronger; select one in `.env` (git-ignored):

| variable | purpose |
|---|---|
| `CUA_LLM_PROVIDER` | `ollama` (default) · `anthropic` · `openai` |
| `CUA_LLM_MODEL` | model override (defaults: `qwen3:8b`, `claude-sonnet-5-5`, `gpt-4.1`) |
| `CUA_OLLAMA_THINK` | `1` enables the local model's reasoning mode (slower turns, far more reliable on multi-screen flows) |
| `ANTHROPIC_API_KEY` / `OPENAI_API_KEY` | key for the chosen cloud provider (API credits, separate from chat subscriptions) |
| `CU_ALPHA_COREBANK_USERNAME/PASSWORD`, `CU_BETA_…` | synthetic teller credentials of the mock (defaults in `.env.example`) |
| `CUA_FINGERPRINT_KEY` | optional HMAC key for fingerprints of confidential outputs (auto-generated otherwise) |

No real credentials, customers or PII exist anywhere in this repository; all data is synthetic.

## Demo: discover, then replay

```bash
make mock                       # terminal 1: mock core - cu-alpha :8401, cu-beta :8402

# terminal 2
uv run cua discover requests/get_savings_balance.yaml --tenant cu-alpha    # LLM discovery
uv run cua show corebank.member.get_savings_balance                         # review the artifact
uv run cua replay corebank.member.get_savings_balance -p member_id=100234   # deterministic replay
uv run cua replay corebank.member.get_savings_balance -t cu-beta -p member_id=200145   # other tenant
```

Typical durations on an M4 / 16 GB: balance lookup discovery ~1-3 min and the 4-screen account
opening ~8 min with local `qwen3:8b` (reasoning mode); ~1-2 min with a cloud model; replay 3-4 s.
Escalations wait for an operator for `--escalation-timeout` seconds (default 900), then abort cleanly.

`cua replay` exit codes are scriptable: `0` succeeded · `2` business outcome · `1` failed. Add
`--json` for the machine-readable result contract, `--repeat 10` for a stability report, and
`--on-failure escalate` to route failures to a human instead of failing.

### Error replays and recovery

The mock exposes fault injection (`mock-corebank fault <tenant> <kind> [--path …] [--count N]`):

```bash
uv run cua replay corebank.member.get_savings_balance -p member_id=999999   # business outcome: record_not_found (exit 2)
uv run cua replay corebank.member.get_savings_balance -p member_id=12AB     # INPUT_INVALID, app never touched (exit 1)
uv run mock-corebank fault cu-alpha maintenance_notice --path mbr_detail    # recovered: interstitial dismissed
uv run mock-corebank fault cu-alpha session_expired --path mbr_detail       # recovered: re-login + safe restart
uv run mock-corebank fault cu-alpha server_error --path mbr_detail --count 5  # APP_ERROR with screenshot + DOM
make demo-errors                                                            # all of the above
```

### Human-in-the-loop handoff

```bash
uv run mock-corebank fault cu-alpha unknown_dialog --path mbr_search
uv run cua replay corebank.member.get_savings_balance -p member_id=100234 --on-failure escalate
# -> prints an intervention URL. Open http://127.0.0.1:8765, "Take control", click
#    "Continue Session" in the live screen, then "Retry step". Automation resumes.
```

`make demo-handoff` does the same with `scripts/operator_bot.py` playing the operator over the
console's HTTP API. The console also handles discovery escalations (stuck agent, approvals).

### Irreversible actions and approval

```bash
uv run cua discover requests/open_share_account.yaml          # pauses for approval before "Submit"
uv run cua replay corebank.member.open_share_account -p member_id=100591 -p share_type="Holiday Club" \
    -p nickname="Ski Trip" -p initial_deposit=75               # -> APPROVAL_REQUIRED (draft artifact)
uv run cua approve corebank.member.open_share_account --by reviewer.sam --note "reviewed"
uv run cua replay corebank.member.open_share_account …         # runs unattended; any later edit revokes approval
```

### Regenerating the evidence

`make evidence` (with `make mock` running) replays every scenario above through the CLI and
rebuilds [`evidence/`](evidence/README.md); `make evidence ARGS=--discover` runs LLM discovery first.

### Agent-facing catalog

```bash
uv run cua catalog                       # human view
uv run cua catalog --tools               # JSON-Schema function definitions for an agent
uv run cua invoke corebank__member__get_savings_balance --args '{"member_id": "100234"}'
```

## Running without live services

* **No LLM key:** discovery runs on a local Ollama model (above). The committed artifacts in
  `capabilities/` were recorded by a real model (`qwen3:8b`, see `evidence/discovery/`), replay
  never calls a model, and `cua discover … --provider scripted` (or `make discover-offline`) runs
  the whole discovery pipeline with no model at all, a reference plan standing in for the LLM.
* **No external app:** the target is the bundled mock (`make mock`); the test-suite starts its
  own instance on free ports.
* **Tests:** `make test` (unit + browser end-to-end, ~2 min), `make check` (lint, mypy --strict, tests).

## Repository map

| path | contents |
|---|---|
| `src/cua/artifact/` | artifact schema (Pydantic, versioned `cua.dev/v1`): capability, locators, steps, conditions, app profile, tenant, contract, lint, JSON-patch |
| `src/cua/surface/` | `Surface` abstraction + Playwright implementation; in-page observer, custom selector engines, masking |
| `src/cua/discovery/` | LLM agent loop, provider clients, prompts, flow recorder |
| `src/cua/replay/` | deterministic replay engine |
| `src/cua/runtime/` | step executor (waits, state monitor), session manager, result contract |
| `src/cua/safety/` | policy (allowlist, risk, approvals), redaction, secret vault |
| `src/cua/hitl/` | control lease, intervention desk, operator console |
| `src/mock_corebank/` | the hostile legacy app (2 tenants, fault injection) + reference plans |
| `config/` | policy, app profile (vendor product knowledge), tenants (URL, secrets, vocabulary, overrides) |
| `requests/` | discovery requests: goal + caller contract, owned by humans |
| `capabilities/` | recorded capability artifacts (the catalog) |
| `evidence/` | curated discovery/replay runs: logs, LLM turns, screenshots, results |
| `schemas/` | exported JSON Schemas (`cua schema`) |
