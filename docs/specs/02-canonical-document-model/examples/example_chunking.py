"""ILLUSTRATIVE ONLY — spec, not wired-in code. See examples/README.md.

Would become the v1 `chunk_layout` in `app/chunking.py` (PR #2 introduces that
module; it does NOT exist in the tree yet). The single behavioral change v1 makes:
footnotes are attached by WALKING `footnote_ref` relations, not by scanning chunk
text for the `[^id]` token. Everything else (definition duplicated into each
citing chunk, footnote ids in chunk meta, uncited definitions in a trailing
chunk) is PR #2 behavior preserved.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from example_layout import SCHEMA_VERSION, BlockType, Document  # illustrative import path


@dataclass
class Chunk:
    text: str
    meta: dict = field(default_factory=dict)


def _block_text_chunks(doc: Document) -> list[list[str]]:
    """STUB for PR #2's existing text-chunking (group blocks into chunks by size,
    keep `table` blocks whole, etc.). Returns groups of block ids in reading
    order. The point of this example is the footnote walk, not this grouping."""
    return [[b.id for b in doc.blocks if b.type != BlockType.FOOTNOTE]]


def chunk_layout(doc: Document) -> list[Chunk]:
    by_id = {b.id: b for b in doc.blocks}
    # footnote_ref edges only; reserved relation types are ignored in v1.
    fn_edges = [r for r in doc.relations if r.type.value == "footnote_ref"]
    cited: set[str] = set()
    chunks: list[Chunk] = []

    for ids in _block_text_chunks(doc):
        text = "\n\n".join(by_id[i].text for i in ids)

        # THE WALK: which footnote blocks do the blocks in this chunk cite?
        # NOTE: we never look at the `[^id]` token in `text`. Linkage works even
        # when the marker is absent (the PDF-by-geometry case).
        fn_ids = [e.to_id for e in fn_edges if e.from_id in ids]
        for fid in fn_ids:
            text += "\n\n" + by_id[fid].text  # duplicate the definition (self-contained chunk)
            cited.add(fid)

        leading = by_id[ids[0]] if ids else None
        meta = {
            "schema_version": SCHEMA_VERSION,
            "doc": doc.source,
            "format": doc.format,
            "section": (leading.locator.path if leading and leading.locator else None),
            # ONE id-space: footnotes carries BLOCK IDS (not marker keys like "otel").
            "footnotes": fn_ids,
            "blocks": ids,
        }
        if leading and leading.locator and leading.locator.page is not None:
            meta["page"] = leading.locator.page  # only when a real locator exists
        chunks.append(Chunk(text=text, meta=meta))

    # Uncited footnote definitions (the v0 orphan-`notes` case) -> trailing chunk,
    # not dropped.
    uncited = [b for b in doc.blocks if b.type == BlockType.FOOTNOTE and b.id not in cited]
    if uncited:
        chunks.append(
            Chunk(
                text="\n\n".join(b.text for b in uncited),
                meta={
                    "schema_version": SCHEMA_VERSION,
                    "doc": doc.source,
                    "format": doc.format,
                    "section": None,
                    "footnotes": [b.id for b in uncited],
                    "blocks": [b.id for b in uncited],
                },
            )
        )
    return chunks
