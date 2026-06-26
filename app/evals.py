"""Eval-as-a-release-gate.

Runs the agent over a golden set and blocks a merge (non-zero exit / failing
pytest) when quality regresses. Treats a model-or-prompt change like any other
production release: golden datasets as the regression suite, thresholds and
business-value weights as versioned, reviewed artifacts (evals/gate.json plus the
per-case weights in golden.jsonl), and a recorded baseline so we catch "got worse
but still above the floor", not just "fell through the floor".

Gates (all must pass):
  - overall   : unweighted mean over cases          -- broad-regression canary
  - weighted  : business-value-weighted mean        -- protects what matters
  - per-slice : high-value slices get a hard floor  -- a single weighted scalar
                hides compensating slice movements, so gate slices too
  - regression: neither metric may fall >max_drop below the recorded baseline
  - served    : only whitelisted deployments may have served the run, so an
                unexpected model swap / fallback can't quietly change the result

Determinism note: temp 0 (set in the gateway, applied to every call) reduces
variance but does NOT make the model deterministic -- floating-point
non-associativity and provider-side batching remain. So we sample each case N
times and average; the gate is statistical by design, never exact-match.

Swap the scorer for Ragas / DeepEval / Promptfoo when you want batteries-included
metrics -- the gate contract (per-case score -> aggregates -> pass/fail) is stable.
"""
import json
import sys
from pathlib import Path
from statistics import mean

from . import gateway
from .agent import ask
from .gateway import chat
from .observability import span

GOLDEN_PATH = "evals/golden.jsonl"
GATE_PATH = Path("evals/gate.json")
BASELINE_PATH = Path("evals/baseline.json")


def _load(path: Path) -> dict:
    return json.loads(path.read_text())


def keyword_score(answer: str, keywords: list[str]) -> float:
    if not keywords:
        return 1.0
    hits = sum(1 for k in keywords if k.lower() in answer.lower())
    return hits / len(keywords)


def judge_score(question: str, answer: str, reference: str, judge_model: str) -> float:
    if not reference:
        return 1.0
    # Pinned grader, decoupled from the system under test: swapping the `chat`
    # model must not also move the judge, or we can't tell a model regression from
    # a grader shift.
    verdict = chat(
        [
            {
                "role": "system",
                "content": "Score from 0.0 to 1.0 how well the answer matches the reference for the question. Reply with ONLY the number.",
            },
            {"role": "user", "content": f"Q: {question}\nReference: {reference}\nAnswer: {answer}"},
        ],
        model=judge_model,
    )
    try:
        return max(0.0, min(1.0, float(verdict.strip().split()[0])))
    except (ValueError, IndexError):
        return 0.0


def score_case(case: dict, gate: dict, served: set) -> dict:
    sc = gate["scorer"]
    samples = []
    for _ in range(gate["samples_per_case"]):
        answer = ask(case["question"])
        m = gateway.served_model()  # capture BEFORE the judge call overwrites it
        if m:
            served.add(m)
        s = sc["keyword_weight"] * keyword_score(answer, case.get("keywords", [])) + sc[
            "judge_weight"
        ] * judge_score(case["question"], answer, case.get("reference", ""), sc["judge_model"])
        samples.append(s)
    return {
        "question": case["question"],
        "slice": case.get("slice", "unsliced"),
        "weight": case.get("weight", 1),
        "score": round(mean(samples), 3),
        "samples": [round(s, 3) for s in samples],
    }


def aggregate(cases: list[dict]) -> dict:
    overall = mean(c["score"] for c in cases)
    wsum = sum(c["weight"] for c in cases)
    weighted = sum(c["score"] * c["weight"] for c in cases) / wsum if wsum else 0.0
    by_slice: dict[str, list[float]] = {}
    for c in cases:
        by_slice.setdefault(c["slice"], []).append(c["score"])
    return {
        "overall": round(overall, 3),
        "weighted": round(weighted, 3),
        "by_slice": {s: round(mean(v), 3) for s, v in by_slice.items()},
    }


def evaluate(metrics: dict, cases: list[dict], served: set, gate: dict, baseline: dict) -> dict:
    checks = []

    def check(name: str, ok: bool, detail: str) -> None:
        checks.append({"name": name, "passed": bool(ok), "detail": detail})

    # 1 + 2. Absolute floors -- two independent gates, neither masks the other.
    check(
        "overall_floor",
        metrics["overall"] >= gate["gates"]["overall"],
        f'{metrics["overall"]} >= {gate["gates"]["overall"]}',
    )
    check(
        "weighted_floor",
        metrics["weighted"] >= gate["gates"]["weighted"],
        f'{metrics["weighted"]} >= {gate["gates"]["weighted"]}',
    )

    # 3. Per-slice: high-value slices get a hard floor; the rest only alert.
    ps = gate["per_slice"]
    hv_slices = {c["slice"] for c in cases if c["weight"] >= ps["high_value_weight"]}
    alerts = []
    for s, v in metrics["by_slice"].items():
        if s in hv_slices and v < ps["block_high_value_below"]:
            check(f"slice:{s}", False, f"high-value slice {v} < {ps['block_high_value_below']}")
        elif v < ps["alert_below"]:
            alerts.append(f"{s}={v}")

    # 4. Regression vs the recorded baseline (skipped until a baseline exists).
    md = gate["regression"]["max_drop"]
    for key in ("overall", "weighted"):
        base = baseline.get(key)
        if base is None:
            continue
        check(f"regression:{key}", metrics[key] >= base - md, f"{metrics[key]} >= {base} - {md}")

    # 5. Served-model pins -- catch an unexpected model / fallback mid-run.
    expected = gate["expected_models"]
    unexpected = [m for m in served if not any(e in m for e in expected)]
    check("served_models", not unexpected, f"served={sorted(served)} unexpected={unexpected}")

    return {"passed": all(c["passed"] for c in checks), "checks": checks, "alerts": alerts}


def run(path: str = GOLDEN_PATH) -> dict:
    gate = _load(GATE_PATH)
    baseline = _load(BASELINE_PATH) if BASELINE_PATH.exists() else {}
    raw = [json.loads(line) for line in Path(path).read_text().splitlines() if line.strip()]
    served: set = set()
    with span("evals.run", **{"eval.suite": path, "eval.n": len(raw)}):
        cases = [score_case(c, gate, served) for c in raw]
    metrics = aggregate(cases)
    result = evaluate(metrics, cases, served, gate, baseline)
    return {
        **metrics,
        "passed": result["passed"],
        "served_models": sorted(served),
        "checks": result["checks"],
        "alerts": result["alerts"],
        "cases": cases,
    }


def write_baseline(report: dict) -> None:
    BASELINE_PATH.write_text(
        json.dumps(
            {
                "_doc": "Baseline scores for the current promoted (model + prompt). "
                "Regenerate with `make eval-baseline` after a vetted promotion; CI diffs "
                "candidates against this. null => regression gate skipped, floors still apply.",
                "overall": report["overall"],
                "weighted": report["weighted"],
                "by_slice": report["by_slice"],
            },
            indent=2,
        )
        + "\n"
    )


if __name__ == "__main__":
    report = run()
    for c in report["cases"]:
        print(f"  {c['score']:.2f}  [{c['slice']}] {c['question']}  samples={c['samples']}")
    print(f"\noverall={report['overall']:.2f}  weighted={report['weighted']:.2f}")
    print("by_slice: " + "  ".join(f"{s}={v:.2f}" for s, v in report["by_slice"].items()))
    if report["alerts"]:
        print("ALERTS (non-blocking): " + ", ".join(report["alerts"]))
    for c in report["checks"]:
        print(f"  [{'PASS' if c['passed'] else 'FAIL'}] {c['name']}: {c['detail']}")
    print(f"served_models={report['served_models']}")
    print(f"\ngate={'PASS' if report['passed'] else 'FAIL'}")

    if "--baseline" in sys.argv:
        if not report["passed"]:
            print("refusing to record a failing run as the baseline", file=sys.stderr)
            sys.exit(1)
        write_baseline(report)
        print(f"wrote {BASELINE_PATH}")
        sys.exit(0)

    sys.exit(0 if report["passed"] else 1)  # non-zero => CI fails
