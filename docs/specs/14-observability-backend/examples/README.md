# Examples — illustrative only

These files are a **spec, not shipped code.** They show the intended shapes,
signatures, and file paths for the [observability-backend](../README.md) feature
so a reviewer can judge the design and an implementer has a concrete target. They
are **not** wired into the app and are **not** collected by the test suite:

- They live under `docs/specs/…`, outside the `app/` package and the `tests/`
  dir that `uv run pytest` runs against.
- The example test file is named `example_tests.py` (not `test_*.py` /
  `*_test.py`), so pytest's default discovery does **not** pick it up. Do not
  rename it into a collected pattern while it lives here.
- Do **not** `import` from this directory. Port the relevant pieces into the real
  files under `app/`, `db/`, `tests/`, `pyproject.toml`, `docker-compose.yml`,
  and `.env.example`.

| File | Mirrors / would change | Shows |
| --- | --- | --- |
| [`observability.py`](observability.py) | `app/observability.py` | `flush()`, `atexit` shutdown, `OTEL_CAPTURE_CONTENT` gating in `span()` |
| [`score_sink.py`](score_sink.py) | new `app/score_sink.py` | `ScoreSink` protocol, `NoopSink`, lazy `LangfuseSink`, `get_score_sink()` |
| [`evals.py`](evals.py) | `app/evals.py` | per-case root trace + `format_trace_id`, runtime `CREATE TABLE IF NOT EXISTS`, per-case insert, `trace_url` from template, `score_sink`, flush in `finally` |
| [`agent.py.snippet`](agent.py.snippet) | `app/agent.py` | the one-line `flush()` added to `__main__` |
| [`db_init.sql.snippet`](db_init.sql.snippet) | `db/init.sql` | the `eval_results` table (fresh-stack path) |
| [`docker-compose.observability.yaml`](docker-compose.observability.yaml) | `docker-compose.yml` | profile-gated Langfuse (default) + Phoenix (alternative) services that publish their OTLP port |
| [`env.example.snippet`](env.example.snippet) | `.env.example` | both endpoint forms, headers, `LANGFUSE_INIT_*`, `EVAL_SCORE_SINK`, `OTEL_CAPTURE_CONTENT`, `OTEL_TRACE_URL_TEMPLATE` |
| [`pyproject.snippet.toml`](pyproject.snippet.toml) | `pyproject.toml` | `langfuse` as an **optional** extra (lazy-imported by the adapter only) |
| [`example_tests.py`](example_tests.py) | `tests/test_evals.py` (+ new `tests/test_observability.py`) | offline stubbed tests proving each acceptance criterion |

When implementing, port the pieces into the real files — do not import from this
directory.
