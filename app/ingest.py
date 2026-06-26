"""Embed a small corpus through the gateway and store it in pgvector.
Even embeddings go through the gateway -- the app never calls a provider directly.
"""
import json
import sys
from pathlib import Path

import psycopg

from .config import settings
from .gateway import embed


def ingest(path: str = "data/corpus.jsonl") -> int:
    docs = [json.loads(line) for line in Path(path).read_text().splitlines() if line.strip()]
    vectors = embed([d["content"] for d in docs])
    with psycopg.connect(settings.database_url, autocommit=True) as conn:
        conn.execute("TRUNCATE documents RESTART IDENTITY")
        for d, v in zip(docs, vectors):
            lit = "[" + ",".join(f"{x:.8f}" for x in v) + "]"
            conn.execute(
                "INSERT INTO documents (source, content, embedding) VALUES (%s, %s, %s::vector)",
                (d.get("source", "corpus"), d["content"], lit),
            )
    return len(docs)


if __name__ == "__main__":
    n = ingest(sys.argv[1] if len(sys.argv) > 1 else "data/corpus.jsonl")
    print(f"ingested {n} documents")
