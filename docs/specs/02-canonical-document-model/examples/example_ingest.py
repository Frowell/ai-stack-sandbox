"""ILLUSTRATIVE ONLY — spec, not wired-in code. See examples/README.md.

How `app/ingest.py` would stamp canonical `meta` into `documents.meta` (jsonb).
The real `app/ingest.py` today inserts only `(source, content, embedding)` into a
table with NO `meta` column; both the `meta` column and the layout path land with
PR #2. This sketch keeps the existing structure (psycopg, gateway.embed,
TRUNCATE … RESTART IDENTITY) and adds the meta write on BOTH paths.

PRECONDITION (see README/design §6): the `documents.meta` jsonb column must
already exist. `db/init.sql` is CREATE TABLE IF NOT EXISTS and TRUNCATE does not
add columns, so on a pre-existing volume an insert that writes `meta` fails. Ship
with a fresh volume (`docker compose down -v`) or PR #2's one-time
`ALTER TABLE documents ADD COLUMN IF NOT EXISTS meta jsonb`. THIS SPEC ADDS NO
MIGRATION.
"""
import json
import sys
from pathlib import Path

import psycopg

from app.config import settings
from app.gateway import embed

# Illustrative imports from the would-be PR #2 modules:
# from app.layout import SCHEMA_VERSION, extract  # format -> canonical Document
# from app.chunking import chunk_layout
#
# BOTH ingest paths MUST stamp the SAME single SCHEMA_VERSION constant (AC-4a) —
# never a hardcoded "1". Once app.layout exists, import it and drop this fallback.
SCHEMA_VERSION = "1"  # illustrative stand-in for app.layout.SCHEMA_VERSION


def ingest(path: str = "data/corpus.jsonl") -> int:
    rows = [json.loads(line) for line in Path(path).read_text().splitlines() if line.strip()]

    chunks: list[dict] = []  # {source, content, meta}
    for r in rows:
        if "blocks" in r or r.get("format"):
            # LAYOUT PATH (illustrative): a canonical Document / manifest row.
            # doc = Document(**r); assert_supported_version(doc.schema_version)
            # for c in chunk_layout(doc):
            #     chunks.append({"source": doc.source, "content": c.text, "meta": c.meta})
            ...
        else:
            # BACK-COMPAT RAW ROW: flat {source, content}. Stamp the SAME meta
            # ENVELOPE (schema_version) but with EMPTY blocks so a consumer can
            # tell "no canonical structure" from "a valid v1 doc with 0 blocks".
            chunks.append(
                {
                    "source": r.get("source", "corpus"),
                    "content": r["content"],
                    "meta": {"schema_version": SCHEMA_VERSION, "blocks": []},  # no `relations`; SAME constant as the layout path
                }
            )

    vectors = embed([c["content"] for c in chunks])
    with psycopg.connect(settings.database_url, autocommit=True) as conn:
        conn.execute("TRUNCATE documents RESTART IDENTITY")  # full re-ingest IS the rollout
        for c, v in zip(chunks, vectors):
            lit = "[" + ",".join(f"{x:.8f}" for x in v) + "]"
            conn.execute(
                "INSERT INTO documents (source, content, embedding, meta) "
                "VALUES (%s, %s, %s::vector, %s)",
                (c["source"], c["content"], lit, json.dumps(c["meta"])),
            )
    return len(chunks)


if __name__ == "__main__":
    n = ingest(sys.argv[1] if len(sys.argv) > 1 else "data/corpus.jsonl")
    print(f"ingested {n} chunks")
