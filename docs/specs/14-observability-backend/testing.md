# Observability backend — test & verification plan

How each [acceptance criterion](README.md#acceptance-criteria) is proven, the
fixtures/harness needed, and how it gates merge. Illustrative code is in
[`examples/`](examples/) (`observability.py`, `evals.py`, `score_sink.py`,
`db_init.sql.snippet`, `docker-compose.observability.yaml`, `env.example.snippet`,
and [`example_tests.py`](examples/example_tests.py) — the offline, hermetic tests
for every criterion below); port the tests to `tests/test_observability.py` when
implementing.

## The gate today (and what this adds)

"CI" is `uv run pytest` (Makefile `test:`); the literal merge gate is
`tests/test_evals.py::test_quality_gate`, unchanged here.

This feature adds **hermetic** tests in `tests/test_observability.py` plus a small
extension to the eval path. The key testing principle: **CI never stands up the
Langfuse stack.** Span delivery is asserted against a **lightweight in-process
OTLP sink** (`InMemorySpanExporter`), so the suite needs no backend, no network,
and no keys. The heavy backend is `profiles:`-gated and only exercised in
optional, manual rollout checks.

**Two non-obvious hermeticity points (a naïve port gets both wrong):**

1. **`evals.run()` is not hermetic by itself.** It calls `ask()` → `retrieve()`
   (Postgres) + `chat()`/`embed()` (the gateway). The unit tests therefore
   **monkeypatch `app.evals.ask`** to a canned answer and use a *questions-only*
   fixture suite (no `reference` ⇒ `judge_score` short-circuits without a gateway
   call; no `keywords` ⇒ `keyword_score` returns 1.0). With `ask` stubbed and the
   endpoint unset, `run()` touches no Postgres, no gateway, no network. (DB
   *persistence* is best-effort/non-fatal, so its absence does not fail the run;
   the rows themselves are asserted via the DB-gated tests below.)
2. **The flush test must use a *buffering* processor.** `SimpleSpanProcessor`
   exports on span-end synchronously, so a flush test built on it would pass even
   **without** the fix and prove nothing. Criterion 2 therefore attaches a
   `BatchSpanProcessor` with a long `schedule_delay_millis` (so the background
   timer never fires during the test): the `evals.run` span reaches the exporter
   **only** because of the explicit `flush()` in `run()`'s `finally`. The other
   in-proc tests (content toggle) may use `SimpleSpanProcessor` since they do not
   depend on flush timing.

| Layer | Stack needed | Runs in the gate? |
| --- | --- | --- |
| `tests/test_observability.py` (flush, trace_id, sink, content toggle) — `ask` stubbed | none (in-proc exporter) | **yes**, always |
| persistence / migration-path tests (`eval_results` rows, populated-volume create) | Postgres only | yes when DB present; else skipped |
| live Langfuse trace→score | full observability profile | **no** — manual rollout check |

## Fixtures / harness

- **`InMemorySpanExporter`** (`opentelemetry.sdk.trace.export.in_memory_span_exporter`)
  attached to the existing provider to capture exported spans without a collector —
  via `BatchSpanProcessor(schedule_delay_millis=<large>)` for the flush test (so
  only the explicit flush exports), via `SimpleSpanProcessor` for the content-toggle
  test (timing-independent).
- **Monkeypatched `app.evals.ask`** + a tmp questions-only fixture suite, so
  `run()` makes no retrieval/gateway calls (see hermeticity point 1 above).
- **No endpoint set** for the default-path tests (exporter gated off) — proves
  `trace_id` is valid regardless of export. (The exporter is decided at
  import-time in `observability.py`; CI imports with the endpoint unset, so no
  OTLP exporter is ever attached during the suite.)
- **A Postgres** (the compose `postgres`, already published to host) for the
  `eval_results` persistence + migration tests; gate them on `DATABASE_URL` being
  reachable and `skip` otherwise so the suite stays hermetic by default.
- **Stub `score_sink`** to assert the default is a no-op and that the OTLP path
  imports no vendor SDK (`sys.modules` assertion).

## Acceptance criteria → proof

| # | Criterion | How it is proven |
| --- | --- | --- |
| 1 | Profile `make up` starts backend + full dep set + bootstrap; host `make ask`/`make eval` spans appear with only env set | **Manual rollout check** (heavy stack; not in CI). Documented in Rollout below; the wiring it depends on (host-published OTLP port, `.env` localhost endpoint, `LANGFUSE_INIT_*` keys+project id) is reviewed against `examples/docker-compose.observability.yaml` + `env.example.snippet` |
| 2 | Short-lived run reliably exports (flush fix) | `test_run_flushes_spans`: attach `InMemorySpanExporter` via a **`BatchSpanProcessor` with a long `schedule_delay_millis`** (so the timer never fires), monkeypatch `ask`, call `evals.run()`, assert the `evals.run` span was exported — present **only** because of the `finally: flush()` in `run()` (a `SimpleSpanProcessor` here would pass even without the fix and prove nothing) |
| 3 | Every case persisted with non-null, **distinct per-case** `trace_id`, even with no endpoint | `test_trace_ids_distinct` (hermetic): run a ≥2-case suite with endpoint unset and `ask` stubbed; assert the **returned** `report["cases"]` each carry a non-null `trace_id` and the ids are **distinct** (guards the shared-trace collision — no DB needed). The DB-persisted form (rows in `eval_results`) is covered by the Postgres-gated `test_eval_results_created_on_existing_db` (#7) |
| 4 | `trace_url` set when template configured, null + usable `trace_id` otherwise | `test_trace_url_from_template` (set the template, assert the formatted URL contains the `trace_id`) + `test_trace_url_null_without_template` |
| 5 | Trace→result score via sink; sink off ⇒ OTLP works + no vendor import | `test_score_sink_noop_default` (default no-op) + `test_otlp_path_imports_no_vendor_sdk` (`langfuse` not in `sys.modules` after a run with the sink disabled). Live score-on-trace is a manual check |
| 6 | Endpoint unset ⇒ behaves as today; core 4-container stack unchanged | `test_no_endpoint_no_export_no_error` (run with endpoint unset: no export, no raise) + review that the backend service is `profiles:`-gated (absent from default `make up`) |
| 7 | `eval_results` auto-created on an **already-populated** `pgdata` volume | `test_eval_results_created_on_existing_db`: against a `documents`-only DB, run `evals.run`, assert `eval_results` exists and is populated (exercises the runtime `CREATE TABLE IF NOT EXISTS`, not just `db/init.sql`) |
| 8 | `OTEL_CAPTURE_CONTENT=off` ⇒ no content **span attributes** exported | `test_capture_content_off`: exercise the `span()` helper directly with the toggle off (`span("t", **{"input.question": "secret", "other": "ok"})`) and assert the exported span drops `input.question`/`retrieval.query` but keeps non-content attrs. (Testing `span()` directly is deliberate: with `ask` stubbed in the hermetic `run()` tests the `agent.run`/`retrieve` spans that carry those keys are never created, so a `run()`-based assertion would pass vacuously.) The same toggle gating the `score_sink` comment is covered by `test_score_comment_gated_by_capture_content`. Scoped to span export + sink comment; the local `eval_results.question` column is out of scope per PII posture |

## Notes on the subtle parts

- **Per-case root trace.** `trace_id` is constant across a trace tree, so the test
  in #3 is what forces the design's "each case is its own root trace" — without it
  every row shares one id and the test reds.
- **Flush timeout.** `force_flush()` is called with a short `timeout_millis` and
  failures are non-fatal; a test points the exporter at a dead endpoint and asserts
  `run()` still returns its result promptly (guards the "flush blocks exit" risk).
- **Non-fatal persistence.** A test makes the `eval_results` insert fail and
  asserts the quality gate still returns pass/fail (DB hiccup never fails the gate).

## Example test (project idiom)

Plain pytest, mirroring `tests/test_evals.py`:

```python
def test_run_flushes_spans(tmp_path, monkeypatch):
    # questions-only fixture => no reference/keywords => no gateway call
    suite = tmp_path / "mini.jsonl"
    suite.write_text('{"question": "q1"}\n{"question": "q2"}\n')
    monkeypatch.setattr(evals, "ask", lambda q: f"answer to {q}")  # no retrieve/gateway

    exporter = InMemorySpanExporter()
    # BUFFERING processor + a long delay so the timer never fires during the test:
    # the span lands ONLY because of the explicit flush() in run()'s finally.
    # (SimpleSpanProcessor would export on span-end and pass even without the fix.)
    proc = BatchSpanProcessor(exporter, schedule_delay_millis=60_000)
    observability._provider.add_span_processor(proc)
    try:
        evals.run(path=str(suite))        # endpoint unset; finally: flush() exports
        names = {s.name for s in exporter.get_finished_spans()}
        assert "evals.run" in names       # empty today (no flush on the run path)
    finally:
        proc.shutdown()
```

## Rollout verification (manual, profile-gated)

- `make up` with `--profile observability`: confirm the **full** Langfuse v3 set
  (web + worker + ClickHouse + Redis + object store, dedicated DB) boots and the
  `LANGFUSE_INIT_*` bootstrap created the project + fixed keys/id.
- With the `.env` localhost OTLP endpoint set, run `make eval` on the host and
  confirm traces appear in the Langfuse UI with **no** manual project/key step.
- `EVAL_SCORE_SINK=langfuse`: confirm the eval score shows on its trace, and the
  `eval_results.trace_url` opens that trace.
- Swap to Phoenix via env only and confirm the OTLP (result→trace) direction still
  works (no scores) — proves backend-agnosticism.
- Reversible: unset `OTEL_EXPORTER_OTLP_ENDPOINT` → app behaves exactly as today.
