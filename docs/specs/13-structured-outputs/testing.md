# Structured outputs — test & verification plan

> Companion to [`README.md`](README.md) and [`design.md`](design.md). This file is
> the proof plan: it maps **every acceptance criterion** to a concrete test, says
> what fixtures are needed, and explains how it **gates merge**. Concrete test
> bodies are in [`examples/example_tests.py`](examples/example_tests.py)
> (illustrative; not collected by pytest while it lives under `docs/`).

## How the merge gate works in this repo (ground truth)

There are two distinct "gates," and this spec touches both:

1. **The eval gate** — `app/evals.run()` runs the agent over `evals/golden.jsonl`,
   scores each case, and exits non-zero on regression. It is invoked two ways:
   - `make eval` → `uv run python -m app.evals` (the CLI / `__main__` path).
   - `make test` → `uv run pytest -q`, which collects
     `tests/test_evals.py::test_quality_gate`, which calls `run()` and asserts
     `report["passed"]`. **This is the literal "evals as a merge gate."**
2. **CI** — `.github/workflows/ci.yml` is **not on `main` yet**; it lands with
   roadmap **PR #3** (branch `ci/github-actions`) and is hardened by
   [spec 07-ci-hardening](../07-ci-hardening/README.md). Per spec 07, CI runs
   `lint` (ruff) on every PR and a secret-gated `eval-gate` job that stands up
   pgvector + LiteLLM and runs the eval suite; the required check is the
   always-running `eval-gate-result` summary job. **This spec adds no new CI
   workflow** — it makes the *existing* pytest gate more honest (offline-safe
   unit tests; a live test that skips instead of erroring without a key) and gives
   the gate a labeled failure reason (`gate_status`).

Implication for this spec's tests: the **new** unit tests must pass under
`uv run pytest` with **no network and no API key** (they run in the `lint`-class
environment too), and the **existing** live `test_quality_gate` must not silently
require network — it is reconciled to skip-without-key (chosen option, below).

### Where the new tests live

| File | Purpose | Network? |
| --- | --- | --- |
| `tests/test_structured_outputs.py` (new) | unit tests for `chat_structured`, `_strict_schema`, `run()` error handling | **No** — stubs the seam |
| `tests/test_evals.py` (edited) | the existing live `test_quality_gate`, reconciled to skip cleanly offline | skips without key |

### Fixtures needed

- **`monkeypatch`** (pytest builtin) to replace
  `app.gateway._client.chat.completions.create` with a stub returning a canned
  `choices[0].message.content`. No new fixture files.
- A **`tmp_path` golden file** (1–2 JSONL lines) for `run()` tests, so they don't
  depend on the real `evals/golden.jsonl` or on `ask()` calling the gateway
  (`ask` is monkeypatched to a constant). No network, no DB.
- The `_resp(content)` helper (a `SimpleNamespace` mimicking the OpenAI response
  shape) — see `examples/example_tests.py`.

## Acceptance criterion → proof

Each row ties an AC from the README to the test that proves it.

The row numbers below match the **13** acceptance-criteria checkboxes in
`README.md` in order (AC #3 is the retry role-alternation criterion — it has its
own row here so the proof table can't silently skip a load-bearing AC).

| # | Acceptance criterion | Proven by |
| --- | --- | --- |
| 1 | `chat_structured` takes a pydantic schema, returns a validated instance, routes through the existing gateway client (no provider named) | `test_valid_json_returns_typed_instance` asserts `isinstance(out, JudgeVerdict)`; the stub is on `gateway._client...create`, so it routes through the seam. Reviewer confirms no provider string in `app/`. |
| 2 | Non-matching output raises typed `StructuredOutputError` after one bounded retry — never a string / silent default | `test_dropped_param_freetext_raises` asserts `pytest.raises(StructuredOutputError)` **and** `create` called twice (`calls["n"] == 2`); `test_invalid_then_valid_succeeds_after_retry` proves the same retry can also recover. |
| 3 | The corrective retry preserves role alternation: rejected reply as an `assistant` turn then a `user` corrective turn (no 2nd consecutive `user`, no 2nd `system`), valid under both the OpenAI and the advertised Anthropic mapping of the `chat` alias | `test_retry_preserves_role_alternation` captures the **second** `create` call's `messages` and asserts no two consecutive same-role turns, exactly one `system` turn, and that the last turn is the `user` corrective. |
| 4 | Correct even when provider drops `response_format` (free text returned, no transport error) | `test_dropped_param_freetext_raises`: the stub returns prose with **no** exception — exactly the dropped-param shape — and validation still rejects it. |
| 5 | Schema under `strict:true` carries `additionalProperties:false` + all fields in `required` | `test_strict_schema_shape` asserts `additionalProperties is False`, `required == ["score"]`, `title` stripped. Plus `test_strict_schema_rejects_nested` proves the v1 `$defs` guard fails loud (design.md §4). |
| 6 | `judge_score` uses the structured path; a malformed/refused reply propagates and `run()` records a per-case `error` (no more `0.0`) | `test_judge_error_excluded_from_mean`: q2's judge raises → case has `score is None`, `error` set. |
| 7 | `mean_score` over evaluated (non-errored) cases only; `None` if all error | `test_judge_error_excluded_from_mean` (mean == 1.0, q2 not averaged as 0.0) + `test_all_cases_error_mean_is_none` (`mean_score is None`, no div-by-zero). |
| 8 | CLI/format path tolerates `None` (no `:.2f`/`None >= THRESHOLD` crash); a test exercises the report-print path with an errored case | `test_format_report_is_none_safe` calls the importable `format_report` helper directly (the formatting must NOT be trapped inside `__main__`). The errored case prints `ERR`, the summary prints `n/a` when mean is `None`, a valid case still formats `0.90`. |
| 9 | Gate fails (`passed == False`, non-zero exit) on any errored case; report distinguishes `eval_error` from `quality_fail` | `test_judge_error_excluded_from_mean` asserts `gate_status == "eval_error"` and `passed is False`; **`test_low_valid_score_is_quality_fail`** asserts a low-but-valid score with no errored case labels `gate_status == "quality_fail"` — proving **both** branches of the distinction, not just the infra one. |
| 10 | `pydantic>=2` declared in `pyproject.toml` `[project].dependencies` | Static check: a one-line test reads `pyproject.toml` and asserts a `pydantic` entry, **or** simply rely on review + `uv lock --check` in CI (spec 07). See `examples/pyproject.snippet.toml`. |
| 11 | New unit tests run with no network/key by stubbing the seam (valid / invalid / dropped-param), invalid case accounts for the double `create` | All `test_*` unit tests in `example_tests.py` use `monkeypatch`; none import a key or open a socket. Verified by running them with env unset (command below). |
| 12 | The existing `test_quality_gate` no longer silently needs network: chosen option stated | **Chosen: option (a) — skip-without-key.** `test_quality_gate` calls `pytest.skip(...)` when `OPENAI_API_KEY` is unset, so `uv run pytest` is green offline (skip clearly reported) and green online. Rationale below. |
| 13 | `gateway/litellm_config.yaml` documents the `drop_params` ↔ `response_format` interaction in a comment | Reviewer check against `examples/litellm_config.snippet.yaml`. Not unit-testable; verified in PR review. |

### Why option (a) for AC #11 (skip-without-key)

The README offers (a) skip without key, or (b) stub the seam in the live test.
**Choose (a).** `test_quality_gate` exists specifically to be the *live*,
end-to-end "does real answer quality hold" gate — stubbing the seam there (b)
would turn it into a unit test and delete the only real-LLM coverage, duplicating
what the new offline tests already do. Keeping it live-but-skipped preserves its
purpose: it runs for real in CI's secret-gated `eval-gate` job (spec 07), and
skips cleanly on a developer laptop / fork PR with no key. The new offline unit
tests (`tests/test_structured_outputs.py`) provide the deterministic coverage; the
live test provides the real-quality coverage. Different jobs, different purposes.

## Test layers (summary)

- **Unit (no network) — the bulk, and the proof the schema is authoritative.**
  Stub `create`; cover (a) valid JSON → typed instance, (b) invalid → typed error
  after retry, (c) dropped-param free text → same typed error, (d) strict-schema
  shape + nested-reject, (e) `run()` error channel / mean exclusion / `None`
  mean / `gate_status` (both `eval_error` **and** `quality_fail`) / `None`-safe
  formatting, (f) the retry's cross-provider role-alternation shape. Fast,
  deterministic, key-free.
- **Integration (opt-in, needs key).** The reconciled `test_quality_gate` runs
  `judge_score` against the live gateway for the configured model — this is where
  the README open question is settled: confirm the proxy forwards
  `{"type":"json_schema"}` strict for `gpt-4o-mini`, else fall back to
  `json_object` + the *same* client-side validation. Run via the secret-gated CI
  `eval-gate` job, or locally with a key.
- **Eval gate.** Unchanged contract (pass/fail + score), now with a labeled
  `gate_status`. The new error-path test is the regression guard for the original
  bug: a judge failure must surface as `eval_error`, never a `0.0` that masquerades
  as a quality drop.

## Commands

```bash
# Offline unit tests only — must pass with NO key, NO gateway:
uv run pytest tests/test_structured_outputs.py -q

# Full suite offline — green, with test_quality_gate reported as SKIPPED:
uv run pytest -q

# Full suite online (key + gateway up) — test_quality_gate runs for real:
OPENAI_API_KEY=sk-... make up && make test

# The CLI / __main__ path (exercises the None-safe formatting + exit code):
make eval        # uv run python -m app.evals ; exit 0 on pass, non-zero on fail
```

## A concrete example test (project idiom)

The original `tests/test_evals.py` is a single live assertion. The new offline
idiom monkeypatches the seam — this is the regression test for the original bug
(verbatim from `examples/example_tests.py`):

```python
def test_judge_error_excluded_from_mean(monkeypatch, tmp_path):
    from app import evals
    golden = tmp_path / "g.jsonl"
    golden.write_text(
        '{"question": "q1", "keywords": ["a"], "reference": "r1"}\n'
        '{"question": "q2", "keywords": ["a"], "reference": "r2"}\n'
    )
    monkeypatch.setattr(evals, "ask", lambda q: "a")

    def judge(question, answer, reference):
        if question == "q2":
            raise evals.StructuredOutputError("JudgeVerdict", "prose", cause=None)
        return 1.0
    monkeypatch.setattr(evals, "judge_score", judge)

    report = evals.run(str(golden))
    cases = {c["question"]: c for c in report["cases"]}
    assert cases["q2"]["score"] is None and cases["q2"]["error"] is not None
    assert report["mean_score"] == 1.0          # q2 NOT averaged in as 0.0 (the bug)
    assert report["gate_status"] == "eval_error"
    assert report["passed"] is False            # fail loud, but labeled
```

## Gaps / honest unknowns

- **AC #12 and the `pydantic>=2` line** are verified by review + `uv lock --check`
  (spec 07), not by a runtime test — a config comment and a manifest line aren't
  meaningfully unit-testable. Stated so it isn't mistaken for covered-by-test.
- **The `json_schema`-forwarding open question** can only be settled against the
  *running* gateway (integration layer), not offline. Until then the offline
  tests prove the *fallback* behavior (validation rejects non-conforming text)
  regardless of which `response_format` variant the proxy honors.
- **Empty-content retry edge (not covered by an offline test).** A refusal can
  return *empty* content; the corrective retry then re-appends an empty `assistant`
  turn, which Anthropic (the advertised alias swap) rejects with a 400. The example
  guards this by substituting a placeholder string (`example_gateway.py`), but the
  placeholder substitution itself is not asserted by a unit test — it would need a
  stub returning `""` then valid JSON and an inspection of the second call's
  assistant content. Cheap to add during implementation; recorded here so it isn't
  mistaken for proven.
