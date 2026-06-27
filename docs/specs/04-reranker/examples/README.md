# Examples — illustrative only

> **These files are a specification, not wired-in code.** They sketch *how* the
> design in [`../README.md`](../README.md) and [`../design.md`](../design.md)
> would land against the real files in `app/`, `gateway/`, `evals/`, and
> `pyproject.toml`. They are intentionally **not** importable from the app and
> must **not** be copied verbatim into `app/` as part of expanding this spec —
> implementation happens in a separate PR. Signatures and import paths match the
> current codebase so the gap to real code is small and obvious.

**Why these names:** there is no `[tool.pytest]`/`testpaths` in `pyproject.toml`,
so a bare `uv run pytest` discovers `test_*.py` **recursively from the repo root**,
including under `docs/specs/`. The example test file is therefore named
`example_tests.py` (not `test_*.py` / `*_test.py`) so default discovery does
**not** pick it up. Do not rename it into a collected pattern while it lives here.

| File | Illustrates | Real target |
| --- | --- | --- |
| [`config.py`](config.py) | new `Settings` fields (lowercase attr ← UPPER_SNAKE env) | `app/config.py` |
| [`retrieval.py`](retrieval.py) | dispatcher, per-call backend select, fail-open wrapper, `span("rerank")`, lazy local backend | `app/retrieval.py` |
| [`gateway.py`](gateway.py) | additive `rerank()` via `httpx` to `{gateway}/rerank` | `app/gateway.py` |
| [`litellm_config.snippet.yaml`](litellm_config.snippet.yaml) | the `rerank` model entry | `gateway/litellm_config.yaml` |
| [`pyproject.snippet.toml`](pyproject.snippet.toml) | the `rerank-local` uv dependency-group | `pyproject.toml` + `uv.lock` |
| [`retrieval_eval.py`](retrieval_eval.py) | hit@k / MRR harness, `source`→`id` resolution | new `evals/` harness |
| [`retrieval_gold.jsonl`](retrieval_gold.jsonl) | gold labels keyed on `source` | new `evals/retrieval_gold.jsonl` |
| [`example_tests.py`](example_tests.py) | acceptance-criteria tests in the project's pytest idiom | new `tests/test_rerank.py` |

The matching verification plan is in [`../testing.md`](../testing.md). When
implementing, port the relevant pieces into the real files — do not import from
this directory.
