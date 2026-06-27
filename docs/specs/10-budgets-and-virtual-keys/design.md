# Budgets & virtual keys — design notes

Deeper notes behind [`README.md`](README.md): alternatives weighed, interface
sketches, sequencing, and edge cases. The concrete code is in
[`examples/`](examples/) (illustrative — a spec, not wired-in code); how each
acceptance criterion is proven is in [`testing.md`](testing.md).

The thesis is the same as the rest of the sandbox: **policy lives at the seam, not
in app code.** Today the seam (`app/gateway.py`) is a stateless pass-through that
authenticates with the master key. This feature turns it into a key-managed
control point using LiteLLM's *native* machinery — no new schema, no app-side
budget logic — so the only app change is which key it presents.

## 1. Where budget/key state lives — three options

| Option | What it is | Verdict |
|---|---|---|
| **A. LiteLLM native (Postgres + Redis)** | Give the proxy its own `litellm` database; keys/budgets/spend live in LiteLLM's Prisma-managed tables, rate-limit counters in Redis. Provision via the admin API. | **Chosen.** Zero app-side policy code, battle-tested, exactly the "configured like provider choice" model. Cost: the proxy gains a DB and a one-time migrate on first boot. |
| **B. App-side budget ledger** | A new table in the `sandbox` DB; `app/gateway.py` pre-checks spend before each call and records cost after. | Rejected. Puts policy back in app code (the thing the gateway exists to avoid), misses any caller that doesn't import the wrapper, and re-implements what LiteLLM already does. A non-goal ("no bespoke budget/spend store"). |
| **C. Reverse proxy / API-gateway in front of litellm** | Nginx/Envoy/APISIX enforcing keys + rate limits ahead of the proxy. | Rejected for this slice. Another service to run; duplicates LiteLLM's key model; can't see token spend (only request counts). Overkill for a demo. |

Option A is the literal realization of the compose file's existing hint ("add a
Postgres URL to persist virtual keys / budgets").

## 2. Two databases on one Postgres server

```
        ┌───────────── postgres (pgvector/pgvector:pg16) ─────────────┐
        │                                                             │
        │   database: sandbox            database: litellm            │
        │   owner:    postgres           owner:    litellm            │
        │   ├─ documents (pgvector)      ├─ LiteLLM_VerificationToken │
        │   └─ db/init.sql               ├─ LiteLLM_SpendLogs         │
        │      (01-init.sql)             └─ ... (Prisma-managed)      │
        │                                  created by 02-init-litellm │
        └─────────────────────────────────────────────────────────────┘
                  ▲                               ▲
        app  ────►│ DATABASE_URL=.../sandbox      │ DATABASE_URL=.../litellm
        litellm ──────────────────────────────────┘ (least-privilege `litellm` role)
```

Why a **separate database**, not a schema in `sandbox`:
- LiteLLM runs Prisma `migrate deploy` on its own database; keeping it away from
  the app's `documents` schema means the two migration regimes can never collide.
- The least-privilege `litellm` role owns only its database and has no grant on
  `sandbox`, so a proxy compromise can't read the corpus.

**The trap (called out in README):** Postgres only auto-creates `POSTGRES_DB`
(`sandbox`). Prisma `migrate deploy` creates *tables in an existing database*, not
the database itself. So nothing creates `litellm` unless `db/init-litellm.sql`
does — and like all `docker-entrypoint-initdb.d` scripts it runs **only on first
start of an empty volume**. On an already-initialized stack the script is skipped;
see §6 (rollout).

## 3. Startup ordering (the flake this prevents)

The DB-backed proxy is ready *later* than the stateless one (it migrates on first
boot), and today `app` waits on `litellm` with `service_started` (process spawned,
not listening). `make up && make ingest` would then race the migrate and fail with
what looks like an auth/gateway error.

```
make up
  │
  ├─ postgres  ──(healthcheck: pg_isready)──► healthy
  │      └─ first start runs 01-init.sql (sandbox) + 02-init-litellm.sql (create litellm db)
  │
  ├─ litellm   depends_on postgres: service_healthy
  │      └─ Prisma migrate deploy on `litellm`  ─► binds :4000
  │      └─ NEW healthcheck: GET /health/readiness ──► healthy
  │         (DB-aware: stays unhealthy until migrate deploy finishes;
  │          NOT /health/liveliness, which can pass mid-migrate — see README §1)
  │
  └─ app       depends_on litellm: service_healthy   ◄── CHANGED from service_started
         └─ make ingest / make ask now cannot run before the proxy migrated
```

Two concrete compose deltas make this deterministic (see
[`examples/docker-compose.budgets.yaml`](examples/docker-compose.budgets.yaml)):
add a healthcheck to `litellm` (with a generous `start_period`/`retries` to cover
the one-time migrate), and flip `app`'s dependency to `service_healthy`. The proxy
itself depends on `postgres: service_healthy` so it never connects before the
`litellm` database exists.

## 4. Idempotent seeding (`/key/generate` is not re-runnable)

`/key/generate` mints a brand-new key on every call. A naive seed run twice would
leave two keys and a clobbered `.env`. The seed
([`examples/seed_keys.py`](examples/seed_keys.py)) is made idempotent by a stable
`key_alias` (`app-runtime`) and this decision table:

| `.env` has a value? | gateway accepts it? | alias already exists? | action |
|---|---|---|---|
| yes | yes | — | **no-op** (print "already configured") |
| yes | no | — | refuse; tell user to `--rotate` |
| no | — | no | **generate**, write `.env` |
| no | — | yes | refuse (can't recover plaintext); `--rotate` to replace |

The last row is the sharp edge: **LiteLLM stores only the hashed token.** If the
alias exists but the plaintext isn't in `.env`, the secret is unrecoverable.
Minting again would create a *duplicate* under the same alias and still leave us
without a usable value, so the seed refuses by default and offers `--rotate`
(delete-by-alias + regenerate). The `.env` writer replaces only a commented/blank
`LITELLM_VIRTUAL_KEY=` line and never overwrites a populated one.

Alternatives considered: (a) deriving a deterministic key value — rejected,
LiteLLM owns key generation and hashing; (b) storing the key in the `litellm` DB
and reading it back — rejected, the plaintext is never stored. The alias+`.env`
approach is the only one that's both idempotent and recoverable.

## 5. Error surface — interface sketch

The app already uses the OpenAI SDK, so gateway policy denials arrive as typed
exceptions. The mapping (verify the exact 400 body on the pin):

```
over budget   400  -> openai.BadRequestError     -> PolicyDenial.OVER_BUDGET
rate limited  429  -> openai.RateLimitError      -> PolicyDenial.RATE_LIMITED
revoked/bad   401  -> openai.AuthenticationError -> PolicyDenial.UNAUTHORIZED
provider err  5xx  -> openai.APIError            -> PolicyDenial.NONE
```

[`examples/app_policy_errors.py`](examples/app_policy_errors.py) provides
`classify_gateway_error(exc)` so a caller — and the unit test — can tell "denied by
policy" from "the model/provider failed" **without** changing `app/gateway.py`.
This keeps the README's promise that the only app code change is `config.py`; the
classifier is an additive, optional helper, not a new control path.

### Budget enforcement is a soft ceiling

LiteLLM logs spend **asynchronously** after a call completes, so enforcement is on
*last-known* spend. Consequences the tests must respect:
- A burst of concurrent calls can overshoot before spend flushes (bounded
  overshoot — acceptable for cost control, documented in README).
- Right after a call, `/key/info` spend and the next over-budget rejection may lag
  by a beat — so the integration tests **poll/retry** rather than assert
  immediately (see [`examples/test_budgets.py`](examples/test_budgets.py)).

## 6. Rollout & the "existing volume" gotcha

Turning the feature on for a *fresh* stack is just config: add `DATABASE_URL` +
Redis to `litellm`, mount `db/init-litellm.sql`, `make up`, `make seed`. But the
init script does **not** run against an already-populated `pgdata` volume. For an
existing stack the `litellm` database must be created out of band — either
`make down` (which is `down -v`, dropping the volume) then `make up`, or a one-off
`CREATE DATABASE litellm OWNER litellm;` via `make psql`. This is a one-time
migration cost; document it in the PR. The app's own schema (`db/init.sql`) is
untouched.

Disabling is symmetric and config-only: unset `LITELLM_VIRTUAL_KEY` (app falls
back to the master key) and the proxy can run without `DATABASE_URL` again
(stateless, no budgets).

## 7. Single-replica caveat (Redis wiring)

The demo runs one proxy replica, so cross-process enforcement can't be *observed*
without scaling. We still wire Redis because (a) it's the correct, replica-safe
configuration and (b) it makes rate-limit counters survive a proxy restart — which
*is* observable on one replica and is what the restart test asserts. Cross-replica
enforcement is therefore "configured and argued correct," not separately tested
(stated in the acceptance criteria).

## 8. Interaction with other specs

- **#07 CI hardening** owns the `eval-gate` job that stands up Postgres + the
  gateway. The budget integration tests need that same live stack plus a provider
  secret, so they ride the secret-gated job and stay skipped on fork/unit runs.
  This spec also *adds* `db/init-litellm.sql` and the compose deltas the gate
  brings up — coordinate so the gate's `docker compose up` includes them, and pin
  the litellm image per #07's supply-chain pinning. See [`testing.md`](testing.md).
- **#09 Guardrails** attaches at the same gateway callback/policy seam. No code
  overlap (keys vs. content scanning), but both pin the litellm image — agree on
  one pinned version.
- **#08 Caching** also adds gateway state/Redis; ensure the `router_settings`
  redis config is shared, not duplicated, if both land.

## 9. Edge-case checklist

- **Empty `LITELLM_VIRTUAL_KEY=`** (set but blank): must fall back to master — hence
  `or`, not a two-arg `os.environ.get`, in `config.py`.
- **Budget hit during `make ingest`:** `app/ingest.py` truncates under
  `autocommit=True` *before* the single `embed([...])` call, so a budget rejection
  there leaves `documents` **empty**, not partial. Accepted for v1 (re-run under
  budget restores it); the clean fix (embed-before-truncate, or one transaction) is
  an `ingest.py` change out of scope here — flagged for whoever does ingest
  robustness. (README open question.)
- **Revoked-then-used key:** 401, not a silent empty response — asserted.
- **`/key/info` spend lag:** async; always poll.
- **Restart persistence:** keys + spend in Postgres, RPM/TPM counters in Redis;
  both survive `docker compose restart litellm` (volume-backed). Asserted.
- **Health endpoint** must be the DB-aware `/health/readiness` (not
  `/health/liveliness`, which can return OK before Prisma `migrate deploy`
  finishes and would re-open the migration race — see README §1 and AC #8). The
  endpoint **name** and **admin request/response shapes** are version-sensitive —
  every such line in `examples/` carries a `# VERIFY` against the pinned image.
