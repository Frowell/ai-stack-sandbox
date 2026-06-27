---
title: Governance & audit
slug: governance-and-audit
area: governance
tier: Horizon
size: XL
status: Backlog
depends_on: [observability-backend]
issue:        # set to the GitHub issue number when created
---

# Governance & audit

> **Area** `governance` · **Tier** `Horizon` · **Size** `XL` · **Status** `Backlog` · **Depends on:** [observability-backend](../14-observability-backend/README.md)

## Summary

The technical substrate for high-risk-AI governance: a **durable, queryable
record** of what the system did, **tagged with a risk classification**, plus a
**human-in-the-loop (HITL) gate** that can pause a run on a high-stakes path and
record the human's decision. This is the sandbox-sized demonstration of the seam
EU AI Act high-risk obligations require — not a compliance product. It is an
**epic**: see [Decomposition](#decomposition) — it must be split into shippable
sub-features before any one of them is expanded into its own feature directory.

## Problem / Motivation

EU AI Act high-risk obligations apply from Aug 2026: retained, queryable traces
tied to a risk classification, with human-in-the-loop gates and audit trails on
high-stakes decisions.

Today the sandbox has **none of the substrate** these obligations assume:

- **Traces are ephemeral.** `app/observability.py` only attaches an OTLP exporter
  when `OTEL_EXPORTER_OTLP_ENDPOINT` is set; otherwise spans are created and
  dropped. There is **no durable, queryable store** the app owns — retention and
  "queryable by risk class" cannot be satisfied by the current beside-path.
- **No risk classification exists.** Nothing tags a request or trace with a risk
  class; spans carry only GenAI-convention attributes.
- **No HITL gate.** `app/agent.py` is a non-interactive, two-node LangGraph
  (`retrieve → generate`) with **no checkpointer**; `ask()` returns a string and
  cannot pause for approval and resume.
- **No actor / audit record.** There is no identity model (one shared
  `LITELLM_MASTER_KEY`) and no append-only audit table, so "who approved what" is
  unrepresentable.

(Epic — see Decomposition.)

## Goals

- **Durable trace/audit store** the app owns (Postgres), retained and queryable by
  risk class — not dependent on an external observability backend being present.
- **Risk classification tagging** of a run, propagated to spans and the audit
  record.
- **HITL escalation gate**: a high-stakes path pauses, surfaces the decision, and
  resumes only after a recorded human approve/reject.
- **Append-only audit trail** on high-stakes paths capturing actor, decision,
  timestamp, risk class, and the run/trace id.

## Non-goals

- Legal compliance sign-off (this is the technical substrate, not legal advice).
- A custom audit/trace UI (CLI + SQL queries are sufficient to demonstrate the
  seam; reuse the observability backend's UI for trace browsing).
- Real authentication / SSO / RBAC. The sandbox has a single operator identity;
  the audit record carries an `actor` field but identity *enforcement* is out of
  scope (see Risks).
- Cryptographic tamper-proofing (signed/Merkle-chained logs). Append-only +
  immutable-by-convention is the sandbox bar; note the gap.

## Decomposition

This XL epic should be promoted as the following shippable sub-features, **in
order**. Only after splitting should an individual sub-feature be expanded into
its own `design.md` / `examples/` / `testing.md`.

1. **Durable audit/trace store (S–M)** — Postgres tables + write path + retention.
   *Prereq for everything below.*
2. **Risk classification (S)** — a classifier seam (config-driven rules to start)
   that tags a run with a risk class and stamps it onto spans + the store.
3. **HITL gate (M–L)** — LangGraph checkpointer + interrupt on high-stakes paths,
   plus a resume/decision CLI; this is the largest and riskiest piece.
4. **Audit query + retention enforcement (S)** — `query`/`expire` CLI and the
   retention job, closing the loop on "queryable by risk class".

## Proposed design

Lives behind **two seams**: the orchestration graph (HITL) and a new
**governance store** beside the existing Postgres substrate. No hot-path
(`app/gateway.py`) change.

- **Store (owned by the app, not the observability backend).** Add tables to
  `db/init.sql` (additive, `CREATE ... IF NOT EXISTS`).
  **Application mechanism:** the repo has *no* migration runner — `db/init.sql`
  executes only on **first container start against an empty volume**. For the
  sandbox the sanctioned path is recreate-the-volume (`make down && make up` with a
  fresh volume) on a dev DB; the new `CREATE`s are idempotent so a fresh init is
  safe. Applying the same DDL to an *existing* volume (e.g. a long-lived dev DB) is
  a manual `psql` step (`make psql < db/init.sql` is safe because everything is
  `IF NOT EXISTS`). Call out that introducing a real migration tool is its own
  future feature, not in scope here. Tables:
  - `audit_events` — `id`, `run_id`, `trace_id`, `risk_class`, `event_type`
    (`run_started` | `hitl_requested` | `hitl_decided` | `run_completed`),
    `actor`, `decision` (`approve` | `reject` | NULL), `payload jsonb`,
    `created_at`. Append-only by convention: **no in-place UPDATE and no row-level
    DELETE** in app code — the *only* sanctioned delete is the bulk `expire`
    retention purge below (whole rows past the window, never selective edits).
    Document the gap that the DB does not *enforce* immutability.
  Linking to OTLP traces via `trace_id` keeps the beside-path optional: governance
  works with traces *off*, and is *navigable from* a trace when on (depends on
  [observability-backend](../14-observability-backend/README.md) for the UI link).
  `trace_id` is captured from the current span context
  (`trace.get_current_span().get_span_context().trace_id`, rendered as 32-hex) at
  write time; with no exporter the id still exists but resolves nowhere, so the
  column is **nullable/best-effort** and never a write prerequisite.
- **Risk classification.** A `classify_risk(question, context) -> RiskClass`
  seam (`minimal` | `low` | `high`), config-driven (keyword/route rules) to start,
  pluggable later. The class is set as a span attribute (`governance.risk_class`)
  and written to `audit_events`.
- **HITL gate.** Add a checkpointer to the graph in `app/agent.py` and an
  `interrupt()` before `generate` when `risk_class == high`.
  - **Checkpointer must be durable.** The resume path is a *separate* CLI process,
    so the paused graph state must outlive the first process. `MemorySaver` cannot
    satisfy cross-process resume (a new process gets an empty saver) and is **only**
    acceptable for an in-process/test resume. The shipped path uses the Postgres
    saver (`langgraph-checkpoint-postgres`, a new dependency — call it out in
    `pyproject.toml`). Note its checkpoint tables are **not** hand-written in
    `db/init.sql`: the saver owns its schema and creates it via
    `PostgresSaver.setup()`, which is **idempotent**. Bootstrap timing is explicit:
    it runs in a dedicated, run-once step — `make governance-init` (or
    `python -m app.governance init`) — invoked when `governance_enabled` is first
    turned on, **not** lazily on the hot path (lazy first-call setup would add
    schema-DDL latency to a user request and let two concurrent first runs race the
    `CREATE`s). The same step creates the `audit_events` partial unique index. The
    `audit_events` table itself *is* in `init.sql`. The graph's
    `thread_id` **is** the `run_id`, so `--resume <run_id>` keys directly into the
    checkpoint.
  - **`run_id` minting.** Every `ask()` mints `run_id = uuid4().hex` at entry and
    uses it as the LangGraph `thread_id`; it is stamped on every `audit_events` row
    and emitted to the log so even normal (`minimal`/`low`) runs stay correlatable
    by the `query` CLI. The bare-string return of `ask()` is **unchanged** on
    non-high runs — the id is logged, not appended to the answer.
  - **`ask()` return contract changes when a run pauses.** On a `high` run, `ask()`
    does **not** return an answer string; it returns/raises a typed `HitlPending`
    carrying the `run_id` (and the CLI prints `run_id`). Callers (`evals.py`,
    `make ask`) only hit this when `governance_enabled=true` *and* no
    `hitl_default_decision` is set — see the headless rule below.
  - **Resume CLI** `app.agent --resume <run_id> --approve|--reject --actor <id>`
    loads the checkpoint, records the decision, and continues (`approve`) or aborts
    (`reject`). **Decide-once:** resuming a run that already has a `hitl_decided`
    row is a no-op error (no second model call, no second audit row). This must be
    enforced in the **database**, not by a check-then-write (two concurrent
    `--resume` processes would both pass a read check and double-decide). Enforce
    with a partial unique index —
    `CREATE UNIQUE INDEX ... ON audit_events (run_id) WHERE event_type = 'hitl_decided'`
    — and treat the unique-violation on insert as the no-op error. Write the
    `hitl_decided` row **before** the model call (so a crash mid-resume cannot leave
    the run decidable twice), then run the model on `approve`.
  - **Crash-after-decision recovery (resume is idempotent, decision is not
    re-openable).** Writing the decision before the model call means a crash *after*
    the row but *before* `run_completed` would otherwise strand an approved run:
    the partial unique index blocks a fresh decision, so the run can never finish.
    Resolve by distinguishing **a conflicting decision** from **re-driving an
    already-made one.** On `--resume`, attempt the `hitl_decided` insert; on
    unique-violation, read the existing row: (a) if it is a `reject`, or an
    `approve` that already has a `run_completed` row → **no-op error** (idempotent,
    no second model call); (b) if it is an `approve` with **no** `run_completed`
    row → this is crash recovery: do **not** write a new decision, re-load the
    checkpoint and re-drive `generate` to produce the answer and the
    `run_completed` row. The requested `--approve|--reject` flag must match the
    recorded decision on recovery; a mismatch is a no-op error (you cannot flip a
    recorded decision). `generate` re-execution is at-least-once — acceptable
    because the model call is idempotent w.r.t. audit state (only `run_completed`
    is written, and it carries the `run_id`).
  - **Headless never blocks.** When `hitl_default_decision` is set
    (`approve` | `reject`), a `high` run resolves immediately with that decision
    instead of interrupting — `approve` runs the model, `reject` returns an explicit
    refusal answer (non-empty, so the eval gate still *scores* it rather than
    crashing on an empty string). Both still write a `hitl_decided` row with
    `actor="system:default"`. With `governance_enabled=false` the gate is entirely
    absent. **No headless path may ever raise `HitlPending`.**
    - **Caveat — `reject` default lowers eval scores, it does not just "still
      score".** A refusal answer will score ~0 on keyword/judge overlap, so any
      golden case that classifies `high` under a `reject` default would fail the
      gate. The regression that must keep CI **green** therefore runs with either
      `governance_enabled=false` (default) **or** `hitl_default_decision=approve`;
      `reject`-default is exercised only in a dedicated test that asserts
      non-blocking + a scored (not necessarily passing) result. Keep the golden set
      free of `high`-classified questions so the default eval path is unaffected.
- **Retention.** A documented `retention_days` config + an `expire` command that
  deletes rows past the window; the GDPR-erasure vs. retention tension is called
  out (see Open questions).

Config additions (`app/config.py`): `governance_enabled` (default **off**, so the
base slice is unchanged), `risk_rules`, `retention_days`, `hitl_default_decision`.

## Acceptance criteria

- [ ] With `governance_enabled=true`, every `ask()` run writes an
      `audit_events` row with `run_id`, `risk_class`, and `event_type`.
- [ ] Audit/trace records are **retained in Postgres and queryable by risk class**
      via a documented command (e.g. `app.audit query --risk high`) **with the
      observability backend OFF** (no external dependency for retention).
- [ ] A run classified `high` **pauses** before the model call and does **not**
      produce an answer until a recorded human decision; `ask()` surfaces the
      `run_id` (via `HitlPending`) rather than an answer. A *separate-process*
      `app.agent --resume <run_id> --approve` resumes and `--reject` aborts, each
      writing a `hitl_decided` row with `actor`, `decision`, and `created_at`
      (proving the checkpoint is **durable across processes**, not in-memory).
- [ ] Resuming an already-decided `run_id` is a **no-op error**: no second model
      call and no second `hitl_decided` row. Decide-once is enforced by a DB
      constraint (partial unique index on `run_id` where
      `event_type='hitl_decided'`), not a check-then-write, so two **concurrent**
      `--resume` processes on the same `run_id` yield exactly one decision and one
      model call.
- [ ] **Crash-after-decision recovers, not deadlocks:** a run with a `hitl_decided`
      = `approve` row but **no** `run_completed` row can be re-resumed with
      `--approve` to finish (re-driving `generate`, writing `run_completed`) without
      writing a second decision; a `--resume` whose flag conflicts with the recorded
      decision is a no-op error.
- [ ] Bootstrap is **explicit and idempotent**: `make governance-init` (run-once)
      invokes `PostgresSaver.setup()` and creates the partial unique index; no
      schema DDL runs on the request hot path. Running it twice is safe.
- [ ] Headless/default runs **never raise `HitlPending`**:
      `governance_enabled=false` (the default) and `governance_enabled=true` with
      `hitl_default_decision` set both let the eval gate and `make ask` complete —
      `reject`-default returns a non-empty refusal answer so the gate still scores.
- [ ] When the observability backend is on, an audit event is navigable to/from its
      OTLP trace via shared `trace_id`.
- [ ] `expire` removes records older than `retention_days` and the behavior is
      covered by a test.

## Dependencies

- [observability-backend](../14-observability-backend/README.md) — **for the
  trace↔audit linkage and trace UI only**. It is itself an unbuilt Horizon stub
  and **must ship first** (or this epic must own a minimal trace_id capture).
  Retention/query of the audit record is deliberately **independent** of it so
  this work isn't fully blocked.

## Open questions

- **What counts as "high-stakes" in a QA sandbox?** *Resolved (proposed default,
  no longer blocking):* `risk_rules` ships a small keyword/regex list that marks a
  question `high` (default seed: terms like `delete`, `legal`, `medical`,
  `financial advice` — demoable and obviously "consequential" for a doc-QA demo).
  The list is **empty unless `governance_enabled=true`**, and the demo doc set
  includes one seeded question that trips it so the HITL pause is reproducible. Real
  routing/topic classifiers are a later pluggable upgrade of the `classify_risk`
  seam. *(Confirm the seed wording during the risk-classification sub-feature.)*
- **Risk taxonomy:** adopt the AI Act risk tiers verbatim or a sandbox-simplified
  3-level scale? (Proposed: simplified `minimal/low/high`.)
- **Retention vs. erasure:** how to reconcile a fixed retention window with
  GDPR right-to-erasure when traces may contain PII? (Interacts with
  [guardrails](../09-guardrails/README.md) PII redaction and
  [data-residency](../16-data-residency/README.md).)
- **Actor identity:** with one shared master key, what does `actor` mean? Accept a
  caller-supplied label for the sandbox, or wait for an auth story?

## Risks & mitigations

- **HITL deadlocks headless runs** (evals/CI hang waiting for approval). *Mitigate:*
  `governance_enabled` defaults off and `hitl_default_decision` provides a
  non-interactive resolution; assert non-blocking in tests. *(High → mitigated.)*
- **Dependency on an unbuilt stub.** observability-backend (14) is also Backlog;
  building this first would block. *Mitigate:* the audit store is app-owned and
  works with traces off; trace linkage degrades gracefully. *(High → mitigated.)*
- **Audit integrity is convention-only.** No DB-enforced immutability or crypto
  chaining; a compromised app could rewrite history. *Accepted risk* for the
  sandbox (out of scope above); note prominently so it isn't mistaken for real
  compliance.
- **PII in retained traces/audit payloads.** Retention amplifies any PII leak.
  *Mitigate:* depend on guardrails redaction before persistence; keep `payload`
  minimal. *(Open question above.)*
- **Scope creep** — "audit trail" can absorb auth, RBAC, DLP, crypto. *Mitigate:*
  hard non-goals + the staged Decomposition.
- **`ask()` contract change.** Surfacing `HitlPending` instead of a string is a
  caller-visible change. *Mitigate:* it is reachable *only* under
  `governance_enabled=true` with no `hitl_default_decision`; every headless caller
  (evals, `make ask`) runs in a config where it cannot fire, asserted in the
  regression test. *(High → mitigated.)*
- **New runtime dependency** (`langgraph-checkpoint-postgres`) for durable resume.
  *Accepted:* it is the only way the documented separate-process resume CLI can
  work. Note its tables are created by `PostgresSaver.setup()` at bootstrap, not by
  `init.sql`. *(Medium.)*
- **No migration runner in the repo.** `db/init.sql` only runs on a fresh volume,
  so adding `audit_events` to an existing dev DB needs a manual `psql` apply (safe
  because all DDL is `IF NOT EXISTS`). *Accepted* for the sandbox; a real migration
  tool is a separate future feature, not in scope. *(Medium.)*

## Test & rollout plan

- **Unit:** `classify_risk` rules; `audit_events` write path; `expire` retention
  deletes the right rows.
- **Integration:** a `high`-classified run pauses; a **separate process** resumes
  it via `--resume <run_id>` (proving durable checkpointing), and only an explicit
  `approve`/`reject` resumes/aborts, with the correct audit rows; re-resuming a
  decided run is a no-op error, and **two concurrent resumes** of the same
  `run_id` produce exactly one `hitl_decided` row and one model call (decide-once);
  **crash recovery** — an `approve`d run with no `run_completed` row re-resumes to
  completion without a second decision row, and a conflicting-flag resume is a no-op
  error; a `minimal` run flows through unchanged.
- **Regression:** existing eval gate + `make ask` pass with `governance_enabled`
  off (default) and on with `hitl_default_decision=approve` — proving headless runs
  never block *and* the gate stays green. A separate test exercises
  `hitl_default_decision=reject` and asserts it is non-blocking and returns a
  scored (non-empty) refusal, without requiring it to pass the threshold.
- **Rollout:** behind `governance_enabled` (default off); additive Postgres
  migration only (no destructive change to `documents`); ship per the
  Decomposition order, store first.

## References

- [Feature roadmap](../../ROADMAP.md)
- [Specs index](../README.md)
- [Observability backend](../14-observability-backend/README.md)
- [Guardrails](../09-guardrails/README.md) · [Data residency](../16-data-residency/README.md)
