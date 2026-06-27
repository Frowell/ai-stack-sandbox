# Examples — illustrative only

> **These files are a specification, not wired-in code.** They are sketches that
> show *how* the design in [`../README.md`](../README.md) and
> [`../design.md`](../design.md) would land against the real files in `app/`,
> `gateway/`, and `pyproject.toml`. They are intentionally **not** importable from
> the app and must **not** be copied verbatim into `app/` as part of expanding this
> spec — implementation happens in a separate PR. Signatures and import paths match
> the current codebase (langgraph 1.2.6, langgraph-checkpoint 4.1.1) so the gap to
> real code is small and obvious.

| File | Illustrates | Real target |
| --- | --- | --- |
| `config.py` | new `Settings` fields | `app/config.py` |
| `gateway_usage.py` | additive usage-returning call | `app/gateway.py` |
| `agent_multi.py` | supervisor + specialists + caps + lazy checkpointer + resumable API | `app/agent.py` |
| `checkpointer_setup.py` | idempotent `setup()` at process start | startup hook in `app/agent.py` |
| `observability_resume.py` | span nesting + `resumed_from` link across resume | `app/observability.py` |
| `pyproject_dependency.toml` | the dependency + lock step | `pyproject.toml` + `uv.lock` |
| `test_orchestration.py` | acceptance-criteria tests in the project's pytest idiom | `tests/test_orchestration.py` |

The matching verification plan is in [`../testing.md`](../testing.md).
