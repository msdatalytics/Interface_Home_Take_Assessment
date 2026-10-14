# Computer-use automation - design report

## 1. Architecture

Two loops share one execution core. **Discovery** (LLM in the loop) turns a human-owned
`DiscoveryRequest` (goal + typed contract) into a `Capability`. **Replay** (no LLM) executes a
capability with typed inputs and returns a typed result. Both drive the app through the same
`Surface` interface and the same `StepExecutor`, so discovery exercises exactly the code path
replay will run.

* **Surface** (`surface/`): `observe / state / resolve / act / evidence`. The Playwright
  implementation injects an observer into *every* frame (framesets included) that builds a
  compact semantic view (roles, accessible names, *visual* labels for unlabeled table layouts,
  table coordinates, key/value pairs) and three custom selector engines (`cua-label`,
  `cua-cell`, `cua-value`) so locators speak the operator's language. A desktop/Citrix surface
  would implement the same interface over UIA or OCR; nothing above it is browser-specific.
* **Agent** (`discovery/agent.py`): stateless per turn - each call carries goal, contract,
  history and the current redacted screen, and must return exactly one tool call
  (`click/fill/select_option/press_enter/extract/finish/request_human/give_up`). Every
  proposal is validated (ref exists, input declared, constant not sensitive), risk-classified
  and policy-checked *before* acting. The action is performed through freshly synthesised,
  **verified** locators - the ones that get stored. Actions that make the app report a business
  outcome (e.g. Search before the field is filled) are *dead ends*: reported to the model, never
  recorded, so the artifact holds only the happy path. Providers: local Ollama (default; the
  committed artifacts were discovered by `qwen3:8b` on a laptop), Anthropic, OpenAI.
* **Recorder**: derives each step's post-condition from what the action changed (frame URL +
  static heading, or newly-appeared static text for in-place postbacks), copies the app's
  business outcomes into the artifact, rejects artifacts that would persist sensitive data,
  and stamps provenance + a content digest.
* **Replay engine** (`replay/engine.py`): validate inputs → preflight policy/approval for every
  step → ensure session → check app version → execute steps, racing every wait against a
  **state monitor** (session expired, transient error, interstitial, business outcome) →
  verify final checkpoint → typed outputs.
* **HITL** (`hitl/`): a control lease with a fencing epoch, an intervention desk (routing,
  claim/resolve, audit), and an operator console served from the *same process* and browser
  context, so the human acts on the live session automation was using.

## 2. Artifact schema

Kubernetes-style documents (`apiVersion: cua.dev/v1`, `kind`, `metadata`, `spec`), Pydantic
models with `extra=forbid`, exported as JSON Schema (`schemas/`), linted by `cua validate`.

* **Capability** - `metadata`: name, semver, app + supported `app_versions`, status
  (draft/approved/deprecated), approvals bound to a digest, provenance (run id, tenant, app
  version, model, human-assisted). `spec`: typed `inputs` (type, pattern, enum, sensitivity),
  typed `outputs` (with sensitivity), declared business `outcomes`, `steps`, `success`
  checkpoint. Referential integrity is validated (params, outputs, unique ids).
* **Step** (discriminated union `navigate|click|fill|select|press|extract`): `id`, `title`,
  `risk` (read/write/irreversible), `target`, value expression (`{param}`, `{secret}`,
  `{literal}` - never raw caller data), `expect` checkpoint, timeout, `source` (llm/human/author)
  and the model's `rationale`.
* **Target**: a frame reference plus an ordered list of locators, each verified at record time
  to resolve *uniquely to the acted-on element*, ranked by robustness: `role+name` > `label`
  (layout-aware) > `table_cell(row key, column)` > `label_value` > `text` > `css` (last resort).
  Replay uses the first unique match; using a fallback succeeds but is reported as **drift**.
* **Checkpoints**: `text_visible` (literal or regex with a captured `message`),
  `element_visible`, `url_matches`, `all_of`, `any_of` - all frame-scoped.
* **AppProfile** (vendor product knowledge, shared): session entry/login steps, state handlers
  (session_expired, transient_error with reload/backoff, interstitial with steps, fatal), business
  outcomes, version probe. **Tenant**: base URL, expected version, secret mapping, vocabulary,
  overrides. **Policy**: allowlist, risk keywords, per-mode rules, redaction.

## 3. Determinism & error handling

Replay is a pure function of (artifact, profile, tenant, inputs, app state). No LLM, no retries
of unknown state, no "best guess" clicks: ambiguity (0 or >1 matches for every locator) is a
failure, not a choice. Every wait is a poll that, on each tick, evaluates the expected
checkpoint **and** the state monitor, so an error page or expiry is recognised immediately
instead of after a timeout. The result contract has three statuses (CLI exit 0/2/1):

* **`business_outcome`** - a legitimate answer the caller must handle: `record_not_found`,
  `access_denied`, `validation_error` (with the app's message), `duplicate_request`.
* **Recoverable conditions** - handled in place and recorded in `recoveries`: interstitials
  (run handler steps, resume same step), transient errors (backoff + frame reload, bounded),
  session expiry (re-login and **restart from step 1** - only if nothing state-changing has
  committed), slowness (network-quiet settling + generous checkpoint timeouts).
* **`failed`** with a category (`caller`, `policy`, `application`, `automation`, `operator`,
  `internal`), a stable code (`INPUT_INVALID`, `APPROVAL_REQUIRED`, `TARGET_NOT_FOUND`,
  `TARGET_AMBIGUOUS`, `CHECKPOINT_FAILED`, `APP_ERROR`, `OUTCOME_UNKNOWN`, …), `retryable`,
  expected-vs-observed, locator attempts, and evidence (masked screenshot, masked per-frame DOM,
  redacted semantic observation). Crucially, an error *after* a state-changing step returns
  `OUTCOME_UNKNOWN, retryable=false` - the system never blindly resubmits a possibly-committed
  transaction.

Evidence for every run lives in `runs/<id>/`: `events.jsonl` (redacted, sequenced), per-step
screenshots, `result.json` (confidential outputs stored as HMAC fingerprints), interventions.

## 4. Heterogeneity & multi-tenant

Three layers, from most to least shared: **capability** (one per business task, versioned),
**app profile** (one per vendor product: login, handlers, outcomes, version probe), **tenant**
(one per institution: URL, credentials, `expected_version`, vocabulary, overrides).

* **Vocabulary** maps canonical UI terms to a tenant's labels ("Member Number" → "Account
  Number", "Balance" → "Current Balance") and is applied inside locator resolution, so one
  artifact serves relabelled installations unchanged.
* **Overrides** are small RFC-6902 patches addressed by step id (`/spec/steps/@click_link_view`),
  e.g. cu-beta auto-opens single search results so the "View" hop is removed. Overrides may
  change *how* a capability runs, never its contract (enforced), and are reported in each result.
* **Versions**: capabilities declare `app_versions`; the profile probes the running version;
  out-of-range versions fail fast (`VERSION_UNSUPPORTED`); a version that differs from the
  tenant's `expected_version` (e.g. an unannounced upgrade) runs but is reported as drift.
* **Desktop/Citrix**: the `Surface` interface and locator vocabulary (role, label, cell, text)
  map onto UIA/AX trees; for pixel-only Citrix the same contract would be backed by OCR +
  template locators, with the semantic layer unchanged.
* **Scale**: the catalog is plain files (`capabilities/<app>/<name>.yaml`) keyed by name+semver;
  CI validates every artifact, so onboarding tenant N+1 is config, not re-recording.

## 5. Escalation & handoff

Escalation triggers: discovery stuck (same action on an unchanged screen ×3, three consecutive
failed actions, step budget), agent `request_human`, approval of an irreversible action, and -
with `--on-failure escalate` - any replay failure that is not a business outcome. The desk
records an intervention (reason, step, expected state, masked screenshot + observation),
routes it (console log, optional webhook to a queue/pager), and waits with a timeout.

Handoff is a **lease transfer**, not a screenshot exchange: the operator claims the
intervention, which moves the `ControlChannel` to `human` and bumps its epoch; automation
re-checks the epoch before every action, so a stale coroutine can never act on a session a
human holds. The operator works in the live browser context (console screen + click/type, or
headed mode); their interactions are captured in-page and logged (sensitive values masked).
Resolutions: `retry_step`, `step_done` (automation **verifies** the step's checkpoint rather
than trusting the human), `approve/reject`, `continue` (discovery: captured human actions are
converted into `source: human` steps and the agent is told what happened), `abort`. Timeouts
fail with `ESCALATION_TIMEOUT`.

## 6. Safety

* **Allowlist** at the network layer: every request outside the tenant origin/allowed paths
  (or on denied paths such as `/__admin/*`, `logoff.asp`) is aborted and logged; actions are
  restricted to an allowlisted set.
* **Risk** = max(declared, keyword-inferred from the control's label), so neither the model nor
  a hand-edited artifact can downgrade "Submit". Discovery requires live human approval for
  irreversible actions; replay requires an **approved artifact whose approval is bound to its
  content digest** (any edit revokes it) or a just-in-time operator approval; otherwise it fails
  at preflight before touching the app.
* **Redaction**: secrets and sensitive inputs are registered as known values; patterns catch
  SSNs, Luhn-valid cards, e-mails, phones, tokens, long account numbers; label-aware masking
  hides values next to "Name", "SSN", "Date of Birth" in the model's view, screenshots (CSS
  blur), DOM snapshots and logs. The model never sees credentials or caller values (it refers to
  inputs by name), never receives screenshots by default, and artifacts are leak-checked.
* **Least privilege at rest**: confidential outputs are persisted as keyed fingerprints; secrets
  are resolved from a vault interface (env-backed here) at execution time only.

## 7. Cuts

* **Desktop/Citrix surface** - designed for (interface + locator vocabulary), not implemented.
* **Persistent intervention queue / auth** - the desk is in-process with a webhook router; a
  real deployment needs a durable queue, SSO for operators and per-role authorisation.
* **Secret manager** - `EnvVault` stands in for Vault/KMS.
* **Self-healing** - drift is detected and reported, but locator repair is a human review step
  (re-discovery or edit + re-approval), deliberately not automatic.
* **Parallelism & scheduling** - one session per run; no pool, rate limiting or session reuse.
* **Visual grounding** - the agent reads the semantic DOM view; screenshot-to-LLM exists but is
  off by default for privacy and not tuned.
* **Model strength** - evidence was recorded with a free 8B local model. It is slower (~8 min for
  the 4-screen flow) and over-cautious: it flagged "Continue" (review page) as committing, so the
  artifact asks approval twice. Risk can only be *raised* by the model, never lowered - a reviewer
  would downgrade that step before approving. A cloud model removes both issues.
