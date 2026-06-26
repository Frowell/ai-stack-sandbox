"""Hybrid ingestion: route each source by whether it has exploitable layout.

- Layout-rich (md / html / pdf / docx)  -> layout extraction, then structure-aware
  chunking (`chunk_layout`), with footnotes merged into the chunk that cites them.
- Tabular (csv / xlsx)                   -> one chunk per row (`chunk_records`).
- Unstructured (plain text / .jsonl content) -> semantic chunking (`semantic_chunk`).

Every chunk is embedded through the gateway and stored in pgvector. The app never
calls a provider directly.

Inputs:
- a `.jsonl` manifest: each line is either {"source","content"} (unstructured text,
  back-compatible with data/corpus.jsonl) or {"source"?, "path", "format"?}
  (pointer to a layout file).
- or a single file path (.md/.html/.csv/.pdf/.docx/.xlsx/.txt), routed by extension.
"""
import json
import sys
from pathlib import Path

import psycopg

from . import layout
from .chunking import Chunk, chunk_layout, chunk_records, semantic_chunk
from .config import settings
from .gateway import embed


def _unstructured(source: str, text: str) -> list[Chunk]:
    return [Chunk(c, "text", i, {"doc": source})
            for i, c in enumerate(semantic_chunk(text))]


def chunks_for_file(source: str, fmt: str, raw: bytes) -> list[Chunk]:
    fmt = layout.canonical(fmt)
    if fmt in layout.TABULAR:
        return chunk_records(layout.get_extractor(fmt).extract(raw, source))
    if fmt in ("markdown", "html", "pdf", "docx"):
        return chunk_layout(layout.get_extractor(fmt).extract(raw, source))
    return _unstructured(source, raw.decode("utf-8", "replace"))  # .txt and friends


def chunks_for_item(item: dict) -> list[Chunk]:
    """Route one manifest entry (or synthesized file entry) to its chunker."""
    if "content" in item and "path" not in item:
        return _unstructured(item.get("source", "corpus"), item["content"])
    path = Path(item["path"])
    source = item.get("source", path.stem)
    fmt = item.get("format", path.suffix.lstrip("."))
    return chunks_for_file(source, fmt, path.read_bytes())


def collect_chunks(path: str) -> list[Chunk]:
    p = Path(path)
    if p.suffix == ".jsonl":
        items = [json.loads(line) for line in p.read_text().splitlines() if line.strip()]
    else:
        items = [{"path": str(p)}]
    chunks: list[Chunk] = []
    for item in items:
        chunks.extend(chunks_for_item(item))
    return chunks


def ingest(path: str = "data/corpus.jsonl") -> int:
    chunks = collect_chunks(path)
    if not chunks:
        return 0
    vectors = embed([c.content for c in chunks])
    with psycopg.connect(settings.database_url, autocommit=True) as conn:
        conn.execute("TRUNCATE documents RESTART IDENTITY")
        for c, v in zip(chunks, vectors):
            lit = "[" + ",".join(f"{x:.8f}" for x in v) + "]"
            conn.execute(
                "INSERT INTO documents (source, doc_id, chunk_index, kind, content, meta, embedding) "
                "VALUES (%s, %s, %s, %s, %s, %s::jsonb, %s::vector)",
                (c.meta.get("doc", "corpus"), c.meta.get("doc"), c.index, c.kind,
                 c.content, json.dumps(c.meta), lit),
            )
    return len(chunks)


if __name__ == "__main__":
    n = ingest(sys.argv[1] if len(sys.argv) > 1 else "data/corpus.jsonl")
    print(f"ingested {n} chunks")
