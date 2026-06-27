# Testing & verification — Agent orchestration (multi-node)

How every acceptance criterion in [`README.md`](README.md) is proven, and how it
gates merge. Concrete example tests are in
[`examples/test_orchestration.py`](examples/test_orchestration.py) (illustrative).

## The gate

The merge gate is **`uv run pytest`** (`make test`). Two layers:

1. **Quality gate** — `tests/test_evals.py::test_quality_gate` runs the eval suite
   over `evals/golden.jsonl` and asserts `mean_score >= 0.7`. This must still pass
   in `multi` mode (AC: eval gate). `app/evals.py` exits non-zero on regression so
   CI blocks merge.
2. **Unit/integration gate** — a new `tests/test_orchestration.py` (the real home
   for the `examples/test_orchestration.py` sketch) covers the orchestration
   mechanics that the eval suite does not exercise (caps, resume, signatures).

CI wiring: per [spec 07 / PR #3](07-ci-hardening/README.md) the `eval-gate` job
stands up Postgres + the gateway via `docker compose` and runs the gate; `lint`
runs ruff always. There is **no `.github/` in the tree yet** — it lands with PR #3.
This spec adds tests to that existing gate; it does not introduce a new CI seam.
The `multi`-mode eval run is **dark-launched** (run, variance recorded) before the
default flips, per the rollout plan.

### Test layering vs. infrastructure

| Layer | Needs | How |
| --- | --- | --- |
| Unit (most cases) | nothing | swap `app.agent.settings` via `dataclasses.replace` (see config gotcha below) to set mode/caps; `monkeypatch` `app.gateway` (`chat`/`chat_with_usage`) and `app.agent.retrieve`; compile the multi graph with the **in-memory** checkpointer (`_build_multi()` with no saver) so caps/routing/signatures test without Postgres. For the token-budget unit test, also pin `agent.get_graph` to `lambda: agent._build_multi()` so `ask_resumable()` does **not** reach for `PostgresSaver`. |
| Integration (resume) | compose Postgres | `PostgresSaver`; gated by a `postgres_available` fixture that `pytest.skip`s when `settings.database_url` is unreachable, so the unit suite stays hermetic. |
| Eval | Postgres + gateway | existing `test_evals.py` launched with `ORCHESTRATION_MODE=multi` **set in the environment before the interpreter starts** (CI job env / subprocess), not via in-test `setenv`. |

> **Config gotcha (load-bearing).** `app.config` exposes a module-level *frozen*
> `settings = Settings()` constructed **at import time**, and each dataclass field
> default captures `os.environ.get(...)` **once at class-definition time**. Therefore
> `monkeypatch.setenv("MAX_ITERATIONS", …)` (or `ORCHESTRATION_MODE`, `TOKEN_BUDGET`)
> inside an already-imported test is a **no-op** — it changes neither the live
> `settings` nor a freshly built `Settings()`. Two consequences for the suite:
> 1. **In-process** tests must drive config by swapping the module global with a copy:
>    `monkeypatch.setattr(agent, "settings", dataclasses.replace(agent.settings, orchestration_mode="multi", max_iterations=3))`. `replace` works on frozen dataclasses; `_route`/`ask_resumable` read the module-level `settings`, so the rebind is seen. (The `recursion_limit` *property* is the lone field read from env at call time.)
> 2. **Env-driven** cases (eval-gate in `multi`, the single-mode and fresh-process
>    resume tests) must run in a **fresh interpreter** (CI job env or `subprocess`),
>    which re-reads env at import. The example `test_single_mode_import_opens_no_db`
>    and `test_resume_in_fresh_process` already shell out for exactly this reason.

Fixtures needed: `postgres_available` (probe `settings.database_url`), a
`use_settings(**overrides)` helper that does the `replace`-and-rebind above (see
`examples/test_orchestration.py::_use_settings`) plus a fake `ChatResult`-returning
gateway. Reset module-level graph caches (`agent._DURABLE_GRAPH`; rebuild
`agent.GRAPH` only if a test needs the import-time graph for the swapped mode)
between tests that flip the mode.

## Acceptance-criteria proof matrix

| # | Acceptance criterion (README) | Proof | Type |
| --- | --- | --- | --- |
| 1 | multi workflow (supervisor + ≥2 specialists) answers a golden question | eval suite run with `ORCHESTRATION_MODE=multi` passes the threshold; the supervisor→retrieve→research→synthesize path executes | eval / integration |
| 2 | interrupted run resumes by `thread_id` in a **fresh process**, no re-run of committed nodes | `test_resume_in_fresh_process`: compile with `interrupt_after=["retrieve"]`, capture `thread_id`, resume in a subprocess; assert complete answer + a persisted side-effect counter shows the committed node ran **once** | integration |
| 3 | per-node spans nest under the run span; resume carries `resumed_from`/link | in-process: use an OTel `InMemorySpanExporter`, assert child spans' parent == `agent.run` span; resume: assert new run span has `resumed_from` attribute + Link to the original `trace_id` | unit + integration |
| 4 | caps enforced; exceeding any routes to `truncate`, returns `truncated`/partial, **never** raises `GraphRecursionError` or hangs | `test_looping_supervisor_truncates_before_recursion_limit`: a supervisor that never says `done` ends with `truncated=True`, `iterations<=cap`, no exception | unit |
| 5 | `token_budget` consumes real `resp.usage`; small budget → `truncated=True` | `test_token_budget_truncates`: fake `chat_with_usage` returns large `total_tokens`; assert `res.truncated` | unit |
| 6 | `ask(question)->str` unchanged; `evals.py`/`__main__`/`test_evals.py` run unchanged | `test_ask_signature_unchanged` (inspect signature) + existing `test_evals.py` passes; resume exercised only via `ask_resumable` | unit |
| 7 | `single` mode = identical `retrieve->generate`, no checkpointer, no DB at import | `test_single_mode_import_opens_no_db` (import with unreachable DB succeeds) + assert `_build_single()` node set/edges equal today's `build_graph()` | unit |
| 8 | `langgraph-checkpoint-postgres` in `pyproject.toml`+lock; `import langgraph.checkpoint.postgres` succeeds | `test_postgres_saver_importable` (red today, green after the dep lands) + `uv sync --locked` succeeds in CI | unit + CI |
| 9 | invalid/out-of-range route falls back instead of crashing | `test_invalid_route_falls_back_to_truncate` asserts `_route({"next":"bogus"}) == "truncate"` | unit |
| 10 | eval gate still passes in `multi`, not flaky | run `app/evals.py` N times in `multi`; record mean/variance vs `single`; gate green; grow golden set / seed judge if variance threatens threshold | eval |
| 11 | checkpoint tables created by idempotent setup, not by hand; `make up` on clean volume boots | call `ensure_checkpoint_schema()` twice → no error, tables present (`\dt` shows `checkpoints*`); `make down -v && make up` boots with `db/init.sql` untouched | integration / manual |

## Notes on specific tests

- **State round-trips through the saver.** A unit test invokes a one-step multi
  graph with the `PostgresSaver`, then `get_state(config)` and asserts `context`
  (a `list[tuple[int,str]]`) survives serialise/deserialise with the expected
  shape — tuples may come back as lists (design.md §2); normalise or assert
  accordingly.
- **No `GraphRecursionError`.** AC #4 asserts the *absence* of that exception: the
  test would fail loudly if `recursion_limit` were misconfigured at/below the cap
  ceiling, which is exactly the regression we are guarding.
- **Variance measurement (AC #10).** Not a hard assertion in the merge gate;
  produce a short report (mean/stdev over repeated `multi` runs) attached to the
  PR. The gate stays the existing `mean_score >= 0.7`; if `multi` variance pushes
  it under, the remedy is grow `evals/golden.jsonl` (spec 06) or seed/temperature-0
  the judge — not loosen the threshold silently.
- **Idempotency of `setup()` (AC #11).** Calling it on an existing schema must be a
  no-op; the test calls it twice in one process. This is what lets it double as the
  migration without touching `db/init.sql`.

## What is explicitly NOT tested here (per README scope)

- Horizontal/distributed execution, queueing, human-in-the-loop interrupts
  (out of scope).
- LLM output determinism — AC #7 asserts identical graph *wiring*, not identical
  generated text.
- Checkpoint retention/growth — accepted while default stays `single`; a retention
  job (and its test) is a prerequisite for flipping the default, tracked as an open
  question, not part of this slice's gate.
