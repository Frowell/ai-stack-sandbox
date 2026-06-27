"""ILLUSTRATIVE — spec for spec 13, not wired-in code.

Shows the refactor of `app/evals.py`:
  - JudgeVerdict (pydantic schema; extra="forbid"; score NOT range-bound)
  - judge_score -> chat_structured, clamp, let StructuredOutputError propagate
  - run() records a per-case error channel; mean over evaluated cases only;
    mean_score is None if ALL cases error; gate_status distinguishes
    eval_error vs quality_fail
  - __main__ / format path is None-safe (no :.2f or `None >= float` crash)

Unchanged from today: keyword_score, THRESHOLD, span(), ask().
Do NOT import from this file. Port the pieces into app/evals.py.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

from pydantic import BaseModel, ConfigDict

from .agent import ask
from .gateway import StructuredOutputError, chat, chat_structured
from .observability import span

THRESHOLD = 0.7


class JudgeVerdict(BaseModel):
    # extra="forbid" -> the strict-mode schema gets additionalProperties:false.
    # NOTE: score is deliberately NOT Field(ge=0, le=1). A judge saying 5 or
    # 1.0001 is a scale slip, not a structural failure — we CLAMP below instead
    # of turning a benign overshoot into an eval_error (design.md §7).
    model_config = ConfigDict(extra="forbid")
    score: float


def keyword_score(answer: str, keywords: list[str]) -> float:
    if not keywords:
        return 1.0
    hits = sum(1 for k in keywords if k.lower() in answer.lower())
    return hits / len(keywords)


def judge_score(question: str, answer: str, reference: str) -> float:
    """Returns a clamped float, or raises StructuredOutputError (no longer
    swallows failures into 0.0 — that was the bug this whole spec exists to fix).
    """
    if not reference:
        return 1.0
    verdict = chat_structured(
        [
            {
                "role": "system",
                "content": (
                    "Score from 0.0 to 1.0 how well the answer matches the "
                    "reference for the question. Return JSON: {\"score\": <number>}."
                ),
            },
            {"role": "user", "content": f"Q: {question}\nReference: {reference}\nAnswer: {answer}"},
        ],
        JudgeVerdict,
    )
    return max(0.0, min(1.0, verdict.score))  # clamp scale slips; structural fails already raised


def run(path: str = "evals/golden.jsonl") -> dict:
    cases = [json.loads(line) for line in Path(path).read_text().splitlines() if line.strip()]
    results: list[dict] = []
    with span("evals.run", **{"eval.suite": path, "eval.n": len(cases)}):
        for c in cases:
            answer = ask(c["question"])
            try:
                score = 0.5 * keyword_score(answer, c.get("keywords", [])) + 0.5 * judge_score(
                    c["question"], answer, c.get("reference", "")
                )
                results.append(
                    {
                        "question": c["question"],
                        "score": round(score, 3),
                        "error": None,
                        "passed": score >= THRESHOLD,
                    }
                )
            except StructuredOutputError as e:
                # Infra/parse failure — recorded, NOT scored 0.0. Excluded from the mean.
                results.append(
                    {
                        "question": c["question"],
                        "score": None,
                        "error": f"{type(e).__name__}: {e}",
                        "passed": False,
                    }
                )

    evaluated = [r for r in results if r["error"] is None]
    any_error = any(r["error"] is not None for r in results)
    # Mean over EVALUATED cases only; None (not 0.0) if every case errored — no div-by-zero.
    mean = round(sum(r["score"] for r in evaluated) / len(evaluated), 3) if evaluated else None

    quality_ok = mean is not None and mean >= THRESHOLD
    passed = quality_ok and not any_error  # fail loud on infra error...
    if any_error:
        gate_status = "eval_error"          # ...but say WHY (the whole point of the spec)
    elif not quality_ok:
        gate_status = "quality_fail"
    else:
        gate_status = "pass"

    return {"mean_score": mean, "passed": passed, "gate_status": gate_status, "cases": results}


def format_report(report: dict) -> list[str]:
    """Render a run() report into printable lines. PURE (no I/O) and None-safe, so
    AC #7 can be proven by a unit test that calls this directly — the formatting
    must NOT live only inside `__main__`, which pytest cannot import/call. An
    errored case prints ERR + its error (never crashes on `:.2f`), and a None mean
    renders `n/a` (never `None >= THRESHOLD` / `:.2f` on None).
    """
    lines: list[str] = []
    for r in report["cases"]:
        if r["error"] is not None:
            lines.append(f"[ERR ] ----  {r['question']}  ({r['error']})")
        else:
            tag = "PASS" if r["passed"] else "FAIL"
            lines.append(f"[{tag}] {r['score']:.2f}  {r['question']}")
    mean = report["mean_score"]
    mean_str = "n/a" if mean is None else f"{mean:.2f}"   # None-safe summary line
    lines.append(
        f"\nmean={mean_str}  status={report['gate_status']}  "
        f"gate={'PASS' if report['passed'] else 'FAIL'}"
    )
    return lines


if __name__ == "__main__":
    report = run()
    for line in format_report(report):
        print(line)
    sys.exit(0 if report["passed"] else 1)  # non-zero => CI fails (eval_error OR quality_fail)


# Suppress unused-import warning for `chat` (kept to show the documented revert
# path: point judge_score back at chat() to disable the structured path).
_ = chat
