# Canonical document model — design notes

> Companion to [`README.md`](README.md). This file holds the *deeper* design:
> the seam diagram, the v0→v1 mapping table, the chunker's relation-walk
> algorithm, the two id-spaces (block ids vs marker keys), the version guard,
> the meta-shape contract across both ingest paths, and the alternatives that
> were considered and rejected. Illustrative code lives in
> [`examples/`](examples/); the proof plan in [`testing.md`](testing.md).
> **None of this is wired-in code — it is a spec.**
>
> ⚠️ **Standing caveat (binding).** Every claim below about `app/layout.py`,
> `app/chunking.py`, the footnote behavior, and the `documents.meta` column
> describes **PR #2's assumed merged shape**, which is *not yet in the tree*
> (verified: no `app/layout.py`, no `app/chunking.py`, `db/init.sql` has no
> `meta` column, and `app/ingest.py` ingests a flat `{source,content}` corpus
> into `(source, content, embedding)`). Re-baseline this file against PR #2's
> actual merge before implementing — see README *Open questions*.

## 1. Where the feature lives (the seam)

```
   md/html/csv extractor          (PR #3) pdf/docx extractor
   app/layout.py                   app/layout.py
        |                                |
        |  emits ──────────────┐  ┌──────┘  emits
        v                      v  v
        ┌──────────────────────────────────────────────┐
        │     canonical Document  (THE SEAM, v1)        │   <- this spec
        │  schema_version · source · format · meta ·    │
        │  blocks: Block[]      · relations: Relation[] │
        └──────────────────────────────────────────────┘
                       |                      ^
        chunk_layout() │  walks relations     │ reject unknown major
        chunk_records()│                      │ (adapter / stored manifest)
                       v                      |
              app/chunking.py  ──────────────┘
                       |
                       |  list[Chunk(text, meta)]
                       v
              app/ingest.py  ── stamps meta ──> documents(source, content, embedding, meta jsonb)
                       |
                       v
              retrieval.py  (UNTOUCHED — selects id, content only; never reads meta)
```

The canonical `Document` is the **extractor↔chunker interface**. Everything to
the left of the box (how a format becomes blocks) is backend-specific; everything
to the right (chunking, footnote linkage, storage) speaks only the canonical
schema and never branches on `format`. That is the same decoupling the gateway
gives the model *provider*, applied to the document *format*.

**Touched modules** (all introduced by PR #2): `app/layout.py`, `app/chunking.py`,
`app/ingest.py`. **Untouched:** `app/retrieval.py`, `app/agent.py`,
`app/gateway.py`, `gateway/litellm_config.yaml`, `evals/golden.jsonl`,
`db/init.sql` (no migration — see §6). **`app/evals.py`: gate logic untouched**;
it gains one *additive, non-behavioral* `keyword_mean` return key so the AC-5
no-regression delta has a *lower-variance* signal (it removes the judge-LLM call's
variance; the combined `mean_score` is additionally LLM-judge-noisy and is not the
gate). Note `keyword_mean` is **not** fully deterministic — `ask()` generation is
unpinned (`chat()` takes no temperature), so it still varies run-to-run; average it
over N≥3. See README Acceptance criteria / testing.md.

## 2. Types and the closed taxonomy

The v1 container is five dataclasses plus two enums and one constant
(`SCHEMA_VERSION = "1"`). The load-bearing property is that the taxonomy is
**closed and enforced at construction**, not merely documented:

```
# field listing (logical), NOT positional call order — schema_version carries a
# default (SCHEMA_VERSION) so in the dataclass it is declared last; construct with
# keywords (see examples/example_layout.py).
Document(schema_version, source, format, meta, blocks: list[Block], relations: list[Relation])
Block(id, type: BlockType, text, level?, order?, locator?: Locator, attrs?)
Locator(page?, bbox?, char_start?, char_end?, path?)     # all optional
Relation(type: RelationType, from_id, to_id)
BlockType(Enum): heading paragraph list list_item table caption code
                 blockquote figure footnote record page_break
RelationType(Enum): footnote_ref caption_of contains cross_ref
```

`Block.__post_init__` coerces/validates `type` against `BlockType` and raises on
anything outside the set (see [`examples/example_layout.py`](examples/example_layout.py)).
The point of enforcing rather than documenting: an unmapped backend `kind` fails
**loudly at extraction**, not silently three modules downstream when the chunker
or a `meta` consumer hits a `kind` it has never seen.

**Why dataclasses, not pydantic.** The repo has no pydantic dependency today
(`pyproject.toml`: openai, psycopg, redis, langgraph, otel, dotenv). The
canonical model is an *internal* in-process contract, not an external API
surface, so stdlib `dataclasses` + `enum` + a hand-rolled `__post_init__` check
keeps the dependency footprint at zero. (Spec 13-structured-outputs introduces
pydantic for *gateway output* validation — a different seam; we deliberately do
not couple to it.)

## 3. The v0 → v1 mapping (in the extractors)

Backends map *into* the closed set; they never invent kinds. The complete
mapping from PR #2's v0 `Element.kind` strings:

| v0 `kind` (PR #2)         | v1 `BlockType`            | Notes |
| ------------------------- | ------------------------- | ----- |
| `heading`                 | `heading`                 | `level` carried through |
| `body`                    | `paragraph`               | the rename |
| `table`                   | `table`                   | unchanged; kept whole |
| `record`                  | `record`                  | unchanged (csv/xlsx row) |
| *orphan `notes` chunk*    | `footnote` block          | no inbound `footnote_ref` → lands in the trailing uncited chunk |
| *(any other v0 `kind`)*   | **raises**                | closed taxonomy; enumerate-and-fail test guards this |

The mapping is exercised by a parametrized test over **every** v0 `kind` so a
backend that emits an unmapped string fails at the mapping boundary, not later.

**Optional fields are omitted, not faked.** md/html/csv have no geometry, so
`locator` is `None` (omitted from JSON), and `order` is omitted because the
`blocks` array index *is* the reading order (§4). Real `Locator` values arrive
with PR #3's PDF backend; that is when `char_start/char_end`'s base (raw bytes
vs normalized text) gets decided (README open question), because there is finally
a real producer.

## 4. Ordering: the array is the source of truth

`blocks` is an ordered list and that order **is** reading order. `Block.order`
is advisory/redundant: if present it must equal the array index, and the chunker
reads the array and never re-sorts by `order`. This avoids a class of bug where
`order` and array position disagree and two consumers pick different ones. The
`CANONICAL_MODEL.md` example carries `order: 0,1,2` matching position; v1 keeps
that invariant but does not *require* extractors to emit `order` at all.

## 5. Footnotes as relations (the load-bearing refactor)

This is the only relation v1 is required to *produce*. v0 linked footnotes by
scanning chunk text for an inline `[^id]` token. v1 walks typed edges instead.

### 5.1 The two id-spaces — and why there is only one

`CANONICAL_MODEL.md`'s worked example **was** internally inconsistent: its
relation said `to_id: "b3"` (a block id) but its prose `meta.footnotes` said
`["otel"]` (a human marker key). v1 resolves this: **there is one namespace, the
block-id space** — and `CANONICAL_MODEL.md` has now been corrected to match
(`meta.footnotes` carries block ids; the marker moved to a blockquote note).

```
   ┌─ block id space (canonical) ─┐         ┌─ marker key space (rendering) ─┐
   relation.to_id   = "b3"                  text contains "[^otel]"
   meta.footnotes   = ["b3"]   ◀── SAME ──▶ attrs.marker = "otel"  (optional UX)
   meta.blocks      = ["b2","b3"]
```

`footnote_ref.to_id` targets the **footnote block id**; `meta.footnotes` carries
those **same block ids**. The marker key (`otel`) is presentation only and, if a
citation UX wants it, it lives in `attrs.marker` — never in an id and never in
`meta.footnotes`. This keeps the chunker from ever translating between two
namespaces. `CANONICAL_MODEL.md`'s example has been corrected accordingly
(`meta.footnotes` in block-id space).

### 5.2 The chunk-time algorithm (relation walk, not token scan)

```
chunk_layout(doc):
    cited_fn = {}                       # footnote block id -> definition text
    for chunk in build_text_chunks(doc.blocks):     # PR #2's existing chunking
        ids = [b.id for b in chunk.blocks]
        fn_ids = [r.to_id for r in doc.relations
                  if r.type == footnote_ref and r.from_id in ids]   # ← the walk
        for fid in fn_ids:
            chunk.text += "\n\n" + block_by_id[fid].text            # self-contained
            cited_fn[fid] = ...
        chunk.meta = {schema_version, doc, format, section,
                      footnotes: fn_ids, blocks: ids, page?}
        emit(chunk)
    # uncited footnote blocks → one trailing chunk (preserve v0 behavior)
    uncited = [b for b in doc.blocks if b.type == footnote and b.id not in cited_fn]
    if uncited: emit(trailing_chunk(uncited))
```

Behavior preserved from v0 (now stated against v1):

- The definition is **duplicated into every citing chunk** so each chunk stays
  self-contained for retrieval.
- Footnote ids ride in `meta.footnotes`.
- **Uncited** `footnote` blocks (the v0 orphan-`notes` case) collect into a
  trailing chunk rather than being dropped.

The crucial new test: linkage must work **with the `[^id]` token absent from
`text`**, because a PDF backend recovers a footnote by geometry and leaves no
inline marker. Token-based v0 linkage breaks there; relation-based v1 linkage
does not.

## 6. Storage and the (no-)migration

Per-chunk provenance is written to the **existing** `documents.meta` jsonb column
(added by PR #2). Because `meta` is jsonb, the new keys (`schema_version`, `doc`,
`format`, `section`, `footnotes`, `blocks`, `page?`) need **no DB migration**.
Rollout is a full re-ingest — `ingest()` already does
`TRUNCATE … RESTART IDENTITY` then re-inserts.

> **"No migration" has a precondition.** It is true only for the jsonb *keys*; it
> assumes the `meta` *column itself* exists. `db/init.sql` is
> `CREATE TABLE IF NOT EXISTS` and runs once on first volume init, and `TRUNCATE`
> does not add columns. So on a **pre-existing** DB volume the column is absent
> and an insert that writes `meta` will fail. Operational precondition: deploy
> with the column already present — either a fresh volume (`docker compose down
> -v`) or a one-time `ALTER TABLE documents ADD COLUMN IF NOT EXISTS meta jsonb`
> shipped by PR #2. **This spec asserts the precondition; it does not add a
> column** (its `db/init.sql` is unchanged).

### Two ingest paths, one meta shape (the cross-path contract)

`app/ingest.py` today reads a flat `{source, content}` corpus
(`data/corpus.jsonl`). After PR #2 there are two paths that both write `meta`:

| Path | Produces `meta` with | `blocks` | `relations` |
| ---- | -------------------- | -------- | ----------- |
| **Layout** (extractor → `chunk_layout`) | full provenance | populated block ids | walked |
| **Back-compat raw row** (`{source, content}`) | `schema_version` (+ empty `blocks`) | **empty / absent** | none |

Both carry `schema_version` from the single `SCHEMA_VERSION` constant, so a
consumer can rely on the *meta envelope* existing on every row. But the two are
**distinguishable**: a raw row has empty/absent `blocks`. Consumers MUST treat
empty `blocks` as *"no canonical structure for this row"*, **not** as *"a valid
v1 document that happens to have zero blocks."* `schema_version` on a raw row
asserts meta-shape compatibility only — not that the canonical extractor produced
the row. A unit test asserts the two metas are distinguishable.

## 7. Versioning guard

`schema_version` is a string (`"1"`, `"2"`, …). A reader rejects an unknown
**major** (string-compare the leading integer) rather than mis-parse; a
same-major doc is accepted. In v1 there is no real multi-version corpus, so the
guard fires only at two boundaries:

1. a **third-party IR adapter** (docling/unstructured → canonical), and
2. a **stored canonical-JSON manifest** read back by `ingest`.

The in-process producer and consumer share the `SCHEMA_VERSION` constant, so the
guard is a forward-compat assertion exercised by a unit test until one of those
boundaries actually exists (README open question).

## 8. Explicitly out of scope (deferred)

- **Surfacing `meta` into retrieval/generate** so answers cite section+page.
  `retrieval.py` selects only `id, content` and never reads `meta`; the citing UX
  is spec [15-governance-and-audit](../15-governance-and-audit/README.md) (via
  [05-retrieval-uses-chunk-metadata](../05-retrieval-uses-chunk-metadata/README.md)),
  not this spec. **This spec stops at writing `meta`.**
- `caption_of` / `contains` / `cross_ref` as *required* output (reserved,
  schema-valid, may be emitted and ignored).
- Structured table cells; multi-modal blocks
  ([18-multi-modal-ingestion](../18-multi-modal-ingestion/README.md)).
- Real `Locator` values and cross-re-ingest stable block ids — both land with /
  are decided by PR #3 ([03-real-layout-backends](../03-real-layout-backends/README.md)).

## 9. Alternatives considered

| Alternative | Why rejected for v1 |
| ----------- | ------------------- |
| **Keep the free-string `kind`** | the whole motivation: a free string survives md/html but rots the moment a geometry-only backend or third-party IR emits an unmapped kind; closed enum + enforce-at-construction is the fix |
| **pydantic models for the canonical types** | adds a dependency the repo doesn't have for an *internal* contract; dataclasses + `__post_init__` suffice. (pydantic is spec 13's tool, for a different seam) |
| **Keep token-based footnote linkage** | breaks for PDF (geometry-recovered footnotes leave no `[^id]` token); relations survive backends that have no inline marker |
| **Two id-spaces (marker keys in `meta`, block ids in relations)** | forces the chunker to translate between namespaces and is the exact inconsistency in `CANONICAL_MODEL.md`; collapse to block ids, push marker keys to `attrs` |
| **Add a `meta` DB migration here** | unnecessary — `meta` is jsonb and the column is PR #2's; this spec only adds *keys*. Re-ingest is the only rollout step |
| **Content-derived (hash) block ids for cross-re-ingest stability** | nothing downstream persists a block id beyond one rebuild today; deferred to governance (README open question) |
| **Gate retrieval quality on the absolute eval floor only** | the gate is `mean >= 0.7` over 4 cases with a nondeterministic judge — an absolute pass can hide a real drop; use a **before/after delta** instead (§ testing) |

## 10. Edge cases checklist

- Block with `type` outside the enum → raises at construction.
- `order` present but ≠ array index → invalid (array wins; chunker ignores `order`).
- Footnote cited from a block whose chunk does not contain the footnote block →
  definition still appended to the citing chunk (duplication is intended).
- Footnote block cited from **two** chunks → definition duplicated into both;
  its id appears in both chunks' `meta.footnotes`.
- Footnote block cited from **zero** blocks → trailing uncited chunk.
- `[^id]` token present in `text` but **no** relation → not linked (relation is
  the source of truth; the token is rendering only).
- Raw `{source, content}` row → `meta.schema_version` set, `blocks` empty;
  distinguishable from a layout chunk.
- Unknown-major `schema_version` at adapter/manifest boundary → raises.
