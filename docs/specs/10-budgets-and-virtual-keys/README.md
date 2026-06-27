---
title: Budgets & virtual keys
slug: budgets-and-virtual-keys
area: gateway
tier: Later
size: M
status: Backlog
depends_on: []   # no spec-level deps, but has a hard infra prerequisite: gateway persistence (see Dependencies)
issue:        # set to the GitHub issue number when created
---

# Budgets & virtual keys

> **Area** `gateway` · **Tier** `Later` · **Size** `M` · **Status** `Backlog` · **Depends on:** gateway persistence (DB-backed LiteLLM proxy)

## Summary

Turn the LiteLLM gateway from a stateless pass-through into a key-managed control
point: issue scoped **virtual keys** (one per tenant/caller) instead of handing
out the master key, attach **spend budgets** and **rate limits** (RPM/TPM) to each
key, and reject calls that exceed them with a clear error. This uses LiteLLM's
native key/budget machinery, which requires giving the proxy its own Postgres
database (today the `litellm` service is stateless). The app stops using
`LITELLM_MASTER_KEY` for traffic and uses a scoped virtual key instead; the master
key becomes admin-only.

## Problem / Motivation

No per-tenant cost control, rate limiting, or key management. Today every caller —
including the app — authenticates to the gateway with the single shared
`LITELLM_MASTER_KEY` (see `app/config.py` → `gateway_api_key`). There is no way to
cap spend, throttle a noisy caller, attribute usage, or revoke access short of
rotating the master key for everyone.

## Goals

- Virtual keys: per-caller keys issued/revoked via the gateway, scoped to the
  `chat` and `embeddings` aliases (never to provider keys).
- Per-key spend **budgets** with a defined reset period; over-budget calls are
  rejected.
- Per-key **rate limits** (requests/min and tokens/min); throttled calls are
  rejected with `429`.
- Per-key usage/spend/limits are **queryable** (LiteLLM `/key/info`, `/spend/*`).
- The app authenticates with a scoped virtual key, not the master key.

## Non-goals

- Billing/invoicing or chargeback reporting.
- A full multi-tenant identity/SSO model in the app — "tenant" here maps to a
  virtual key (or LiteLLM "team"); the app remains single-process and demonstrates
  the mechanism with 1–2 example keys.
- A bespoke budget/spend store — use LiteLLM's built-in tables + Redis, not a new
  schema.

## Proposed design

Seam: **gateway config + gateway persistence** (`gateway/litellm_config.yaml`,
`docker-compose.yml`, `app/config.py`). No application call-site changes in
`app/gateway.py` — it already reads its key from config.

> Deeper rationale, alternatives, and edge cases are in
> [`design.md`](design.md); illustrative code (compose/config/seed/tests) is in
> [`examples/`](examples/); the per-criterion proof + CI gating is in
> [`testing.md`](testing.md).

```
                 admin/seed only (master key)
                 ┌───────────────────────────────────────────┐
  scripts/seed_keys.py ──►/key/generate /key/info /key/delete │
                 │                                             ▼
   app ──gateway.chat()/embed()──►  litellm proxy  ──►  provider (OpenAI/…)
        LITELLM_VIRTUAL_KEY            │   ▲
        (scoped: chat,embeddings)      │   │ budget/RPM/TPM check on the virtual key
        budget→400  rpm/tpm→429        │   │
        revoked→401                    ▼   │
                            postgres `litellm` db        redis
                            (keys, budgets, spend)   (RPM/TPM counters)
```

The app's runtime key is the **scoped virtual key**; the **master key** is used
only on the admin/seed path. State the proxy needs to enforce policy moves out of
proxy memory into Postgres (keys/budgets/spend) and Redis (rate-limit counters).

1. **Give the proxy a database.** LiteLLM key/budget/spend state lives in Postgres;
   the proxy runs its own Prisma migrations on startup. Add a `DATABASE_URL` for
   the `litellm` service (the compose file already flags this: "add a Postgres URL
   to persist virtual keys / budgets"). Use a **separate database** (`litellm`) from
   the app's `sandbox` DB so app migrations and LiteLLM migrations don't collide; the
   existing `postgres` container can host both.
   **Create the database first.** Postgres only auto-runs `docker-entrypoint-initdb.d`
   against `POSTGRES_DB` (`sandbox`), and Prisma `migrate deploy` applies migrations
   to an *existing* database — neither will create the `litellm` DB. Add a second
   first-run init script (e.g. `db/init-litellm.sql` mounted into
   `/docker-entrypoint-initdb.d/` with `CREATE DATABASE litellm;` and, ideally, a
   distinct least-privilege role) so the proxy has a database to connect to on its
   first start. The proxy's `depends_on: postgres` must wait for
   `condition: service_healthy` so it doesn't race the DB creation.
   **Proxy readiness gating.** Two ordering hazards appear once the proxy is DB-backed:
   (a) the proxy now runs Prisma `migrate deploy` on first start, so it is ready
   *later* than today; (b) the `litellm` service currently has **no healthcheck**, and
   `app` waits on it with `condition: service_started` (process spawned, not ready).
   The app's first call — and `make ingest` / `make ask` run right after `make up` —
   can therefore hit the proxy before it has migrated and bound `:4000`. Add a
   healthcheck to the `litellm` service and change `app`'s `depends_on: litellm` to
   `condition: service_healthy`. **Probe `/health/readiness`, not `/health/liveliness`.**
   `/health/liveliness` only reports "process alive" and can return OK before Prisma
   `migrate deploy` has finished, which would re-introduce the very migration race this
   healthcheck exists to close; `/health/readiness` reflects DB connectivity/readiness
   and is the correct gate for "safe to send traffic". The healthcheck command must use
   an HTTP client that actually exists in the pinned `litellm` image — confirm `curl`
   (or `wget`) is present, otherwise use a Python one-liner (the image ships Python),
   e.g. `python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://localhost:4000/health/readiness').status==200 else 1)"`.
   A healthcheck that references a missing binary marks the service permanently
   `unhealthy` and blocks `app` from ever starting. Without this gating the demo's first
   run flakes intermittently and the failure looks like a gateway/auth error rather than
   a startup race.
2. **Define budgets/limits as config + seeded keys.** Express default
   budget/RPM/TPM in `litellm_config.yaml` (`litellm_settings` /
   `general_settings`), and create the demo virtual key(s) via the admin API
   (`/key/generate` with `max_budget`, `budget_duration`, `rpm_limit`, `tpm_limit`,
   `models: [chat, embeddings]`) in a small seed step (Makefile target /
   idempotent script). Keys are NOT committed to the repo; the generated value is
   written to `.env` (gitignored).
   **Idempotency:** `/key/generate` mints a *new* key on every call, so a naive seed
   script is not re-runnable. Make it idempotent: give the key a stable `key_alias`,
   check `/key/info`/`/key/list` for that alias first and generate only if absent, and
   write to `.env` only when the var is unset. Re-running the seed must not mint
   duplicate keys or clobber a working `.env`.
3. **Repoint the app.** This is the **one app code change**: `app/config.py` today
   reads `gateway_api_key` directly from `LITELLM_MASTER_KEY`; change it to prefer a
   new `LITELLM_VIRTUAL_KEY` and fall back to `LITELLM_MASTER_KEY` (then the
   `sk-sandbox-master` default) only if unset, so first-run still works. `app/gateway.py`
   is unchanged — it already reads its key from config. `LITELLM_MASTER_KEY` stays for
   admin/seed operations only. Concretely, the frozen `Settings` field becomes
   `gateway_api_key = os.environ.get("LITELLM_VIRTUAL_KEY") or os.environ.get(
   "LITELLM_MASTER_KEY", "sk-sandbox-master")` (an empty `LITELLM_VIRTUAL_KEY` must
   fall through, hence `or`, not a two-arg `get`). Also add a commented
   `LITELLM_VIRTUAL_KEY=` line to `.env.example` so the switch is discoverable, and a
   `make seed` target wrapping the idempotent seed script.
4. **Rate limiting** uses LiteLLM's Redis backing so limits are enforced consistently,
   not per-process. The `redis` service exists, but the `litellm` service is **not
   currently wired to it** — add `REDIS_URL` (or `REDIS_HOST`/`REDIS_PORT`) to the
   `litellm` service env and the corresponding `router_settings`/`litellm_settings`
   redis config so the proxy actually uses Redis for limit counters. Note: the demo
   runs a **single proxy replica**, so cross-process enforcement is configured but not
   directly observable without scaling the proxy (see acceptance criteria / testing).
5. **Error surface.** Over-budget → HTTP `400` with a budget message; rate-limited
   → `429`; revoked/invalid key → `401`. The `openai` client in `app/gateway.py`
   surfaces these as `openai.BadRequestError` (400), `openai.RateLimitError` (429),
   and `openai.AuthenticationError` (401) respectively; document this mapping so
   callers can distinguish "denied by policy" from "provider error". Confirm the exact
   status/body LiteLLM returns for budget exhaustion against the **pinned** image
   version (see open questions) rather than assuming, since this is version-sensitive.

### Budget-enforcement consistency (important)

LiteLLM tracks spend **asynchronously** (it logs the cost after a call completes),
so budgets are enforced on *last-known* spend, not a synchronous pre-charge. Under
concurrency a burst of in-flight requests can overshoot the budget before spend is
flushed. This is acceptable for cost-control (the budget is a soft ceiling with
bounded overshoot), not a hard transactional limit. Acceptance criteria below
reflect "rejected once spend exceeds budget", not "never exceeds by a single token".

## Acceptance criteria

- [ ] The `litellm` proxy runs against its own Postgres database (`litellm`, separate
      from `sandbox`); the database is created on first stack start (init script), the
      proxy applies its Prisma migrations, and restarting it preserves issued keys and
      accumulated spend (no in-memory-only state).
- [ ] A virtual key can be generated and revoked via the admin API; a revoked key is
      rejected (`401`).
- [ ] A virtual key scoped to `[chat, embeddings]` can call both aliases; it cannot
      use the master key's admin endpoints.
- [ ] A key whose accumulated spend exceeds its `max_budget` is rejected with a clear,
      machine-distinguishable error (HTTP `400`, budget reason), and a key under budget
      still succeeds. (Soft ceiling: rejection occurs once recorded spend ≥ budget;
      bounded concurrent overshoot is accepted — see consistency note.)
- [ ] A key exceeding its RPM/TPM limit is rejected with `429`. The proxy is wired to
      Redis (counters live in Redis, not proxy memory), so the limit would hold across
      replicas; the single-replica demo verifies enforcement and that counters survive
      a proxy restart (cross-replica enforcement is configured, not separately tested).
- [ ] Per-key usage/spend/limits are queryable (`/key/info` and a spend endpoint),
      and the queried spend reflects both chat **and** embedding calls.
- [ ] The app authenticates with a scoped virtual key (`LITELLM_VIRTUAL_KEY`), not
      `LITELLM_MASTER_KEY`; the master key is used only for admin/seed actions and is
      not the app's runtime traffic key.
- [ ] First-run still works: with no virtual key configured the stack starts (falls
      back to master key) and the seed step is documented to switch it on. An empty
      `LITELLM_VIRTUAL_KEY=` (set but blank, e.g. before seeding) also falls back to the
      master key rather than authenticating with an empty string.
- [ ] Startup ordering is deterministic: the `litellm` service has a healthcheck that
      probes `/health/readiness` (DB-aware, not `/health/liveliness`) using a client
      proven present in the image, and `app` waits on `condition: service_healthy`, so
      `make up && make ingest` does not race the proxy's first-start Prisma migration.
      Running `make seed` twice in a row mints no duplicate key and does not clobber an
      existing `LITELLM_VIRTUAL_KEY` in `.env` (idempotent).

## Dependencies

- **Gateway persistence (hard prerequisite, infra not a spec):** the `litellm`
  service must have a `DATABASE_URL`. Today it is stateless. This is shared with any
  other gateway feature needing state and should be stood up first.
- LiteLLM proxy native key/budget/rate-limit support (already the gateway image).
- Redis (already present) for rate-limit counters.
- No dependency on other roadmap specs.

## Open questions

- **Budget reset period:** calendar-monthly vs rolling vs daily? Proposed default:
  monthly (`budget_duration: 30d`) for the demo; confirm.
- **"Tenant" granularity:** per-key vs LiteLLM "team" (key groups sharing a budget)?
  Proposed: start per-key; teams are a later refinement.
- **Budget exhaustion during ingest:** `app/ingest.py` does **not** batch — it makes
  a single `embed([...all docs...])` call. Verified against the current code
  (`app/ingest.py` lines 16–18), the order is **embed first, then open the DB
  connection and `TRUNCATE documents RESTART IDENTITY`**. So if the key hits budget on
  the embed call, the exception propagates *before* `connect`/`TRUNCATE` ever runs and
  the existing corpus is left **intact** — the failure mode is "ingest aborts, prior
  corpus preserved", not "corpus wiped". (Accepted risk for v1: the budget error
  surfaces clearly and re-running `make ingest` once under budget refreshes the corpus;
  there is no per-document checkpoint/resume, and a single oversized `embed([...])` call
  is all-or-nothing. The previously-feared "TRUNCATE-then-failed-embed wipes the table"
  ordering does **not** exist in the current code; do not "fix" it by moving TRUNCATE
  earlier. If batched/resumable ingest is ever wanted, that is an `ingest.py` change out
  of scope here.)
- **Image pin:** the proxy currently uses the moving tag `main-stable`. Key/budget
  behavior is version-sensitive; pin to a specific LiteLLM version before relying on
  it (tracked as a risk below).
- **LiteLLM DB role privileges:** if a distinct least-privilege role is used for the
  `litellm` database (rather than reusing `postgres`), it must own the `litellm`
  database (or hold `CREATE` on its schema) so Prisma `migrate deploy` can create the
  LiteLLM tables on first start. Decide: reuse the `postgres` superuser for the demo
  (simplest, accepted) vs. provision a scoped role in `db/init-litellm.sql`. Proposed
  default for the demo: a dedicated `litellm` role that owns the `litellm` database.

## Risks & mitigations

- **Spend overshoot under concurrency** (async spend tracking) → documented soft
  ceiling; if a hard cap is ever required, that is a separate, larger change.
- **Two databases on one Postgres** (app `sandbox` + LiteLLM) → use a separate
  `litellm` database with distinct credentials so migrations and `db/init.sql` can't
  clash; never point LiteLLM at the `documents` schema. The `litellm` DB must be
  **created explicitly** (extra first-run init script) — Postgres won't auto-init it
  and Prisma won't create it; missing this is a silent proxy-won't-start failure.
- **Master-key leakage** → the master key remains powerful (creates keys, no
  budget). Keep it out of the app runtime path and out of the repo; only the seed
  step uses it.
- **Moving image tag** (`main-stable`) silently changing key/budget semantics →
  pin a known-good LiteLLM version.
- **Lockout / first-run regression** → master-key fallback when no virtual key is
  set keeps the stack bootable.

## Test & rollout plan

- **Integration (primary evidence):** bring up the stack with the DB-backed proxy;
  seed a key with a tiny `max_budget` and low `rpm_limit`; assert (a) under-budget
  call succeeds, (b) over-budget call returns `400`, (c) over-RPM call returns `429`,
  (d) revoked key returns `401`, (e) `/key/info` reports spend including an embedding
  call, (f) spend/keys survive a proxy restart.
  **Cost/secret note:** budget tests track *real* spend, so they need a live provider
  call and a real `OPENAI_API_KEY`. Keep cost negligible by setting `max_budget` so low
  that one cheap call crosses it (or use a mock/fixed-cost provider). This test class
  is network-dependent and must be marked/skippable so CI without provider secrets
  (and the default unit run) still passes — coordinate with the CI-hardening spec (07)
  on whether the live-stack integration job runs on every PR or on a gated/nightly job.
  Because spend tracking is async, over-budget assertions may need a short poll/retry
  on `/key/info` rather than asserting immediately after the call.
- **Unit:** a thin wrapper test that the app distinguishes policy-denial (`400`/`429`)
  from provider errors and surfaces a clear message.
- **Rollout:** behind config — adding `DATABASE_URL` (+ Redis config) to the proxy and
  `LITELLM_VIRTUAL_KEY` to the app turns it on; absent these, behavior is unchanged
  (master key, no budgets). One migration concern: the proxy's first DB-backed start
  creates/runs Prisma migrations — document the one-time startup cost and the new
  `service_healthy` dependency ordering. The app's own schema (`db/init.sql`) is
  untouched; the only DB additions are the new `litellm` database and its init script.

## References

- [Design notes](design.md) — alternatives, ordering diagram, idempotent-seed
  decision table, edge cases.
- [Testing plan](testing.md) — each acceptance criterion → test, and how it gates
  merge via the eval/CI gate.
- [Examples](examples/) — illustrative compose/config/seed/test shapes (a spec,
  not wired-in code).
- [CI hardening (#07)](../07-ci-hardening/README.md) — owns the secret-gated
  `eval-gate` job the integration tests ride and the image-pinning these tests need.
- [Guardrails (#09)](../09-guardrails/README.md) — the adjacent gateway policy seam
  (shares the litellm image pin).
- [Feature roadmap](../../ROADMAP.md)
- [Specs index](../README.md)
