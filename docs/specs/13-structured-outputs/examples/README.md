# Examples — illustrative only

These files are a **spec, not shipped code.** They show the intended shapes,
signatures, and file paths for the [structured-outputs](../README.md) feature so a
reviewer can judge the design and an implementer has a concrete target. They are
**not** wired into the app and are **not** collected by the test suite:

- They live under `docs/specs/…`, outside the `app/` package and the `tests/`
  dir that `uv run pytest` is run against.
- The example test file is named `example_tests.py` (not `test_*.py` / `*_test.py`),
  so pytest's default discovery does **not** pick it up. Do not rename it into a
  collected pattern while it lives here.

| File | Mirrors / would change | Shows |
| --- | --- | --- |
| [`example_gateway.py`](example_gateway.py) | `app/gateway.py` | `StructuredOutputError`, `_strict_schema`, `chat_structured` with the bounded retry |
| [`example_evals.py`](example_evals.py) | `app/evals.py` | `JudgeVerdict`, the refactored `judge_score`/`run`, the `None`-safe `__main__` |
| [`example_tests.py`](example_tests.py) | `tests/test_evals.py` (+ a new `tests/test_structured_outputs.py`) | offline stubbed tests proving each acceptance criterion |
| [`litellm_config.snippet.yaml`](litellm_config.snippet.yaml) | `gateway/litellm_config.yaml` | the `drop_params` ↔ `response_format` comment |
| [`pyproject.snippet.toml`](pyproject.snippet.toml) | `pyproject.toml` | the `pydantic>=2` dependency line |

When implementing, port the relevant pieces into the real files under `app/`,
`tests/`, `gateway/`, and `pyproject.toml` — do not import from this directory.
