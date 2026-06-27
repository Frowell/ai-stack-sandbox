"""ILLUSTRATIVE — spec for spec 14, not wired-in code.

Shows the changes to `app/evals.py`. The new pieces vs today:
  - per-case ROOT trace (detach context per case) so each row gets a DISTINCT
    trace_id — read via opentelemetry.trace.format_trace_id
  - runtime CREATE TABLE IF NOT EXISTS eval_results (covers already-populated
    pgdata volumes, not just fresh initdb)
  - one eval_results row per case (trace_id always set; trace_url from a template
    or NULL)
  - optional score_sink push (trace -> result); non-fatal
  - flush() in run()'s finally so short-lived runs (incl. pytest) export

Unchanged: keyword_score, judge_score, THRESHOLD, the pass/fail gate contract.
Persistence and sink failures are NON-FATAL — a DB/backend hiccup must never fail
the quality gate.

Do NOT import from this file. Port the pieces into app/evals.py.
"""
import json
import logging
import os
import sys
from pathlib import Path
from string import Template

import psycopg
from opentelemetry import context as otel_context
from opentelemetry.trace import format_trace_id

from .agent import ask
from .config import settings
from .gateway import chat  # noqa: F401  (judge_score still uses it; omitted below)
from .observability import capture_content, flush, span
from .score_sink import get_score_sink

log = logging.getLogger(__name__)
THRESHOLD = 0.7

# Built once at import; ${VARS} expanded from env, {trace_id} filled per case.
# Unset => trace_url stays NULL and the row degrades to "trace_id only".
_URL_TEMPLATE = os.environ.get("OTEL_TRACE_URL_TEMPLATE")

_CREATE_EVAL_RESULTS = """
CREATE TABLE IF NOT EXISTS eval_results (
    id        BIGSERIAL PRIMARY KEY,
    run_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    suite     TEXT,
    question  TEXT,
    score     NUMERIC,
    passed    BOOLEAN,
    trace_id  TEXT,
    trace_url TEXT
)
"""


# keyword_score / judge_score are unchanged from today — omitted for brevity.
# (Keep them exactly as in app/evals.py.)


def _trace_url(trace_id: str) -> str | None:
    if not _URL_TEMPLATE:
        return None
    try:
        # ${LANGFUSE_HOST}/${PROJECT_ID} from env; {trace_id} substituted here.
        return Template(_URL_TEMPLATE).safe_substitute(os.environ).format(trace_id=trace_id)
    except Exception as e:  # noqa: BLE001 - a bad template must not break persistence
        log.warning("trace_url: bad template %r: %s", _URL_TEMPLATE, e)
        return None


def _persist(conn, suite, question, score, passed, trace_id, trace_url) -> None:
    # Non-fatal: a DB hiccup must never fail the gate.
    try:
        conn.execute(
            "INSERT INTO eval_results (suite, question, score, passed, trace_id, trace_url) "
            "VALUES (%s, %s, %s, %s, %s, %s)",
            (suite, question, score, passed, trace_id, trace_url),
        )
    except Exception as e:  # noqa: BLE001
        log.warning("eval_results: insert failed for %r: %s", question, e)


def run(path: str = "evals/golden.jsonl") -> dict:
    cases = [json.loads(line) for line in Path(path).read_text().splitlines() if line.strip()]
    results = []
    sink = get_score_sink()
    try:
        # Reuse the exact connection pattern from app/retrieval.py (autocommit=True).
        # connect() is best-effort: if the DB is down we still run the gate, just
        # without persistence.
        try:
            conn = psycopg.connect(settings.database_url, autocommit=True)
            conn.execute(_CREATE_EVAL_RESULTS)  # idempotent; covers populated volumes
        except Exception as e:  # noqa: BLE001
            log.warning("eval_results: DB unavailable, skipping persistence: %s", e)
            conn = None

        with span("evals.run", **{"eval.suite": path, "eval.n": len(cases)}):
            for c in cases:
                # Each case is its OWN ROOT TRACE: detach the current context so
                # evals.case starts with no parent => a fresh trace_id. Without
                # this every row would carry the SAME run-level trace_id.
                token = otel_context.attach(otel_context.Context())
                try:
                    with span("evals.case", **{"eval.question": c["question"]}) as case_span:
                        trace_id = format_trace_id(case_span.get_span_context().trace_id)
                        answer = ask(c["question"])  # agent.run nests under this case's trace
                        score = round(
                            0.5 * keyword_score(answer, c.get("keywords", []))  # noqa: F821
                            + 0.5 * judge_score(c["question"], answer, c.get("reference", "")),  # noqa: F821
                            3,
                        )
                        passed = score >= THRESHOLD
                finally:
                    otel_context.detach(token)

                url = _trace_url(trace_id)
                if conn is not None:
                    _persist(conn, path, c["question"], score, passed, trace_id, url)
                # trace -> result push (no-op unless EVAL_SCORE_SINK=langfuse).
                # The score VALUE is fine to push; the question is content, so gate
                # it behind OTEL_CAPTURE_CONTENT — otherwise, with the toggle off but
                # the Langfuse sink on, the question would still leave the process via
                # the score comment (a side channel the toggle must also cover).
                comment = c["question"] if capture_content() else None
                sink.record(trace_id, "eval_score", float(score), comment=comment)

                results.append(
                    {
                        "question": c["question"],
                        "score": score,
                        "passed": passed,
                        "trace_id": trace_id,
                        "trace_url": url,
                    }
                )
        if conn is not None:
            conn.close()
    finally:
        flush()  # export spans before this short-lived process exits (covers pytest too)

    mean = sum(r["score"] for r in results) / len(results) if results else 0.0
    return {"mean_score": round(mean, 3), "passed": mean >= THRESHOLD, "cases": results}


if __name__ == "__main__":
    report = run()
    for r in report["cases"]:
        print(f"[{'PASS' if r['passed'] else 'FAIL'}] {r['score']:.2f}  {r['question']}")
    print(f"\nmean={report['mean_score']:.2f}  gate={'PASS' if report['passed'] else 'FAIL'}")
    sys.exit(0 if report["passed"] else 1)  # non-zero => CI fails
