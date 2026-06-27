"""ILLUSTRATIVE ONLY — spec, not collected by pytest. See examples/README.md.

Named `example_tests.py` (not `test_*.py`) so pytest's default discovery skips it
while it lives under docs/specs/. When implementing, port these into
`tests/test_canonical_model.py`. Every test maps to an acceptance criterion in
../README.md (see ../testing.md for the full traceability matrix).

These are OFFLINE and DETERMINISTIC: the canonical types, the v0->v1 mapping, the
chunker's relation-walk, and the version guard touch no network. Only the eval
before/after-delta check (AC-5) needs the live stack; it lives in the gated job,
not here.
"""
import json

import pytest

# Illustrative import paths (would be `app.layout` / `app.chunking` after PR #2):
from example_chunking import chunk_layout
from example_layout import (
    SCHEMA_VERSION,
    Block,
    BlockType,
    Document,
    Relation,
    RelationType,
    assert_supported_version,
    extract_markdown,
    map_v0_kind,
)


# AC-1: closed taxonomy is ENFORCED at construction, not advisory.
def test_block_rejects_type_outside_enum():
    Block(id="b0", type="paragraph", text="ok")  # string coerces to the enum
    with pytest.raises(ValueError):
        Block(id="b1", type="sidebar", text="not in the closed set")


# AC-2: v0->v1 mapping for EVERY v0 kind; an unmapped kind raises (fail loud).
@pytest.mark.parametrize(
    "v0_kind,expected",
    [
        ("heading", BlockType.HEADING),
        ("body", BlockType.PARAGRAPH),
        ("table", BlockType.TABLE),
        ("record", BlockType.RECORD),
    ],
)
def test_v0_kind_mapping(v0_kind, expected):
    assert map_v0_kind(v0_kind) is expected


def test_v0_unmapped_kind_raises():
    with pytest.raises(ValueError):
        map_v0_kind("callout")  # not a known v0 kind


# AC-2: the orphan v0 `notes` chunk maps to a FOOTNOTE block in the EXTRACTOR
# (special-cased, not via the mapping table) with no inbound footnote_ref.
def test_orphan_notes_maps_to_footnote_block():
    doc = extract_markdown(
        "d",
        [{"kind": "body", "text": "prose"}, {"kind": "notes", "text": "an endnote"}],
    )
    notes_block = doc.blocks[1]
    assert notes_block.type is BlockType.FOOTNOTE
    assert not any(r.to_id == notes_block.id for r in doc.relations)  # no inbound ref


# AC-2: optional fields are omitted, not faked, for token-only formats.
def test_markdown_block_omits_locator_and_order():
    b = Block(id="b0", type=BlockType.PARAGRAPH, text="prose")
    assert b.locator is None and b.order is None


# AC-3: footnote linkage works via the relation EVEN WHEN the [^id] token is
# absent from text (the PDF-by-geometry case). This is the key new test.
def test_footnote_linked_by_relation_without_token():
    doc = Document(
        source="d",
        format="markdown",
        blocks=[
            Block(id="b1", type=BlockType.PARAGRAPH, text="body with no inline marker"),
            Block(id="b2", type=BlockType.FOOTNOTE, text="the definition"),
        ],
        relations=[Relation(type=RelationType.FOOTNOTE_REF, from_id="b1", to_id="b2")],
    )
    chunks = chunk_layout(doc)
    cite = chunks[0]
    assert "the definition" in cite.text          # duplicated into the citing chunk
    assert cite.meta["footnotes"] == ["b2"]       # by BLOCK ID
    assert "[^" not in "body with no inline marker"  # there was never a token


# AC-3 / id-space: meta.footnotes and relation.to_id share ONE namespace (block
# ids). No marker-key ("otel") leakage into ids or meta.
def test_single_footnote_id_space():
    doc = Document(
        source="d",
        format="markdown",
        blocks=[
            Block(id="b1", type=BlockType.PARAGRAPH, text="cite[^otel]"),
            Block(id="b2", type=BlockType.FOOTNOTE, text="def", attrs={"marker": "otel"}),
        ],
        relations=[Relation(type=RelationType.FOOTNOTE_REF, from_id="b1", to_id="b2")],
    )
    meta = chunk_layout(doc)[0].meta
    assert meta["footnotes"] == ["b2"]            # block id, NOT "otel"
    assert "otel" not in meta["footnotes"]


# AC-3: uncited footnote (the orphan v0 `notes` case) -> trailing chunk, not dropped.
def test_uncited_footnote_goes_to_trailing_chunk():
    doc = Document(
        source="d",
        format="markdown",
        blocks=[
            Block(id="b1", type=BlockType.PARAGRAPH, text="body"),
            Block(id="b2", type=BlockType.FOOTNOTE, text="orphan note"),
        ],
        relations=[],  # nothing cites b2
    )
    chunks = chunk_layout(doc)
    assert any("orphan note" in c.text for c in chunks)
    assert chunks[-1].meta["footnotes"] == ["b2"]


# AC-4: reader rejects an unknown MAJOR; same-major is accepted.
def test_version_guard():
    assert_supported_version("1")        # ok
    assert_supported_version("1.4")      # same major, ok
    with pytest.raises(ValueError):
        assert_supported_version("2")    # unknown major -> raise, not mis-parse


# AC-4 (stamping): extractors stamp from the single SCHEMA_VERSION constant.
def test_document_stamps_schema_version():
    assert Document(source="d", format="markdown", blocks=[]).schema_version == SCHEMA_VERSION


# AC-3 (cross-path): a raw {source,content} row's meta is DISTINGUISHABLE from a
# layout chunk's. Both carry schema_version; only the layout chunk has blocks.
def test_raw_row_meta_distinguishable_from_layout_chunk(monkeypatch):
    """Sketch: drive example_ingest's chunk-building (not the DB) and compare meta.
    Layout chunk: non-empty blocks. Raw row: schema_version present, blocks empty.
    Consumers MUST read empty blocks as 'no canonical structure', not 'valid v1
    doc with zero blocks'."""
    raw_meta = {"schema_version": "1", "blocks": []}
    layout_meta = chunk_layout(
        Document(
            source="d",
            format="markdown",
            blocks=[Block(id="b1", type=BlockType.PARAGRAPH, text="x")],
        )
    )[0].meta
    assert raw_meta["schema_version"] == layout_meta["schema_version"]  # same envelope
    assert raw_meta["blocks"] == [] and layout_meta["blocks"] != []      # distinguishable


# Regression (AC-3): the corrected sample serializes/round-trips and keeps one
# id-space (this guards the CANONICAL_MODEL.md fix).
def test_sample_json_uses_block_id_space():
    import pathlib

    sample = json.loads((pathlib.Path(__file__).parent / "canonical_document.sample.json").read_text())
    rel = sample["relations"][0]
    assert rel["to_id"] == "b3"
    assert sample["_chunk_meta_would_be"]["footnotes"] == ["b3"]  # block id, not "otel"
