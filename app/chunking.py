"""Chunking + footnote attachment.

Three entry points:
- `chunk_layout(doc)`  -- structure-aware (the primary path for layout-rich docs).
  Starts a chunk at each heading, keeps a section together up to a size budget,
  never splits a table, and falls back to `semantic_chunk()` for any single block
  bigger than the budget. Every footnote cited inside a chunk is appended to that
  chunk and recorded in `meta["footnotes"]`, so a retrieved chunk is
  self-contained -- the footnote travels with the text that cites it.
- `chunk_records(doc)` -- one chunk per row for tabular sources (no footnotes).
- `semantic_chunk(text)` -- the secondary tool for unstructured text: sentence-
  aware greedy packing to a target size with overlap. Swap in embedding-similarity
  splitting where marked for true semantic boundaries.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from .layout import Element, LayoutDoc, _dedupe

TARGET_CHARS = 1200
OVERLAP_CHARS = 150

_SENT = re.compile(r"(?<=[.!?])\s+")


@dataclass
class Chunk:
    content: str
    kind: str
    index: int = 0
    meta: dict = field(default_factory=dict)


def semantic_chunk(text: str, target: int = TARGET_CHARS, overlap: int = OVERLAP_CHARS) -> list[str]:
    text = (text or "").strip()
    if not text:
        return []
    if len(text) <= target:
        return [text]
    # Sentence-aware greedy packing. For true semantic boundaries, embed each
    # sentence and start a new chunk where adjacent cosine similarity drops below
    # a threshold -- drop that in here, the rest of the pipeline is unchanged.
    chunks: list[str] = []
    cur = ""
    for sent in _SENT.split(text):
        if cur and len(cur) + len(sent) + 1 > target:
            chunks.append(cur.strip())
            tail = cur[-overlap:] if overlap else ""
            cur = (tail + " " + sent).strip()
        else:
            cur = (cur + " " + sent).strip()
    if cur.strip():
        chunks.append(cur.strip())
    return chunks


def _render(el: Element) -> str:
    if el.kind == "heading":
        return "#" * max(1, el.level) + " " + el.text
    return el.text


def _attach_footnotes(body: str, ids: list[str], footnotes: dict[str, str]) -> str:
    notes = [(i, footnotes[i]) for i in ids if i in footnotes]
    if not notes:
        return body
    return body + "\n\n" + "\n".join(f"[^{i}]: {t}" for i, t in notes)


def _make_chunk(doc: LayoutDoc, body: str, kind: str, ids: list[str], section: str,
                index: int) -> Chunk:
    ids = [i for i in _dedupe(ids) if i in doc.footnotes]
    meta: dict = {"doc": doc.source}
    if doc.meta.get("format"):
        meta["format"] = doc.meta["format"]
    if section:
        meta["section"] = section
    if ids:
        meta["footnotes"] = ids
    return Chunk(_attach_footnotes(body, ids, doc.footnotes), kind, index, meta)


def chunk_layout(doc: LayoutDoc, target: int = TARGET_CHARS) -> list[Chunk]:
    chunks: list[Chunk] = []
    section: list[str] = []  # heading breadcrumb
    buf: list[Element] = []

    def sec() -> str:
        return " > ".join(section)

    def size(elems) -> int:
        return sum(len(e.text) for e in elems)

    def flush():
        nonlocal buf
        if not buf:
            return
        body = "\n\n".join(_render(e) for e in buf).strip()
        ids = [f for e in buf for f in e.footnote_ids]
        kind = "table" if all(e.kind == "table" for e in buf) else "body"
        chunks.append(_make_chunk(doc, body, kind, ids, sec(), len(chunks)))
        buf = []

    for el in doc.elements:
        if el.kind == "heading":
            flush()
            section[:] = section[: max(0, el.level - 1)]
            section.append(el.text)
            buf = [el]
        elif el.kind == "table":
            flush()
            chunks.append(_make_chunk(doc, el.text, "table", el.footnote_ids, sec(), len(chunks)))
        elif len(el.text) > target:
            # Oversized single block: split semantically; a footnote attaches to the
            # piece that actually carries its [^id] marker.
            flush()
            for piece in semantic_chunk(el.text, target):
                pids = [f for f in el.footnote_ids if f"[^{f}]" in piece]
                chunks.append(_make_chunk(doc, piece, "body", pids, sec(), len(chunks)))
        else:
            if buf and size(buf) + len(el.text) > target:
                flush()
            buf.append(el)
    flush()

    # Uncited footnotes: keep them rather than silently drop -- one trailing chunk.
    cited = {f for c in chunks for f in c.meta.get("footnotes", [])}
    orphans = {k: v for k, v in doc.footnotes.items() if k not in cited}
    if orphans:
        body = "\n".join(f"[^{k}]: {v}" for k, v in orphans.items())
        chunks.append(Chunk(body, "notes", len(chunks),
                            {"doc": doc.source, "footnotes": list(orphans),
                             "section": "uncited footnotes"}))
    return chunks


def chunk_records(doc: LayoutDoc, batch: int = 1) -> list[Chunk]:
    cols = doc.meta.get("columns")
    chunks = []
    for n in range(0, len(doc.elements), batch):
        group = doc.elements[n : n + batch]
        body = "\n".join(e.text for e in group)
        meta = {"doc": doc.source, "format": doc.meta.get("format", "csv")}
        if cols:
            meta["columns"] = cols
        chunks.append(Chunk(body, "record", len(chunks), meta))
    return chunks
