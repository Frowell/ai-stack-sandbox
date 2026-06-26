"""Eval-as-a-gate.

Run the agent over a golden set, score each answer (deterministic keyword overlap
plus an LLM-as-judge call through the gateway), and exit non-zero on regression so
CI can block a merge. Swap this scorer for Ragas / DeepEval / Promptfoo when you
want batteries-included metrics -- the gate contract (a pass/fail + a score) stays
the same.
"""
import json
import sys
from pathlib import Path

from .agent import ask
from .gateway import chat
from .observability import span

THRESHOLD = 0.7


def keyword_score(answer: str, keywords: list[str]) -> float:
    if not keywords:
        return 1.0
    hits = sum(1 for k in keywords if k.lower() in answer.lower())
    return hits / len(keywords)


def judge_score(question: str, answer: str, reference: str) -> float:
    if not reference:
        return 1.0
    verdict = chat(
        [
            {
                "role": "system",
                "content": "Score from 0.0 to 1.0 how well the answer matches the reference for the question. Reply with ONLY the number.",
            },
            {"role": "user", "content": f"Q: {question}\nReference: {reference}\nAnswer: {answer}"},
        ]
    )
    try:
        return max(0.0, min(1.0, float(verdict.strip().split()[0])))
    except (ValueError, IndexError):
        return 0.0


def run(path: str = "evals/golden.jsonl") -> dict:
    cases = [json.loads(line) for line in Path(path).read_text().splitlines() if line.strip()]
    results = []
    with span("evals.run", **{"eval.suite": path, "eval.n": len(cases)}):
        for c in cases:
            answer = ask(c["question"])
            score = 0.5 * keyword_score(answer, c.get("keywords", [])) + 0.5 * judge_score(
                c["question"], answer, c.get("reference", "")
            )
            results.append(
                {"question": c["question"], "score": round(score, 3), "passed": score >= THRESHOLD}
            )
    mean = sum(r["score"] for r in results) / len(results) if results else 0.0
    return {"mean_score": round(mean, 3), "passed": mean >= THRESHOLD, "cases": results}


if __name__ == "__main__":
    report = run()
    for r in report["cases"]:
        print(f"[{'PASS' if r['passed'] else 'FAIL'}] {r['score']:.2f}  {r['question']}")
    print(f"\nmean={report['mean_score']:.2f}  gate={'PASS' if report['passed'] else 'FAIL'}")
    sys.exit(0 if report["passed"] else 1)  # non-zero => CI fails
