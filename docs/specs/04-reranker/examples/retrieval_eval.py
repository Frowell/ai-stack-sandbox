"""ILLUSTRATIVE — spec for a retrieval-quality harness, not wired in.

Reports hit@k / MRR over labelled gold doc ids for RERANK_BACKEND=none vs the
configured backend. This is REPORTED (printed in the PR), not asserted as a hard
threshold — a 7-doc / 4-question corpus is too small for a stable gate (see the
README risk note). The end-to-end answer-score gate in app/evals.py is unchanged.

Why label by `source`, not integer id: id is BIGSERIAL and ingest() does
`TRUNCATE documents RESTART IDENTITY`, so ids track corpus *line order* and would
silently rot if the corpus is reordered/extended. `source` is the stable semantic
key; we resolve source -> id at eval time.

Run (mirrors `make eval`'s idiom):
    uv run python -m evals.retrieval_eval                 # backend from env
    RERANK_BACKEND=cohere uv run python -m evals.retrieval_eval
"""
import dataclasses
import json
from pathlib import Path

import psycopg

from app import retrieval
from app.config import settings


def _resolve_sources(conn) -> dict[str, int]:
    """source -> id. (Corpus sources are unique today; if that changes, map to a
    set of ids and count a hit if ANY resolved id lands in top-N.)"""
    rows = conn.execute("SELECT id, source FROM documents").fetchall()
    return {source: doc_id for doc_id, source in rows}


def _hit_and_rr(ranked_ids: list[int], gold_ids: set[int]) -> tuple[int, float]:
    hit = int(any(i in gold_ids for i in ranked_ids))
    rr = 0.0
    for rank, doc_id in enumerate(ranked_ids, start=1):
        if doc_id in gold_ids:
            rr = 1.0 / rank
            break
    return hit, rr


def evaluate(backend: str, gold_path: str, top_n: int = 4) -> dict:
    cases = [
        json.loads(line)
        for line in Path(gold_path).read_text().splitlines()
        if line.strip()
    ]
    # Per-call backend select means we can swap by patching the module settings,
    # exactly as the tests do — no reimport needed.
    retrieval.settings = dataclasses.replace(settings, rerank_backend=backend)
    with psycopg.connect(settings.database_url, autocommit=True) as conn:
        src_to_id = _resolve_sources(conn)
        hits, rrs = [], []
        for c in cases:
            gold_ids = {src_to_id[s] for s in c["relevant_sources"] if s in src_to_id}
            ranked = retrieval.retrieve(c["question"], top_n=top_n)
            ranked_ids = [doc_id for doc_id, _ in ranked]
            hit, rr = _hit_and_rr(ranked_ids, gold_ids)
            hits.append(hit)
            rrs.append(rr)
    n = len(cases)
    return {
        "backend": backend,
        "n": n,
        f"hit@{top_n}": round(sum(hits) / n, 3) if n else 0.0,
        "mrr": round(sum(rrs) / n, 3) if n else 0.0,
    }


if __name__ == "__main__":
    gold = "evals/retrieval_gold.jsonl"
    baseline = evaluate("none", gold)
    treatment = evaluate(settings.rerank_backend, gold)
    for r in (baseline, treatment):
        print(f"backend={r['backend']:<8} hit@4={r['hit@4']:.3f}  mrr={r['mrr']:.3f}  (n={r['n']})")
    print(
        f"\ndelta  hit@4={treatment['hit@4'] - baseline['hit@4']:+.3f}  "
        f"mrr={treatment['mrr'] - baseline['mrr']:+.3f}"
    )
