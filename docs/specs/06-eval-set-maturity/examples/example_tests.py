"""ILLUSTRATIVE — spec for spec 06, would replace tests/test_evals.py. Not wired in.

Two kinds of test:
  (A) OFFLINE, deterministic tests of the schema validator / scorer routing /
      gate logic — these need NO gateway and run in plain CI (and in PR #3's lint
      job). They prove the structural acceptance criteria.
  (B) The LIVE merge gate — one test that runs the real suite and asserts it
      passes, skipping ONLY on gateway-unreachability. This is the literal
      "evals as a merge gate" and the thing PR #3 enforces with the stack up.

The critical property (design.md §8, AC): the live gate skips on connection error
ONLY. A schema error, scoring error, or any gate breach must surface as ERROR/FAIL,
never a skip — a broad `except: pytest.skip` would silently disable the gate.

Renamed example_tests.py so pytest does not collect it from docs/. Port into
tests/test_evals.py.
"""
import pytest

# Real imports once ported:
# from app.evals import run, load_cases, load_config, score_case, aggregate_and_gate, GoldenSchemaError


# --------------------------------------------------------------------------- #
# (A) OFFLINE structural tests — no gateway needed
# --------------------------------------------------------------------------- #
def test_unknown_slice_fails_validation(tmp_path):
    """AC: bad row / unknown slice -> non-zero exit (raised before any model call)."""
    cfg = load_config()
    bad = tmp_path / "golden.jsonl"
    bad.write_text('{"id":"x1","slice":"not-a-slice","weight":1,"question":"q"}\n')
    with pytest.raises(GoldenSchemaError):
        load_cases(cfg, str(bad))


def test_duplicate_id_fails_validation(tmp_path):
    """AC: duplicate id -> non-zero exit."""
    cfg = load_config()
    bad = tmp_path / "golden.jsonl"
    bad.write_text('{"id":"dup","slice":"retrieval","weight":1,"question":"a"}\n'
                   '{"id":"dup","slice":"retrieval","weight":1,"question":"b"}\n')
    with pytest.raises(GoldenSchemaError):
        load_cases(cfg, str(bad))


def test_high_value_slice_needs_four_cases(tmp_path):
    """AC: >=4 cases per high-value slice, enforced at load."""
    cfg = load_config()
    thin = tmp_path / "golden.jsonl"
    thin.write_text('{"id":"r1","slice":"retrieval","weight":1,"question":"q"}\n')
    with pytest.raises(GoldenSchemaError):
        load_cases(cfg, str(thin))


def test_decline_case_routes_to_decline_scorer(monkeypatch):
    """AC: decline slices scored by decline judge, not keyword overlap."""
    import app.evals as ev
    calls = {"factual": 0, "decline": 0}
    monkeypatch.setattr(ev, "judge_score", lambda *a, **k: calls.__setitem__("factual", calls["factual"] + 1) or 1.0)
    monkeypatch.setattr(ev, "decline_judge_score", lambda *a, **k: calls.__setitem__("decline", calls["decline"] + 1) or 1.0)
    ev.score_case({"expect": "decline", "question": "q", "keywords": ["x"]}, "I can't answer that.")
    assert calls == {"factual": 0, "decline": 1}  # factual scorer never called


def test_injected_regression_fails_baseline_diff(monkeypatch):
    """AC: a lowered known-good answer trips the baseline-diff gate (offline, stubbed)."""
    cfg = load_config()
    cfg["regression"]["delta"] = 0.05
    # one case that scored 0.90 in baseline now scores 0.50 -> drop 0.40 > delta
    results = [{"id": "retr-001", "slice": "retrieval", "weight": 3,
                "score": 0.50, "cost_usd": 0.0001, "latency_ms": 1000, "usage_estimated": False}]
    # ... pad remaining high-value slices to satisfy floors; see helper in real test
    report = aggregate_and_gate(cfg, _pad_to_floors(results))
    assert not report["passed"]
    assert any("retr-001 regressed" in f for f in report["failures"])


def test_over_budget_cost_fails(monkeypatch):
    """AC: an inflated per-case served cost trips the cost budget gate (offline, stubbed).

    Gate is on mean_cost_per_case, not run total (README §4 / AC #11) — setting a
    per-case cost above the per-case budget trips it regardless of suite size.
    """
    cfg = load_config()
    cfg["budgets"]["mean_cost_per_case_usd"] = 0.001
    results = _pad_to_floors([])
    for r in results:
        r["cost_usd"] = 0.01  # blow the per-case budget
    report = aggregate_and_gate(cfg, results)
    assert not report["passed"]
    assert any("mean cost/case" in f for f in report["failures"])


def test_slice_floor_breach_fails_even_when_overall_passes():
    """AC: one high-value slice below floor fails even if weighted overall passes."""
    cfg = load_config()
    results = _pad_to_floors([])
    for r in results:
        if r["slice"] == "retrieval":
            r["score"] = 0.10              # tank retrieval below its floor
    report = aggregate_and_gate(cfg, results)
    assert report["overall"] >= cfg["overall"]["floor"]  # overall still fine
    assert not report["passed"]
    assert any("slice retrieval" in f for f in report["failures"])


# --------------------------------------------------------------------------- #
# (B) LIVE merge gate — skips ONLY on gateway-unreachability
# --------------------------------------------------------------------------- #
def test_quality_gate():
    """The merge gate. Requires the stack up (make up). In PR #3 CI the stack is
    always stood up, so the skip path is unreachable; a skip there is a signal."""
    import openai
    try:
        report = run()
    except openai.APIConnectionError:                       # ONLY this -> skip
        pytest.skip("gateway unreachable; eval gate not enforceable locally")
    # NOTE: no broad `except Exception`. A GoldenSchemaError or scoring error
    # propagates as a test ERROR; a gate breach fails the assert below.
    assert report["passed"], f"quality gate failed: {report['failures']}"


def _pad_to_floors(extra):
    """Helper: build a results list that passes all floors, then let the caller
    perturb it. Omitted here for brevity; the real test builds 4+ cases per
    high-value slice at scores comfortably above the configured floors."""
    raise NotImplementedError("illustrative placeholder")
