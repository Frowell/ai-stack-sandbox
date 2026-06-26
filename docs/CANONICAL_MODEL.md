# Canonical Document Model

The single, format-agnostic schema that **every** layout extractor normalizes
into. It is the extraction-layer seam: just as the gateway makes the model
*provider* a config decision, the canonical model makes the document *format* an
extraction-time decision — chunking, footnote linkage, retrieval, and storage
speak only this schema and never branch on the source format.

A new backend (docling, pymupdf, python-docx, …) is "done" when it emits a valid
canonical `Document`. Nothing downstream changes.

---

## Status: v0 shipped, v1 is the target

| | v0 — shipped (PR #2) | v1 — this spec (target) |
| --- | --- | --- |
| Container | `LayoutDoc(source, elements, footnotes, meta)` | `Document(schema_version, source, format, meta, blocks, relations)` |
| Unit | `Element(kind, text, level, footnote_ids, meta)` | `Block(id, type, text, level, order, locator?, attrs)` |
| Block types | free string (`heading`/`body`/`table`/`record`) | **closed** `BlockType` enum |
| Footnotes | `footnotes: dict[id,str]` + inline `[^id]` token + `footnote_ids` | `footnote` **block** + `footnote_ref` **relation** (token is rendering-only) |
| Provenance | none | optional `Locator` (page / bbox / char offsets / path) |
| Versioning | none | `schema_version` |

**v0 → v1 is additive and adapter-friendly:** today's extractors already produce
the v1 *shape* minus IDs, `order`, `locator`, and relations. The refactor lands
with the **PDF backend** — the first producer of real locators — so the optional
fields have an actual source instead of being speculative.

---

## Principles

1. **Minimal core, optional richness.** Token-only formats (Markdown) and
   geometry-bearing formats (PDF) both fit: everything beyond `type`/`text` is
   optional, so a Markdown block simply omits `locator`.
2. **Closed taxonomy.** Backends map *into* a fixed `BlockType` set; they don't
   invent kinds. This is what keeps downstream branching from rotting.
3. **Relations over inline tokens.** Cross-element links (footnotes today) are
   typed edges with stable IDs, not text conventions — so linkage survives
   backends that recover footnotes by geometry, not by an inline marker.
4. **Version everything.** `schema_version` lets extraction and consumers evolve
   independently.
5. **Third-party IRs are adapters, not the model.** docling's `DoclingDocument`
   and unstructured's elements are mapped *to* this schema by a thin adapter;
   we don't adopt a vendor IR as canonical.

---

## Schema

### `Document`

| Field | Type | Req | Notes |
| --- | --- | --- | --- |
| `schema_version` | string | ✅ | e.g. `"1"`. See [Versioning](#versioning). |
| `source` | string | ✅ | Logical document id (stable across re-ingest). |
| `format` | string | ✅ | Origin format: `markdown`/`html`/`pdf`/`docx`/`csv`/`xlsx`/… |
| `meta` | object | — | Doc-level: `title`, `author`, `created`, page count, … |
| `blocks` | `Block[]` | ✅ | In reading order. |
| `relations` | `Relation[]` | — | Typed edges between blocks. |

### `Block`

| Field | Type | Req | Notes |
| --- | --- | --- | --- |
| `id` | string | ✅ | Stable within the document (e.g. `b12`). Targets of relations. |
| `type` | `BlockType` | ✅ | Closed set (below). |
| `text` | string | ✅ | Normalized text. May embed `[^id]` markers (rendering convenience only). |
| `level` | int | — | Heading depth (1–6) or list nesting; `0`/absent otherwise. |
| `order` | int | — | Reading-order index; defaults to array position. |
| `locator` | `Locator` | — | Provenance; omit when the format has none. |
| `attrs` | object | — | Type-specific (e.g. table `columns`, code `lang`, record `row`). |

### `BlockType` (closed set)

| Value | Meaning |
| --- | --- |
| `heading` | Section title; carries `level`. |
| `paragraph` | Body prose. |
| `list` / `list_item` | List container / item; `level` = nesting. |
| `table` | A table kept whole (never split by the chunker). |
| `caption` | Caption bound to a table/figure via `caption_of`. |
| `code` | Code block; `attrs.lang` optional. |
| `blockquote` | Quoted block. |
| `figure` | Image/figure reference. |
| `footnote` | A footnote/endnote **definition**. |
| `record` | One row of a tabular source (csv/xlsx). |
| `page_break` | Page boundary marker (geometry-bearing formats). |

> v0 mapping: `body` → `paragraph`, `record`/`table` unchanged, `notes` (the
> orphan-footnote chunk) → a `footnote` block with no inbound `footnote_ref`.

### `Locator` (all optional)

| Field | Type | Notes |
| --- | --- | --- |
| `page` | int | 1-indexed. |
| `bbox` | `[x0,y0,x1,y1]` | Page coordinates. |
| `char_start` / `char_end` | int | Offsets into the source text. |
| `path` | string | Section breadcrumb / DOM path, e.g. `Mature Stacks > Observability`. |

### `Relation`

| Field | Type | Notes |
| --- | --- | --- |
| `type` | `RelationType` | `footnote_ref` \| `caption_of` \| `contains` \| `cross_ref` |
| `from_id` | string | Source block id. |
| `to_id` | string | Target block id. |

**v1 scope:** `footnote_ref` only is required to be produced. The other
`RelationType` values are reserved (schema-valid) but **deferred** — backends may
emit them, consumers may ignore them, until a feature needs them.

---

## Footnotes — the worked example

This is the relation that v1 makes load-bearing.

- The definition is a `Block{type: "footnote", id: "fnX"}`.
- A citation is a `Relation{type: "footnote_ref", from_id: <citing block>, to_id: "fnX"}`.
- The chunker attaches a footnote by **walking `footnote_ref` relations** for the
  blocks in a chunk — not by scanning for an inline token. The `[^id]` marker may
  still appear in `text` (and the merged definition is still appended to the
  chunk so the chunk stays self-contained), but the marker is presentation; the
  relation is the source of truth.
- **Why this matters:** a PDF backend finds a footnote by geometry (a superscript
  near a page-bottom block) and leaves no inline token. Token-based linkage (v0)
  breaks there; relation-based linkage (v1) does not.

Behavior the chunker must preserve (already true in v0, restated against v1):
duplicate a definition into every citing chunk; carry footnote ids in chunk
`meta`; collect uncited `footnote` blocks into a trailing chunk rather than
dropping them.

---

## Serialization & storage

The canonical `Document` is plain JSON. The ingestion pipeline turns it into
chunks (`chunk_layout` / `chunk_records`) and persists each chunk to the
`documents` table; the canonical provenance survives in the `meta jsonb` column:

```
documents.meta = {
  "schema_version": "1",
  "doc": "<source>",
  "format": "pdf",
  "section": "Mature Stacks > Observability",
  "footnotes": ["otel", "graph"],   // ids merged into this chunk
  "blocks": ["b8", "b9"],           // canonical block ids in this chunk
  "page": 3                          // from the chunk's leading block locator
}
```

Retrieval already returns `content`; with this, the generate step can cite
section + page provenance, which is the concrete enabler for the
governance/audit roadmap item (a chunk that points back to where it came from).

### Example

```json
{
  "schema_version": "1",
  "source": "sample",
  "format": "markdown",
  "meta": { "title": "Mature AI Stacks" },
  "blocks": [
    { "id": "b1", "type": "heading", "level": 2, "order": 0, "text": "Observability",
      "locator": { "path": "Mature AI Stacks > Observability" } },
    { "id": "b2", "type": "paragraph", "order": 1,
      "text": "Observability sits beside the hot path[^otel]." },
    { "id": "b3", "type": "footnote", "order": 2,
      "text": "OpenTelemetry GenAI semantic conventions, exported over OTLP." }
  ],
  "relations": [
    { "type": "footnote_ref", "from_id": "b2", "to_id": "b3" }
  ]
}
```

---

## Versioning

`schema_version` is a string (`"1"`, `"2"`, …). **Additive** changes (new
optional field, new reserved `RelationType`) keep the major version; a
**breaking** change (renamed/removed field, changed `BlockType` semantics) bumps
it. Consumers should read `schema_version` and reject unknown majors rather than
mis-parse. Extractors stamp the version they emit.

---

## Deferred (explicitly out of v1)

Kept out to honor the "smallest honest version" ethos — add only when a consumer
needs them:

- `caption_of`, `contains`, `cross_ref` relations as *required* output.
- Structured table cells (tables are whole-text blocks in v1).
- Nested/figure-rich layout, multi-column reading-order reconstruction.
- Multi-modal blocks (image/audio payloads) — tracked under the multi-modal
  ingestion horizon item.
