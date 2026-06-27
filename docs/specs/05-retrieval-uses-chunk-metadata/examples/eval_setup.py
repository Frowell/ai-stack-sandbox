"""ILLUSTRATIVE — fallback eval-setup, ONLY if PR #2 does not ingest sample.md.

design.md §6 / README open question: the new golden case needs sample.md's
section chunks present when `python -m app.evals` runs. The CLEAN path is for
PR #2 to add sample.md to the default ingest set so `make ingest` (and the CI
eval-gate's setup) loads it alongside corpus.jsonl.

If #2 declines, this spec owns a small setup step. The hazard: today's
`app.ingest.ingest()` starts with `TRUNCATE documents RESTART IDENTITY`, so
calling it twice would wipe the corpus. The two safe options below avoid that.
NOT wired in — this is the shape of the setup the CI gate would call.
"""
import json
from pathlib import Path

import psycopg

from app.config import settings
from app.gateway import embed


# IMPORTANT: `corpus.jsonl` is JSONL (one chunk-dict per line) but `sample.md` is
# raw markdown — `json.loads(line)` would CRASH on it. Turning markdown into
# chunk-dicts WITH `meta` is PR #2's canonical ingester, NOT this loader. So the
# fallbacks below delegate `.md` to #2's chunker and only json-parse `.jsonl`
# fixtures themselves. `chunk_markdown` is a stand-in for whatever #2 exports
# (e.g. `app.ingest.chunk_markdown` / the canonical pipeline) — wire to the real
# name once #2 lands.
def _load_chunks(path: str) -> list[dict]:                      # DEPENDS-ON-#2
    p = Path(path)
    if p.suffix == ".md":
        # from app.ingest import chunk_markdown            # PR #2's canonical chunker
        # return chunk_markdown(p.read_text())             # -> [{content, meta, source}, ...]
        raise NotImplementedError("md->chunks is PR #2's canonical ingester")
    return [json.loads(line) for line in p.read_text().splitlines() if line.strip()]


# OPTION A (preferred fallback): one truncating pass over BOTH fixtures, so the
# eval store contains the corpus AND sample.md. Mirrors app.ingest.ingest() but
# reads a list of paths (jsonl OR md) and truncates exactly once.
def ingest_for_evals(paths=("data/corpus.jsonl", "data/sample.md")) -> int:  # DEPENDS-ON-#2
    docs: list[dict] = []
    for p in paths:
        docs += _load_chunks(p)            # md goes through #2's chunker, not json.loads
    vectors = embed([d["content"] for d in docs])
    with psycopg.connect(settings.database_url, autocommit=True) as conn:
        conn.execute("TRUNCATE documents RESTART IDENTITY")     # exactly once
        for d, v in zip(docs, vectors):
            lit = "[" + ",".join(f"{x:.8f}" for x in v) + "]"
            conn.execute(
                "INSERT INTO documents (source, content, embedding, meta) "  # DEPENDS-ON-#2
                "VALUES (%s, %s, %s::vector, %s)",
                (d.get("source", "corpus"), d["content"], lit, json.dumps(d.get("meta"))),
            )
    return len(docs)


# OPTION B: append-only ingest of sample.md AFTER the normal `make ingest`, with
# NO truncate, so the corpus stays. Use when the gate already ran `make ingest`.
def append_sample(path="data/sample.md") -> int:                # DEPENDS-ON-#2
    docs = _load_chunks(path)              # md -> chunks via #2's ingester (see _load_chunks)
    vectors = embed([d["content"] for d in docs])
    with psycopg.connect(settings.database_url, autocommit=True) as conn:
        # no TRUNCATE here — that is the whole point
        for d, v in zip(docs, vectors):
            lit = "[" + ",".join(f"{x:.8f}" for x in v) + "]"
            conn.execute(
                "INSERT INTO documents (source, content, embedding, meta) "
                "VALUES (%s, %s, %s::vector, %s)",
                (d.get("source", "sample"), d["content"], lit, json.dumps(d.get("meta"))),
            )
    return len(docs)
