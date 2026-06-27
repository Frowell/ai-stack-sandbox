# Eval-set maturity — test & verification plan

How each acceptance criterion in [`README.md`](README.md) is proven, and how the
suite gates merge through the project's eval/CI gate. The illustrative test idiom
is in [`examples/example_tests.py`](examples/example_tests.py); design rationale in
[`design.md`](design.md).

This feature *is* test machinery, so "the tests" are partly the gate itself. There
are two layers:

- **Offline structural tests** — deterministic, no gateway. They exercise the
  schema validator, scorer routing, and gate logic with stubbed scores. They run
  in any CI job (including PR #3's `lint` job) and never skip.
- **The live merge gate** — `tests/test_evals.py::test_quality_gate` runs the real
  suite against the stack (`make up`) and asserts it passes. It skips **only** on
  gateway-unreachability. PR #3's secret-gated `eval-gate` workflow stands up
  Postgres + the gateway and runs it, so the gate enforces itself on PRs.

## How merge is gated

| Mechanism | Command | Enforced by |
|---|---|---|
| Local gate | `make eval` (`python -m app.evals`, non-zero exit on any blocking gate) | developer |
| pytest merge gate | `make test` / `uv run pytest -q` → `tests/test_evals.py` | developer + CI |
| CI eval-gate | `.github/workflows/*` stands up Postgres + gateway, runs `pytest` (PR #3) | branch protection requiring `eval-gate` |

Today there is no `.github/` in-tree; until PR #3 lands the gate runs locally. The
offline structural tests gate merge immediately (they need no stack).

## Fixtures needed

- `evals/gate_config.yaml` (shape: [`examples/example_gate_config.yaml`](examples/example_gate_config.yaml)).
- Grown `evals/golden.jsonl` (≥20 cases, ≥4 per high-value slice; shape:
  [`examples/example_golden.jsonl`](examples/example_golden.jsonl)).
- Committed `evals/baseline.json` (shape:
  [`examples/example_baseline.json`](examples/example_baseline.json)), produced by
  `make eval-baseline` on a vetted run.
- `tmp_path` golden files written inline by the offline tests (bad row, duplicate
  id, thin slice) — no committed fixture files needed for those.
- A `_pad_to_floors()` helper in the test module that builds a passing results
  list so individual gates can be perturbed in isolation (see example).

## Each acceptance criterion → its proof

| # | Acceptance criterion | Proof | Layer |
|---|---|---|---|
| 1 | Golden set ≥20, ≥4 per high-value slice; every row has unique stable `id`, `slice`, `weight`; schema validated (bad row / unknown slice / dup id → non-zero exit) | `test_unknown_slice_fails_validation`, `test_duplicate_id_fails_validation`, `test_high_value_slice_needs_four_cases`; plus a count assertion on the committed `golden.jsonl` | Offline |
| 2 | Decline slices carry `expect:"decline"` and use the decline judge, not keyword overlap | `test_decline_case_routes_to_decline_scorer` (asserts factual scorer never called) | Offline |
| 3 | Trace-sourced cases redacted per checklist; reviewer confirms in PR | **Process gate**, not an automated test: the README redaction checklist is ticked in the PR description and re-checked at review. Honest non-goal of automation this pass. | Review |
| 4 | `gate_config.yaml` holds floors, `slices.high_value`, weights policy, regression delta, N, price map, budgets; `THRESHOLD` removed from `app/evals.py` | Config schema is exercised by every offline test (they `load_config()`); a grep assertion that `THRESHOLD` no longer appears in `app/evals.py` | Offline |
| 5 | Gateway usage opt-in (`return_usage` / `chat_with_usage`) with default string return unchanged; no hot-path caller modified | Unit test: `chat(msgs)` returns `str`; `chat(msgs, return_usage=True)` returns `(str, usage)` with a stubbed `_client`. `app/agent.py` default path still returns `str` (existing `ask()` callers untouched). | Offline (stubbed client) |
| 6 | `baseline.json` populated by `make eval-baseline` (pins, temp=0, N≥3), committed, with per-case/slice/overall + pins + timestamp; baseline-diff gate active (not skipped) and applies the add/remove-`id` lifecycle | Schema/structure test on `baseline.json`; `test_injected_regression_fails_baseline_diff` proves the diff is *active*; an added-id and a removed-id case prove the lifecycle (added=exempt-from-delta, removed=ignored) | Offline + 1 live (regen) |
| 7 | A deliberately injected quality regression makes `make eval` / `pytest` exit non-zero via baseline-diff | `test_injected_regression_fails_baseline_diff` (offline, stubbed scores); end-to-end smoke: lower a known-good `reference`/answer and observe `make eval` exit 1 | Offline + manual smoke |
| 8 | A deliberately over-budget change (inflated price map / cost) fails the cost budget gate | `test_over_budget_cost_fails` | Offline |
| 9 | Per-slice floor breach in one high-value slice fails even when weighted overall passes | `test_slice_floor_breach_fails_even_when_overall_passes` (asserts overall ≥ floor yet gate fails) | Offline |
| 10 | `tests/test_evals.py` skips **only** on gateway-unreachability; forced scoring error / schema failure / gate breach with stack up → non-zero, not skip | `test_quality_gate` catches only `openai.APIConnectionError`; a test that injects a non-connection error and asserts it propagates as ERROR (not skip); a schema-error test asserts `run()` raises rather than skips | Offline + live |
| 11 | Cost gate is **`mean_cost_per_case`** (sum of served per-case costs ÷ #cases, each meaned over N), excluding judge cost; run total reported but **not** gated; judge tokens recorded but unbudgeted | `test_over_budget_cost_fails` asserts the failure names `mean cost/case`; unit test on `case_cost`/aggregation: judge `chat()` calls contribute 0; per-case cost is the mean of N sample costs; report carries both `mean_cost_per_case_usd` (gated) and `total_cost_usd` (reported) | Offline |

## Concrete example test (project idiom)

From [`examples/example_tests.py`](examples/example_tests.py) — the per-slice floor
breach, the AC that the single-mean gate could never express:

```python
def test_slice_floor_breach_fails_even_when_overall_passes():
    cfg = load_config()
    results = _pad_to_floors([])            # all slices comfortably above floor
    for r in results:
        if r["slice"] == "retrieval":
            r["score"] = 0.10               # tank ONE high-value slice
    report = aggregate_and_gate(cfg, results)
    assert report["overall"] >= cfg["overall"]["floor"]   # weighted overall still passes
    assert not report["passed"]                            # ...but the gate fails
    assert any("slice retrieval" in f for f in report["failures"])
```

This is the literal demonstration that the matured gate catches a regression the
old `mean >= 0.7` gate would have masked.

## The live merge-gate test (skip predicate is load-bearing)

```python
def test_quality_gate():
    import openai
    try:
        report = run()
    except openai.APIConnectionError:      # ONLY connection failure -> skip
        pytest.skip("gateway unreachable; eval gate not enforceable locally")
    assert report["passed"], f"quality gate failed: {report['failures']}"
```

A broad `except Exception: pytest.skip(...)` here would silently disable the merge
gate — the exact failure this feature exists to prevent (AC #10). The offline test
that injects a *non*-connection error and asserts it surfaces as ERROR (not skip)
is what guards against that regression in the test harness itself.

## What is NOT gated (honest)

- **Redaction (AC #3)** — manual review, no automated scrubber this pass.
- **p95 latency budget** — advisory only (small-N p95 noise, README open
  question); it prints an advisory line but does not fail the run.
- **id-stability / rename detection** — accepted evasion vector (design.md §4);
  mitigated by reviewed baseline regeneration, not automation.
- **Statistical significance per slice** — replaced by hard floor + baseline-diff
  delta until a slice reaches N≥20.

## Local verification sequence

```bash
make up                 # stand up postgres + gateway + redis
make ingest             # embed the corpus so retrieval has data
uv run pytest -q tests/test_evals.py   # offline tests pass; live gate runs with stack up
make eval               # human-readable report + exit code
make eval-baseline      # ONLY when deliberately re-baselining; review the diff, commit separately
```
