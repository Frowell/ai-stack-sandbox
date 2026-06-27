# Observability backend — design notes

Deeper notes behind [`README.md`](README.md): the backend trade-off resolved,
the per-case-root-trace mechanics (the subtle bit), the two backend-agnostic
seams (`score_sink`, `trace_url`), the flush ordering, and the edge cases that
bite. Concrete code is in [`examples/`](examples/) — **illustrative, a spec, not
wired-in code.**

The whole feature lives behind the existing `app/observability.py` "beside-path"
seam plus a new persistence step inside `evals.run()`. The hot path
(`app/gateway.py`, `gateway/litellm_config.yaml`) is **untouched**.

---

## 1. Backend choice — Langfuse default, Phoenix documented alternative

| | **Langfuse v3** (default) | **Phoenix** (alternative) | Any OTLP collector |
|---|---|---|---|
| Containers | 4+ (app server + Postgres + ClickHouse + Redis + object store) | **1** | 1 |
| OTLP ingest | `…/api/public/otel` (HTTP), appends `/v1/traces` | `:4318` (HTTP) / `:4317` (gRPC) | yes |
| Auth to ingest | **required** — Basic header from a provisioned project key | none by default | varies |
| Bootstrap to first span | org + project + **fixed project id** + API key, via `LANGFUSE_INIT_*` | none | none |
| result → trace (`trace_url`) | yes (template) | yes (template) | depends |
| **trace → result (scores on trace)** | **yes — scores API** | **no** | no |
| Fit for `make up` demo | heavier, but `profiles:`-gated | lightest | n/a |

**Why Langfuse stays the default.** Goal 4 ("make a trace navigable to its eval
result") is a *primary* goal of this spec, and the trace→result direction needs a
backend-native scores API that only Langfuse has. The standard objection —
"Langfuse is too heavy" — does not apply here for two reasons:

1. The entire backend is `profiles: ["observability"]`-gated. `make up` without
   the profile still brings up exactly the four original containers (acceptance
   criterion: core stack unchanged). Only operators who opt in pay the cost.
2. **CI never runs the backend.** The integration tests assert that a flushed
   `evals.run` trace reaches a *lightweight* OTLP sink (an in-process span
   collector or a single `otel/opentelemetry-collector` with a logging exporter),
   not Langfuse — see [`testing.md`](testing.md). The 4-container stack is a
   local/demo concern, never a CI dependency.

**Why Phoenix is still first-class.** Anyone who only wants the OTLP
(result→trace) direction gets a single container and zero bootstrap by flipping
two `.env` lines and the profile target. Because the generic persistence path
talks only OTLP + a `trace_url` *template* (never a vendor SDK), the swap is
env-only. The one thing Phoenix cannot do is push the score *onto* the trace;
with Phoenix selected, `EVAL_SCORE_SINK` stays at its `noop` default and the
trace→result acceptance criterion is simply N/A for that backend.

Both wirings (compose service + `.env`) are in
[`examples/docker-compose.observability.yaml`](examples/docker-compose.observability.yaml)
and [`examples/env.example.snippet`](examples/env.example.snippet).

### The Langfuse bootstrap is load-bearing (not assumed away)

Self-hosted Langfuse rejects OTLP until an org/project/API-key exists, and the
project id is an internal id assigned at creation — so the `trace_url` template's
`${PROJECT_ID}` is unknowable until someone opens the UI, which defeats the
zero-click goal. Fix: pin all three with `LANGFUSE_INIT_*` so the keys **and the
project id** are known ahead of time:

```
LANGFUSE_INIT_ORG_ID / LANGFUSE_INIT_PROJECT_ID            # fixed, dev-only
LANGFUSE_INIT_PROJECT_PUBLIC_KEY / LANGFUSE_INIT_PROJECT_SECRET_KEY
```

Then derive the ingest header from those same fixed keys:

```
OTEL_EXPORTER_OTLP_HEADERS=Authorization=Basic <base64("pk-...:sk-...")>
```

These are **dev-only placeholders committed to `.env.example`** — they unlock a
local sandbox, not a real deployment; rotate for anything real.

---

## 2. The host-execution caveat (why localhost, not the service name)

`make ask` / `make eval` run `uv run python -m …` as a **host process** (see the
Makefile), *not* `docker compose exec app`. Two consequences that the "spans
appear with only env set" criterion depends on:

- Env set only in the compose `app` service's `environment:` block does **not**
  reach a host process. `OTEL_EXPORTER_OTLP_ENDPOINT` must live in **`.env`**,
  which `config.py`'s `load_dotenv()` reads on the host.
- The compose service name (`langfuse` / `phoenix`) is **not resolvable** from the
  host. The backend must **publish its OTLP port** (like `postgres:5432` and
  `litellm:4000` already do), and the endpoint must be `http://localhost:<port>`.

So `.env.example` documents **two** forms (see
[`examples/env.example.snippet`](examples/env.example.snippet)):

```
# For `make ask` / `make eval` (host processes) — the published port:
OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:3000/api/public/otel        # Langfuse
# OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:4318                      # Phoenix
# For `docker compose exec app …` (in-network) — the service name:
# OTEL_EXPORTER_OTLP_ENDPOINT=http://langfuse:3000/api/public/otel
```

> **OTLP-HTTP path gotcha.** `opentelemetry-exporter-otlp-proto-http` appends
> `/v1/traces` to the endpoint. So the endpoint must be the collector **base**
> (Langfuse: `…/api/public/otel`; Phoenix: `…:4318`), never the full
> `…/v1/traces` path. Getting this wrong yields silent 404s on export.

---

## 3. Reliable export from short-lived processes (the flush fix)

`BatchSpanProcessor` buffers spans and exports on a timer; a CLI/eval process
usually exits before the timer fires, so the batch is dropped — exactly the runs
whose traces we most want. Two-layer fix in `app/observability.py`
([`examples/observability.py`](examples/observability.py)):

1. `flush()` → `_provider.force_flush()` (a no-op when no exporter is attached, so
   safe with the endpoint unset).
2. `atexit.register(_provider.shutdown)` — belt-and-braces backstop.

**Where `flush()` is called is the load-bearing choice.** It must be in a
`finally` **inside `evals.run()` itself**, not only in `__main__`:

```
run()  ──finally──▶ flush()
  ▲
  ├── app.evals.__main__        (covered)
  ├── tests/test_evals.py::test_quality_gate  → calls run() directly, never __main__  (covered)
  └── any future programmatic caller          (covered)
```

`test_quality_gate` calls `run()` directly and never reaches `__main__`, so a
flush in `__main__` alone would leave the very test that asserts delivery
unable to see it. Also call `flush()` at the end of `app.agent.__main__` for the
`make ask` path. The `atexit` shutdown covers everything else.

---

## 4. Per-case root traces (the subtle correctness bit)

### The bug a naïve implementation would ship

`trace_id` is **constant across an entire trace tree**. Today `evals.run` opens
one `evals.run` span and each case's `ask()` opens `agent.run` as a *child* of it.
So reading `trace.get_current_span().get_span_context().trace_id` inside the loop
returns the **same** id for every case. Persisting that would:

- write an identical `trace_id` to every `eval_results` row,
- collapse the result→trace link to one giant trace, and
- make per-case `score_sink` posts ambiguous (N scores on one `traceId`).

### The fix: each case is its own root trace

Detach the current context before opening the per-case span, so it starts with
**no parent** and therefore a **fresh trace id**:

```
evals.run span (optional, aggregate)         trace = R
   │   (context detached per case ↓)
   ├─ evals.case  trace = T1 (root)   ── agent.run (child, trace T1) ── retrieve / generate
   ├─ evals.case  trace = T2 (root)   ── agent.run (child, trace T2) ── …
   └─ evals.case  trace = T3 (root)   ── agent.run (child, trace T3) ── …
```

Mechanically (see [`examples/evals.py`](examples/evals.py)):

```python
from opentelemetry import context as otel_context, trace
from opentelemetry.trace import format_trace_id

with span("evals.run", **{"eval.suite": path, "eval.n": len(cases)}):
    for c in cases:
        token = otel_context.attach(otel_context.Context())  # empty ctx => no parent
        try:
            with span("evals.case", **{"eval.question": c["question"]}) as case_span:
                trace_id = format_trace_id(case_span.get_span_context().trace_id)
                answer = ask(c["question"])          # agent.run nests under evals.case (same trace)
                ...                                  # score, persist (trace_id, trace_url)
        finally:
            otel_context.detach(token)
```

`format_trace_id` gives the canonical 32-hex-char string the backends use in URLs
and the scores API. `evals.run` keeps a *different* trace id and is purely
aggregate context — never persisted as a case's id.

**Tradeoff (accepted, per README risks):** a run is now N sibling root traces, not
one tree with N sub-trees. That is the deliberate price of a case-specific
result↔trace link.

**Why this works with no exporter.** `app/observability.py` always installs a real
SDK `TracerProvider` (default `ALWAYS_ON` sampler); only the *exporter* is gated
on the endpoint. So `get_span_context().trace_id` is a valid non-zero id whether
or not a backend is configured — `eval_results.trace_id` is therefore *always*
populated; only `trace_url` depends on a backend/template.

---

## 5. The two backend-agnostic seams

The OTLP span path must never import a vendor SDK. Two narrow seams keep the
vendor-specific bits out of the generic persistence path.

### 5a. `trace_url` from a template (result → trace)

A Langfuse trace URL is `{host}/project/{projectId}/traces/{trace_id}`; Phoenix's
differs again. Hardcoding either in `evals.run` would couple generic persistence
to a vendor. Instead build it from `OTEL_TRACE_URL_TEMPLATE`:

```
# Langfuse
OTEL_TRACE_URL_TEMPLATE=${LANGFUSE_HOST}/project/${PROJECT_ID}/traces/{trace_id}
# Phoenix (verify exact path against the running Phoenix version)
# OTEL_TRACE_URL_TEMPLATE=http://localhost:6006/projects/default/traces/{trace_id}
```

`evals.run` substitutes `{trace_id}` (env vars like `${LANGFUSE_HOST}` are
expanded once at startup). **When the template is unset, store `trace_id` and
leave `trace_url` NULL** — the link degrades to "trace_id only" rather than
breaking when the backend is swapped. (See open question: Phoenix's exact trace
URL path must be confirmed against the deployed version; the template makes this a
config edit, not code.)

### 5b. `score_sink` adapter (trace → result)

A new `app/score_sink.py` ([`examples/score_sink.py`](examples/score_sink.py))
exposes a tiny protocol:

```python
class ScoreSink(Protocol):
    def record(self, trace_id: str, name: str, value: float, *, comment: str | None = None) -> None: ...
```

- Default `NoopSink` — does nothing; selected when `EVAL_SCORE_SINK` is unset.
- `LangfuseSink` — the **only** place that imports the Langfuse SDK, behind a lazy
  import inside the factory so the import cost/dependency is paid solely when
  `EVAL_SCORE_SINK=langfuse`. It posts the score keyed by `traceId` via the scores
  API.

`evals.run` calls `sink.record(...)` per case. **Failures are non-fatal**
(log + carry on) so a flaky backend never fails the quality gate. Langfuse stores
scores keyed by `traceId` independent of ingest order, so a late-arriving trace
still binds (the eventual-consistency risk in the README).

> **Invariant test:** with `EVAL_SCORE_SINK` unset, importing `app.evals` /
> `app.observability` must import **no** vendor SDK. Enforced in `testing.md`.

---

## 6. PII posture — `OTEL_CAPTURE_CONTENT`

The only content-bearing span attributes today are `input.question` (set in
`agent.run`) and `retrieval.query` (set in `retrieve`); answer/completion text is
*not* captured on any span. A single `OTEL_CAPTURE_CONTENT` toggle (default `on`
for the sandbox; documented "turn off in any environment with real data") gates
their capture. Implement it **once in the `span()` helper** so a single switch
covers all current and future content attributes:

```python
_CAPTURE_CONTENT = os.environ.get("OTEL_CAPTURE_CONTENT", "1") not in ("0", "false", "False")
_CONTENT_KEYS = {"input.question", "retrieval.query"}  # extend as content attrs are added

# inside span(): skip key if key in _CONTENT_KEYS and not _CAPTURE_CONTENT
```

When off, those attributes are never set on the span, so they never leave the
process regardless of exporter. The toggle is exposed as `capture_content()` and
reused at the **other** content channel — the `score_sink` comment: `evals.run`
sends `comment=c["question"]` only when `capture_content()` is true, else
`comment=None`. (The score *value* is not content and is always sent.) So a single
switch covers span attributes **and** the trace→result push; the only
deliberately-ungated channel is the local `eval_results.question` column
(golden-set fixture, local Postgres). **Dual-instrumentation boundary:** the LiteLLM
gateway is a separate process and is *not* in the app trace; gateway-side
cost/token spans (LiteLLM OTel callback + W3C trace-context propagation) are a
deferred follow-up, out of scope here.

---

## 7. Schema: `eval_results`, created idempotently at runtime

```sql
CREATE TABLE IF NOT EXISTS eval_results (
    id        BIGSERIAL PRIMARY KEY,
    run_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    suite     TEXT,
    question  TEXT,
    score     NUMERIC,
    passed    BOOLEAN,
    trace_id  TEXT,
    trace_url TEXT
);
```

**Why `CREATE TABLE IF NOT EXISTS` at runtime, not just `db/init.sql`.**
`db/init.sql` is mounted into `/docker-entrypoint-initdb.d/`, which Postgres runs
**only on an empty data dir**. Appending the table to `init.sql` does *not* create
it on an existing `pgdata` volume (`make up` never wipes it; only `make down -v`
does). So a stack that already has the `documents` table would fail at the first
insert. Fix: `evals.run` runs `CREATE TABLE IF NOT EXISTS eval_results (…)` on its
own connection before the first insert (idempotent, every run), **and** the table
is added to `db/init.sql` for fresh stacks. Reuse the exact connection pattern
from `app/retrieval.py` — `psycopg.connect(settings.database_url, autocommit=True)`
(note `autocommit=True`). Treat persistence failures as **non-fatal** (log + carry
on) so a DB hiccup never fails the quality gate.

---

## 8. Edge cases & failure modes

| Case | Behavior |
|---|---|
| `OTEL_EXPORTER_OTLP_ENDPOINT` unset | No exporter attached; `flush()`/`force_flush()` is a no-op; spans created but go nowhere; `trace_id` still valid → `eval_results` rows still written with `trace_id`, `trace_url` NULL. App behaves exactly as today. |
| Backend down / OTLP export errors | `BatchSpanProcessor` logs and drops; eval gate unaffected (export is fire-and-forget). |
| DB unreachable during persist | Logged, skipped per-case; gate pass/fail contract unchanged. |
| `score_sink` post fails / backend slow | Logged, carry on; never fails the gate; score binds late when trace ingests (Langfuse keys scores by `traceId`). |
| Template set but `${PROJECT_ID}` unresolved | Treated as a config error at startup-substitution time; falls back to NULL `trace_url` + a warning rather than writing a broken URL. |
| Empty suite | No cases → no rows; `evals.run` flush still fires; mean handling unchanged from today. |
| `OTEL_CAPTURE_CONTENT=0` | `input.question` / `retrieval.query` never set on spans. |

---

## 9. Sequencing for the implementer

1. `app/observability.py`: `flush()`, `atexit` shutdown, `OTEL_CAPTURE_CONTENT`
   gating in `span()`. (No behavior change when endpoint unset.)
2. `app/agent.py`: call `flush()` at end of `__main__`.
3. `db/init.sql`: add `eval_results` (fresh-stack path).
4. `app/score_sink.py`: `ScoreSink` protocol, `NoopSink`, lazy `LangfuseSink`,
   `get_score_sink()` factory keyed on `EVAL_SCORE_SINK`.
5. `app/evals.py`: per-case root trace + `format_trace_id`; runtime
   `CREATE TABLE IF NOT EXISTS`; insert one row per case; `trace_url` from
   template; `sink.record(...)`; `flush()` in `run()`'s `finally`.
6. `docker-compose.yml`: profile-gated backend service (Langfuse default; Phoenix
   alternative) publishing its OTLP port.
7. `.env.example`: both endpoint forms, headers, `LANGFUSE_INIT_*`,
   `EVAL_SCORE_SINK`, `OTEL_CAPTURE_CONTENT`, `OTEL_TRACE_URL_TEMPLATE`.
8. `pyproject.toml`: add `langfuse` only as an **optional/dev** extra (the
   adapter lazy-imports it; the core app must not hard-depend on it).
9. Tests per [`testing.md`](testing.md).
