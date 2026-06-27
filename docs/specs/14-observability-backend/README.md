---
title: Observability backend
slug: observability-backend
area: observability
tier: Horizon
size: M
status: Backlog
depends_on: []
issue:        # set to the GitHub issue number when created
---

# Observability backend

> **Area** `observability` · **Tier** `Horizon` · **Size** `M` · **Status** `Backlog` · **Depends on:** —

## Summary

The app already emits OpenTelemetry GenAI spans through the `app/observability.py`
seam and exports them over OTLP whenever `OTEL_EXPORTER_OTLP_ENDPOINT` is set
(`agent.run` → `retrieve` → `generate`, plus `evals.run`). What is missing is the
*other end of the wire*: a concrete, reproducible backend to receive those spans,
a fix so short-lived CLI/eval runs actually flush their spans before exit, and a
durable two-way link between an eval result and the trace that produced it. This
spec wires one default backend (Langfuse, self-hostable, OTLP-native) into the
compose stack, persists eval results keyed by `trace_id`, and makes a stored eval
result navigable to its backend trace and back. Online eval sampling from live
traffic is explicitly **out of scope** here (see Non-goals / follow-up).

## Problem / Motivation

Spans are emitted but go nowhere by default, and evals and traces aren't linked.
Concretely, today:

- There is **no backend service** in `docker-compose.yml`, so `make eval` /
  `make ask` produce spans that are dropped unless an operator manually stands up
  an OTLP collector. The "config a backend" story is undemonstrated.
- The eval gate (`app/evals.py`) returns an in-memory dict and **persists
  nothing**; there is no `eval_results` table and no `trace_id` captured, so an
  eval score cannot be tied to the trace that generated it.
- The eval and `ask` entrypoints are **short-lived processes** that exit via
  process end / `sys.exit`. With `BatchSpanProcessor` and no `force_flush()`/
  `shutdown()` on exit, the batch is frequently never exported — the exact path
  whose traces we most want to keep silently loses them.

## Goals

- **Ship one concrete, reproducible backend.** Add a self-hostable, OTLP-native
  backend (default: **Langfuse**) to `docker-compose.yml` so `OTEL_EXPORTER_OTLP_ENDPOINT`
  points at something real on `make up`, with zero app code changes. OTLP keeps
  the choice vendor-neutral; Langfuse is the default only because it self-hosts
  and exposes a scores API for the eval link.
- **Guarantee delivery from short-lived runs.** Register a flush/shutdown
  (`atexit` + explicit `force_flush()` on the eval/CLI exit path) so eval and
  `ask` traces reliably reach the backend.
- **Persist eval results keyed to traces.** Capture the current span's `trace_id`
  in `evals.run`, write results to a new `eval_results` table, and store the
  backend trace URL so a result links *out* to its trace.
- **Make a trace navigable to its eval result.** Push the eval score back onto the
  trace via a thin, optional backend-native adapter (Langfuse scores API) so a
  trace links *in* to its result — clearly isolated behind an interface so the
  OTLP path stays backend-agnostic.

## Non-goals

- Building a custom tracing UI (use the backend's UI).
- **Online evals sampled from live `ask` traffic.** This is a separate, larger
  pipeline (sampling policy, async scoring worker, queue/storage, cost controls)
  that does not fit an `M`. Tracked as a follow-up spec; this spec only links
  *offline* eval-gate results to traces. See Open questions.
- Wiring metrics and logs signals (this spec is traces-only).
- Multi-tenant retention/residency controls — owned by `governance-and-audit`
  and `data-residency`, which depend on this spec.

## Proposed design

Seam: this lives behind the existing `app/observability.py` "beside-path" seam
plus a new persistence step in the eval harness. The hot path (`app/gateway.py`)
is untouched.

**1. Backend service (compose).** Add a backend service to `docker-compose.yml`
on the `sandbox` network, **profile-gated** (e.g. `profiles: ["observability"]`)
so the core four-container stack stays light and only
`docker compose --profile observability up` pulls it in. (The Makefile `up`
target is currently a bare `docker compose up -d --build` with no profile
passthrough, so a plain `make up` will **not** start the backend; either invoke
compose directly with `--profile`, or add a `PROFILE`/`COMPOSE_PROFILES`
passthrough to the `up` target — see acceptance criterion 1.) Set
`OTEL_EXPORTER_OTLP_ENDPOINT` (and any auth headers via
`OTEL_EXPORTER_OTLP_HEADERS`) for the `app` service so traces flow when the
profile is active. Backend remains swappable: any OTLP-compatible collector works
by changing env only.

  **Host-execution caveat (load-bearing for acceptance criterion 1).** `make ask`
  and `make eval` run `uv run python -m ...` as a **host process** (see Makefile),
  *not* `docker compose exec app`, so env set only in the compose `app` service's
  `environment:` block does **not** reach them, and the compose service name
  (`langfuse`) is not resolvable from the host. For host-run traces to export, the
  backend service must **publish its OTLP port to the host** (like `postgres`/
  `litellm` already do), and `OTEL_EXPORTER_OTLP_ENDPOINT` must be set in **`.env`**
  (read on the host via `config.py`'s `load_dotenv()`) pointing at
  `http://localhost:<port>/...` — not the in-network service name. The compose
  `app` service env (service-name endpoint) only matters for `docker compose exec
  app ...` runs. Document both forms in `.env.example`: the localhost endpoint for
  `make` targets, and a commented service-name endpoint for in-container runs. This
  is the actual wiring the "spans appear with only env set" criterion depends on.

  Backend-specific bootstrap is **load-bearing and must be solved in this spec,
  not assumed away**: self-hosted Langfuse v3 is *not* a single container — it
  needs Postgres + ClickHouse + Redis + object storage **plus required secrets**
  (`NEXTAUTH_SECRET`, `SALT`, `ENCRYPTION_KEY`, `CLICKHOUSE_URL`, S3/object-store
  config), and the OTLP endpoint rejects traces until an org/project/API-key pair
  exists. **The observability profile must therefore declare the *whole* v3
  dependency set, not a single `langfuse:` service** — a one-service compose entry
  will fail to boot and criterion 1 ("spans appear with only env set") is then
  unachievable. The illustrative `examples/docker-compose.observability.yaml`
  currently shows only the `langfuse` web service wired to the existing
  `postgres`; expansion must extend it to the full stack (langfuse-web +
  langfuse-worker + ClickHouse + Redis + object store) with all required secrets
  pinned to fixed dev values, **and give Langfuse a dedicated database/schema** so
  its tables never collide with the sandbox `documents` / `eval_results` tables
  (do *not* let it share the `sandbox` DB used by the app). So "spans appear with
  only env set" requires pre-provisioning: use Langfuse's `LANGFUSE_INIT_*` env
  (org/project/user + a fixed `LANGFUSE_INIT_PROJECT_ID` + fixed
  `LANGFUSE_INIT_PROJECT_PUBLIC_KEY` / `LANGFUSE_INIT_PROJECT_SECRET_KEY`) so the
  keys **and the project id** are known ahead of time — the project id is an
  internal id assigned at creation, so without `LANGFUSE_INIT_PROJECT_ID` the
  `trace_url` template's `${PROJECT_ID}` (below) is unknowable until someone opens
  the UI, defeating the zero-click goal. Then
  derive `OTEL_EXPORTER_OTLP_HEADERS=Authorization=Basic <base64(public:secret)>`
  from those same fixed dev keys. Document this exact wiring (keys + derived
  header) in `.env.example`. Note the OTLP-HTTP exporter appends `/v1/traces` to
  `OTEL_EXPORTER_OTLP_ENDPOINT`, so the endpoint must be the collector *base*
  (Langfuse: `.../api/public/otel`), not the full traces path.

  Because of this heft, Phoenix was reconsidered as the default during
  expansion: Phoenix is genuinely single-container and OTLP-native, which makes
  the zero-friction `make up` demo cheaper. **Decision (see Open questions →
  Resolved): keep Langfuse as the default, ship Phoenix as a first-class
  documented alternative.** The deciding factor is that the trace→result
  direction (Goal 4 — a backend trace navigable *back* to its eval score) is a
  *primary* goal of this spec and is Langfuse-only (Phoenix has no scores API);
  the usual counter-argument — "Langfuse is too heavy for CI" — does not apply,
  because CI never stands up the backend at all (the integration tests assert
  against a lightweight in-process / collector OTLP sink, see `testing.md`), and
  the heavy stack is `profiles:`-gated so it only costs operators who opt in.
  Phoenix remains fully supported via env-only swap for the OTLP-only path
  (no scores). The full trade-off table is in [`design.md`](design.md §1); both
  backends' exact compose + `.env` wiring is in [`examples/`](examples/).

**2. Reliable export from short-lived processes.** In `app/observability.py`,
register `_provider.shutdown` via `atexit`, and expose a `flush()` that calls
`_provider.force_flush()`. Call `flush()` in a **`finally` inside `evals.run()`
itself** (not only in `__main__`) so it covers the `__main__`, pytest
(`test_quality_gate` calls `run()` directly, never reaching `__main__`), and any
future programmatic caller in one place; also call it at the end of
`app.agent.__main__`. `force_flush()` is a no-op when no exporter is attached, so
this is safe with the endpoint unset. (atexit `shutdown` is the belt-and-braces
backstop; the in-`run()` flush is what the acceptance test relies on.)

**3. Eval ↔ trace link.**
- **Capture a *distinct per-case* trace_id, not one shared run-level id.** Today
  `evals.run` wraps the whole loop in a single `evals.run` span and each case's
  `ask()` opens `agent.run` as a **child** of it — and `trace_id` is constant
  across an entire trace tree, so naively reading
  `trace.get_current_span().get_span_context().trace_id` in the loop yields the
  *same* `trace_id` for every case. That would make every `eval_results` row carry
  an identical `trace_id`, collapse the result→trace link to one giant trace, and
  make per-case `score_sink` posts ambiguous (N scores on one `traceId`). Fix:
  make **each case its own root trace** — open the per-case span with no parent
  (e.g. start the `agent.run`/a per-case `evals.case` span in a fresh root context
  via `opentelemetry.context` / `start_as_current_span(..., context=...)` so it is
  *not* nested under `evals.run`), and read *that* span's `trace_id` (formatted via
  `opentelemetry.trace.format_trace_id`) inside the loop, attaching it to that
  case's result. Keep a run-level `evals.run` span if desired, but the persisted
  per-case `trace_id` must be the case's own root-trace id. Acceptance must assert
  the captured ids are **distinct across cases**.
- New table `eval_results`:
  `id BIGSERIAL PK, run_at timestamptz default now(), suite text, question text,
  score numeric, passed bool, trace_id text, trace_url text`. Persist one row per
  case. This is the durable result→trace link (operator opens `trace_url`).
  - **Schema creation must be idempotent at runtime, not initdb-only.**
    `db/init.sql` is mounted into `/docker-entrypoint-initdb.d/`, which Postgres
    runs *only on an empty data dir* — appending to it does **not** create the
    table on any existing `pgdata` volume (and `make up` does not wipe it; only
    `make down -v` does). So `evals.run` must `CREATE TABLE IF NOT EXISTS
    eval_results (...)` on its own connection before the first insert (also add it
    to `db/init.sql` for fresh stacks). Reuse the existing `psycopg.connect(
    settings.database_url)` pattern from `app/retrieval.py`.
- Trace→result link via an optional adapter: a `score_sink` interface with a
  Langfuse implementation that posts the score onto the trace via the scores API,
  selected by env (`EVAL_SCORE_SINK=langfuse`), defaulting to a no-op. The OTLP
  span path never imports a vendor SDK; only this opt-in adapter does.
- **`trace_url` construction is backend-specific — keep it out of the neutral
  path.** A Langfuse trace URL is `{host}/project/{projectId}/traces/{trace_id}`,
  which needs the host and project id; hardcoding that in `evals.run` would couple
  the generic persistence path to a vendor, violating the OTLP-neutrality
  invariant. Build `trace_url` from a configurable template
  (`OTEL_TRACE_URL_TEMPLATE`, e.g. `${LANGFUSE_HOST}/project/${PROJECT_ID}/traces/{trace_id}`)
  or via the same `score_sink` adapter; when no template/sink is set, store
  `trace_id` and leave `trace_url` null. The result→trace link then degrades to
  "trace_id only" rather than breaking when the backend is swapped.

**4. PII posture.** The content-bearing span attributes today are
`input.question` (set in `agent.run`, `app/agent.py`) and `retrieval.query` (set
in `retrieve`, `app/retrieval.py`); answer/completion text is **not** currently
captured on any span. Add an `OTEL_CAPTURE_CONTENT` toggle (default on for the
sandbox, documented as "turn off in any environment with real data") gating
capture of those content attributes (and any answer/prompt content added later),
so retention of sensitive text is a deliberate choice — implement it in the
`span()` helper or at each call site so a single switch covers all of them.
**Scope of the toggle (important):** `OTEL_CAPTURE_CONTENT` gates only what is
attached to *spans* (i.e. what is exported to the tracing backend). It does **not**
gate the `question text` column persisted to the local `eval_results` table — that
row is written regardless. This is acceptable because the eval suite is a
**golden-set fixture**, not live user input, and the table is local Postgres, not
a third-party backend; but it means "no content leaves the process" is true for
*span export*, not for the local results table (acceptance criterion 8 is scoped
to span attributes accordingly). If a deployment ever feeds real user data through
the eval harness, gate the `question` column under the same toggle (defer to
`governance-and-audit`). **Third content channel — the `score_sink` comment.**
The trace→result push can carry the question as the score *comment*, which would
send content to the backend even with `OTEL_CAPTURE_CONTENT` off (the score
*value* is not content and is always fine to send). This channel **is** gated by
`OTEL_CAPTURE_CONTENT`: `evals.run` passes `comment=None` when capture is off, so
the toggle covers both span attributes and the sink comment. (The local
`eval_results.question` column remains the one deliberately-ungated channel,
acceptable because it is golden-set fixture text in local Postgres — see above.)
Note the dual
instrumentation boundary: the LiteLLM gateway is a separate process and is **not**
in the app trace; gateway-side cost/token spans are deferred to a follow-up
(LiteLLM OTel callback + W3C trace-context propagation).

Config/schema/API changes: new env vars (`OTEL_EXPORTER_OTLP_HEADERS`,
`EVAL_SCORE_SINK`, `OTEL_CAPTURE_CONTENT`, `OTEL_TRACE_URL_TEMPLATE`, and the
Langfuse `LANGFUSE_INIT_*` bootstrap keys); new `eval_results` table (created
idempotently at runtime *and* in `db/init.sql`); new profile-gated compose
service. No change to `app/gateway.py` or `gateway/litellm_config.yaml`.

## Acceptance criteria

- [ ] Bringing up the observability profile (`docker compose --profile
      observability up`, or `make up` once the `up` target passes the profile
      through) starts the backend **and its full
      dependency set** (for Langfuse v3: langfuse-web + worker + ClickHouse +
      Redis + object store, all required secrets pinned) **and its bootstrap**
      (project + fixed project id + fixed dev keys provisioned automatically, in a
      dedicated DB/schema that does not collide with the sandbox tables); with only
      env set (no app code change), spans from `make ask`
      and `make eval` — which run on the **host** via `uv run` — appear in that
      backend's UI, no manual click-through to create a project/API key. This
      implies the backend **publishes its OTLP port to the host** and
      `.env` carries a `localhost`-based `OTEL_EXPORTER_OTLP_ENDPOINT` (not the
      compose service name).
- [ ] A short-lived run (`python -m app.evals`) reliably exports its spans —
      verified by asserting an OTLP sink received the `evals.run` trace, proving
      the flush fix (this fails today).
- [ ] Every eval case is persisted to `eval_results` with a non-null `trace_id`,
      **even when no backend endpoint is configured** (the in-proc SDK provider is
      always active, so `trace_id` is valid regardless of export). For a multi-case
      suite the captured `trace_id`s are **distinct per case** (each case is its own
      root trace) — not one shared run-level id repeated across rows.
- [ ] When a backend/`OTEL_TRACE_URL_TEMPLATE` is configured, `trace_url` is
      non-null and opens the correct trace (result → trace); when neither is set,
      `trace_url` is null and the row still carries a usable `trace_id`.
- [ ] From a trace in the backend, its eval score is visible on the trace
      (trace → result) when `EVAL_SCORE_SINK=langfuse`; with the sink disabled the
      OTLP path still works and imports no vendor SDK.
- [ ] With `OTEL_EXPORTER_OTLP_ENDPOINT` unset the app behaves exactly as today
      (no export, no errors, no new hard dependency on the backend); the core
      (non-observability-profile) stack still comes up with only the four
      original containers.
- [ ] `eval_results` is created on an **already-populated** `pgdata` volume (not
      just a freshly initialized one) — i.e. the runtime `CREATE TABLE IF NOT
      EXISTS` path is exercised, not only `db/init.sql`.
- [ ] With `OTEL_CAPTURE_CONTENT` off, no `input.question` / `retrieval.query`
      (or any content) **span attribute** is exported to the tracing backend. (This
      criterion is scoped to span export; the local `eval_results.question` column —
      golden-set fixture text — is out of its scope, see PII posture.)

## Dependencies

- None to start. Backend choice (Langfuse default) is settled in this spec.
- Downstream: `governance-and-audit` and `data-residency` build on this.

## Open questions

- **Online eval sampling** (a stated roadmap goal) — split into its own spec, or
  a fast-follow PR on this one? Leaning separate spec; needs sampling rate, async
  worker, and cost controls that don't fit `M`.
- **Size is closer to `L` than `M` (accepted).** The work is: full Langfuse v3
  multi-service compose + headless bootstrap, the per-case-root-trace refactor of
  `evals.run`, the new idempotent `eval_results` table, the `score_sink` adapter,
  the `trace_url` template path, the flush fix, and the `OTEL_CAPTURE_CONTENT`
  toggle. Profile-gating the backend and deferring online evals keep it tractable,
  but implementers should expect upper-`M`/lower-`L` effort, dominated by getting
  the Langfuse v3 stack to boot zero-click. *Accepted; re-size if the v3 bootstrap
  proves heavier than the examples suggest.*
- **`testing.md` is referenced but not yet written.** README/design link to
  `testing.md` for the CI sink strategy and per-criterion proofs; that file does
  not exist yet. Author it during expansion (the Test & rollout plan below is the
  source of truth until then). *Resolved: `testing.md` and
  `examples/example_tests.py` now exist.*
- **The example compose is a sketch, not the full v3 stack (accepted risk).**
  `examples/docker-compose.observability.yaml` wires only `langfuse` + `phoenix`
  and explicitly *elides* the required ClickHouse + Redis + object-store (minio) +
  langfuse-worker services and the `CREATE DATABASE langfuse` provisioning.
  Acceptance criterion 1 therefore **cannot be validated from the example alone** —
  the implementer must port in Langfuse's official self-host compose for v3 (and a
  dedicated DB) before the zero-click `make up` demo works. This is deliberate: the
  exact v3 image tags + companion set churn upstream, so we point at Langfuse's
  maintained compose rather than freeze a copy that goes stale. *Accepted; tracked
  as the first implementation step for criterion 1.*
- **Resolved — default backend: Langfuse, with Phoenix as a documented
  alternative.** Phoenix is single-container and OTLP-native (cheaper for the
  `make up` demo); Langfuse's edge is the scores API, which this spec isolates
  behind the `score_sink` adapter. The "keep the core light" ethos argues for
  Phoenix, **but** (a) the trace→result link (Goal 4) is a *primary* goal here
  and needs Langfuse's scores API, and (b) the cost objection is moot — the whole
  backend is `profiles:`-gated (the core four-container stack is untouched) and
  CI asserts against a lightweight OTLP sink, never the Langfuse stack (see
  [`testing.md`](testing.md)). So Langfuse stays the default to make the
  scores-on-trace demo work out of the box; Phoenix is a one-line `.env`/profile
  swap for anyone who only wants the OTLP (result→trace) direction. OTLP keeps
  both viable; the `score_sink`/`OTEL_TRACE_URL_TEMPLATE` seams keep the generic
  path backend-agnostic. Full trade-off and both wirings: [`design.md`](design.md) §1.
- **Resolved:** `trace_id` capture does *not* require an exporter. `app/observability.py`
  always installs a real SDK `TracerProvider` (default `ALWAYS_ON` sampler) and
  only the *exporter* is gated on the endpoint, so `get_current_span()` yields a
  valid non-zero `trace_id` whether or not a backend is configured. Therefore
  `eval_results.trace_id` is always populated; only `trace_url` depends on a
  backend/template. No change to the provider's always-on construction is needed.

## Risks & mitigations

- **Dropped spans on short-lived runs** (real today): `BatchSpanProcessor` may exit
  before flushing. *Mitigation:* `atexit` shutdown + explicit `force_flush()` on
  the eval/CLI exit path; acceptance test asserts the trace was received.
- **Vendor lock-in via the eval link.** The trace→result direction needs a
  backend-native scores API, which fights OTLP neutrality. *Mitigation:* isolate
  it behind the opt-in `score_sink` adapter; default no-op; OTLP path never imports
  a vendor SDK.
- **Sensitive data retention.** Prompts/answers in span attributes get retained by
  the backend. *Mitigation:* `OTEL_CAPTURE_CONTENT` toggle; hand retention policy
  to `governance-and-audit`.
- **Scope creep from online evals.** *Mitigation:* explicitly a non-goal here.
- **Backend ops surface.** Langfuse v3 self-hosted pulls in Postgres + ClickHouse
  + Redis + object storage, and rejects OTLP until a project/API key exists.
  *Mitigation:* `profiles:`-gate the whole backend so the core stack stays light;
  pre-provision keys via `LANGFUSE_INIT_*`; or pick the single-container Phoenix
  default (see Open questions) if the heft outweighs the scores-API benefit.
- **Migration won't reach existing volumes.** `db/init.sql` only runs on an empty
  `pgdata` dir, so an appended `eval_results` table never appears on stacks that
  already have data; inserts would fail at runtime. *Mitigation:* `CREATE TABLE IF
  NOT EXISTS` at the start of `evals.run` (idempotent, runs every time) in addition
  to `db/init.sql`; acceptance test exercises the populated-volume path.
- **Per-case-root-trace changes trace shape (accepted).** Making each case its own
  root trace (required for distinct per-case `trace_id`/`trace_url`/score) means a
  run no longer appears as one trace with N sub-trees but as N sibling traces. This
  is the deliberate tradeoff that makes the result→trace link case-specific; an
  optional run-level `evals.run` span may still be emitted for aggregate context,
  but it shares no `trace_id` with the per-case traces. *Accepted.*
- **DB write pattern (Low, note for impl).** Reuse `psycopg.connect(
  settings.database_url, autocommit=True)` exactly as `app/retrieval.py` does
  (note the `autocommit=True`), and run `CREATE TABLE IF NOT EXISTS` + inserts on
  that connection; treat eval-result persistence failures as non-fatal (log + carry
  on) so a DB hiccup never fails the quality gate.
- **Score/trace eventual consistency.** The `score_sink` posts a score for a
  `trace_id` whose OTLP span the backend may still be ingesting. *Mitigation:*
  Langfuse stores scores keyed by `traceId` independent of ingest order, so a
  late-arriving trace still binds; treat sink failures as non-fatal (log + carry
  on) so a flaky backend never fails the eval gate.
- **`force_flush()` can block the exit path (Low).** With a reachable-but-slow or
  unreachable backend, the `finally` flush in `run()` (and `atexit` shutdown) can
  block up to `BatchSpanProcessor`'s default export timeout (~30s) on every `make
  eval` / `make ask`. *Mitigation:* pass an explicit, short `timeout_millis` to
  `force_flush()` and keep flush failures non-fatal (the run already produced its
  result; export is best-effort).
- **`OTEL_TRACE_URL_TEMPLATE` mixes two substitution syntaxes (Low).** `${VAR}` is
  resolved by `python-dotenv` interpolation at load time; `{trace_id}` is filled by
  Python `.format()` per case. If `LANGFUSE_HOST` / `PROJECT_ID` are undefined,
  dotenv silently substitutes empty strings → a broken URL with no error.
  *Mitigation:* `.env.example` defines both vars explicitly (it does today);
  additionally, validate the resolved template at startup (warn + store `trace_url`
  null if it still contains an empty segment) rather than persisting a broken link.
- **`OTEL_EXPORTER_OTLP_HEADERS` value contains a space (Low).** The Basic header
  value is `Basic <base64>` (one space). The Python OTLP exporter splits each
  comma-separated entry on the first `=` and keeps the remainder verbatim, so the
  space is preserved and this works; but some tooling/percent-encoding expectations
  differ. *Mitigation:* keep the `Authorization=Basic <base64>` form documented in
  `.env.example` exactly as the Langfuse self-host docs show it; if a future
  exporter version rejects the space, percent-encode it (`Basic%20<base64>`).
- **Langfuse self-host DB isolation (Medium).** Langfuse must not share the
  `sandbox` application database; point it at a dedicated database/schema (or a
  separate Postgres) so its migrations never touch `documents` / `eval_results`.
  *Mitigation:* dedicated DB in the observability-profile compose; documented in
  `examples/`.

## Test & rollout plan

- **Unit:** flush is invoked from `run()`'s `finally` (covers pytest, which calls
  `run()` not `__main__`); `eval_results` rows carry a well-formed `trace_id`; a
  multi-case suite yields **distinct** per-case `trace_id`s (guards the
  shared-trace collision); `score_sink` defaults to no-op and the OTLP path imports
  no vendor SDK; `OTEL_CAPTURE_CONTENT=off` suppresses content attributes.
- **Integration:** prefer a **lightweight OTLP sink in CI** (e.g. an
  otel-collector with a file/logging exporter, or an in-process span collector)
  rather than the full Langfuse stack — assert (a) the `evals.run` trace is
  received after flush and (b) the `eval_results` row's `trace_id` matches the
  received span's trace id. `trace_url` *format* is unit-tested against the
  template; resolving it against a live backend is an optional, profile-gated job,
  not a required CI step (keeps CI fast and avoids the heavy backend).
- **Migration path:** a test that runs `evals.run` against a pre-existing
  `documents`-only database asserts `eval_results` is auto-created and populated
  (guards the initdb-only gap).
- **Eval gate:** unchanged pass/fail contract; the gate keeps working with the
  endpoint unset.
- **Rollout:** behind config — backend is opt-in via env and an optional compose
  profile; one additive DB migration in `db/init.sql`; no app hot-path change;
  fully reversible by unsetting env.

## References

- [Feature roadmap](../../ROADMAP.md)
- [Specs index](../README.md)
- In this directory:
  - [`design.md`](design.md) — backend trade-off, per-case-root-trace mechanics,
    the `score_sink`/`trace_url` seams, edge cases.
  - [`examples/`](examples/) — illustrative (spec, not wired-in) code for every
    file this feature touches.
  - [`testing.md`](testing.md) — how each acceptance criterion is proven and how
    it gates merge.
- Existing seam: `app/observability.py`, span sites in `app/agent.py`,
  `app/retrieval.py`, `app/evals.py`.
- Related specs: [eval-set maturity](../06-eval-set-maturity/README.md) (judge
  determinism, the eval gate this persists), [CI hardening](../07-ci-hardening/README.md)
  (the `eval-gate` merge gate this ties into), downstream consumers
  [governance-and-audit](../15-governance-and-audit/README.md) and
  [data-residency](../16-data-residency/README.md).
