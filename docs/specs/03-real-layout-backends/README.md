---
title: Real layout backends
slug: real-layout-backends
area: ingestion
tier: Next
size: L
status: Todo
depends_on: [canonical-document-model]
issue:        # set to the GitHub issue number when created
---

# Real layout backends

> **Area** `ingestion` · **Tier** `Next` · **Size** `L` · **Status** `Todo` · **Depends on:** [canonical-document-model](../02-canonical-document-model/README.md)

## Summary

Turn the `pdf`/`docx`/`xlsx` `_RequiresBackend` placeholders in `app/layout.py`
into real extractors that emit a valid canonical `Document` (per spec 02), so the
most common real-world layout/footnote sources can actually be ingested. Ship
three self-contained, permissively-licensed default backends (`pypdfium2`,
`python-docx`, `openpyxl`) plus a `docling` adapter that is opt-in and installed
out-of-band (never in `uv.lock`). Each backend
registers through the existing `layout.register(fmt, extractor)` seam so nothing
downstream (router, `chunk_layout`/`chunk_records`, retrieval, storage) changes.

## Problem / Motivation

PDF/DOCX/XLSX are register-a-backend stubs today (`_RequiresBackend` in
`app/layout.py`); the most common real-world layout/footnote sources can't
actually be ingested. The dependency-free defaults (markdown/html/csv) only cover
text formats, so the canonical model's locator/relation richness has no real
producer until a geometry-bearing backend exists.

## Goals

**Must-have (default backends, CI-gated):**

- PDF (`pypdfium2` default — see Packaging for the AGPL/license rationale that
  rules out `pymupdf` as a default): page/bbox `Locator`s and **geometry-based
  footnote linkage** via `footnote_ref` relations. Single-column, text-layer PDFs
  only.
- `python-docx` (DOCX — headings via the high-level API; footnotes read from the
  `word/footnotes.xml` part, since python-docx does **not** expose them through
  its public object model).
- `openpyxl` (XLSX — one `record` block per data row, fed to `chunk_records`).
- One golden fixture + extractor test per format, runnable in CI without network.

**Opt-in (not CI-gated):**

- `docling` as a heavier adapter that maps `DoclingDocument` → canonical
  `Document` (documented, **not** pinned in the default lock).

## Non-goals

- OCR for scanned / image-only PDFs (no text layer).
- Multi-column reading-order reconstruction.
- Structured table cells (tables stay whole-text blocks, per spec 02).
- DOCX **endnotes** (`word/endnotes.xml`) — only footnotes (`word/footnotes.xml`)
  are extracted in v1; endnotes are deferred (separate part, same adapter shape).
- Persisting bbox/page provenance to `documents.meta` — that depends on the
  `meta jsonb` column landing in spec 02 (see Dependencies / Open questions).

## Proposed design

**Seam.** Each backend is a class implementing the `LayoutExtractor` protocol
(`extract(self, raw, source) -> LayoutDoc`/`Document`) and is wired with
`layout.register("pdf", PdfExtractor())` at import time, replacing the
`_RequiresBackend` placeholder. `get_extractor(fmt)` already raises a clear error
for unregistered formats. Format selection stays by extension/`fmt` string as it
is today; binary backends receive `raw` as `bytes` (the existing `_text` helper
decodes text formats; binary backends parse `raw` directly and must not call it).

**PDF (`pypdfium2`).** Walk pages; for each page emit text blocks with their
bounding boxes in top-to-bottom, left-to-right order (single-column assumption),
attaching a `Locator{page, bbox}`. Headings inferred from font-size/weight
relative to the page body median. **Footnote linkage** is the load-bearing,
highest-risk piece and gets its own algorithm + accuracy bar in `design.md`:
detect footnote *definitions* as small-font blocks in the bottom margin band of a
page; detect *citations* as superscript-sized runs in body blocks; link
citation→definition by marker glyph (e.g. `¹`/`1`) within the page, emitting a
`footnote_ref` relation and a `footnote` block. Must reject false positives
(page numbers, running headers/footers, line numbers) and degrade gracefully
(unlinked footnote → trailing `footnote` block with no inbound relation, exactly
the v0 orphan behavior).

**DOCX (`python-docx`).** Paragraphs → `paragraph`/`heading` (by style name),
tables → whole-text `table` blocks. Footnotes are **not** on the public API: open
the `.docx` zip part `word/footnotes.xml`, map each `w:footnote` to a `footnote`
block, and resolve `w:footnoteReference` runs in body paragraphs to
`footnote_ref` relations.

**XLSX (`openpyxl`).** First non-empty row = header; each subsequent row → one
`record` block whose `attrs.row` holds the cell map (header→value), read with
`data_only=True` (cached formula values, not formulas). **Cell values must be
coerced to JSON-safe scalars** before going into `attrs.row`: `openpyxl` returns
`datetime.datetime`/`datetime.date`/`datetime.time` for date cells (and these are
common), but `attrs` is ultimately persisted via `json.dumps(c.meta)`
(`app/ingest.py`), which raises `TypeError` on a raw `datetime`. Coerce: dates →
ISO-8601 strings, `int`/`float`/`bool`/`None`/`str` pass through, anything else →
`str(v)`. Sheet selection and a row cap are config (see Open questions). **Default decision for v1:** ingest
**all** sheets, skip fully-empty rows, and apply a configurable per-document row
cap (`XLSX_MAX_ROWS`, default **10 000**) — rows beyond the cap are dropped with
a logged warning rather than silently exploding into chunks/embeddings.
`chunk_records(doc, batch)` already batches records into chunks.

**Packaging.** `python-docx` and `openpyxl` move into the default
`[project.dependencies]` (pure-Python wheels, no system libs). The PDF backend
needs a license decision (see below) before it can be a default dependency.

*docling must NOT be a uv `[dependency-groups]` group.* uv resolves and writes
**all** declared groups — default and optional alike — into the single universal
`uv.lock`. Putting `docling` in any group would drag its entire transitive ML
stack into `uv.lock` (massive lock bloat + a heavy resolution every `uv lock`),
even though `uv sync` would not *install* it by default. To keep the committed
lock free of the ML stack, `docling` lives **outside** the uv project entirely:
documented as an out-of-band install (a separate `requirements-docling.txt`
installed via `uv pip install -r requirements-docling.txt` into the active venv,
fronted by a `make docling` target), never referenced from `pyproject.toml` and
never part of `uv.lock`.

*PDF backend license decision (load-bearing).* `pymupdf` (MuPDF) is
**AGPL-3.0 / commercial**, not a permissive wheel; promoting it into default
`[project.dependencies]` makes the default install (and any network service built
on it) carry AGPL obligations. Either (a) accept AGPL explicitly and record it as
an accepted risk, or (b) use `pypdfium2` (Apache-2.0/BSD, also a self-contained
wheel) which exposes the same per-page text + bbox geometry the footnote
algorithm needs. **v1 decision:** default to `pypdfium2` for the page/bbox/text
extraction to keep the default dependency set permissively licensed; `pymupdf` is
allowed only behind the same opt-in mechanism as `docling` if its richer text
extraction is later needed. The footnote-linkage algorithm in `design.md` must be
written against the chosen library's geometry API, not assumed identical across
the two.

**No schema/API changes** beyond what spec 02 introduces. No router or chunker
changes (the whole point of the seam).

## Acceptance criteria

- [ ] `pdf`/`docx`/`xlsx` each have a registered extractor (no `_RequiresBackend`
      left for them); `get_extractor(fmt)` returns a real extractor.
- [ ] Each backend produces a canonical `Document` that validates against the
      spec-02 schema (closed `BlockType`, stable ids, `schema_version` stamped);
      `register()` wires it with **no** router/chunker changes (verified by diff).
- [ ] A single-column, text-layer footnoted PDF round-trips with the footnote
      attached to its citing chunk via a `footnote_ref` relation, with **no**
      inline `[^id]` token present in the source PDF text. The golden fixture is
      **non-trivial**: ≥2 footnotes with citations spanning ≥2 pages, plus
      planted distractors that must NOT become footnotes (at minimum a page
      number, a running header, and a running footer). Bar: **100% recall on the
      fixture's planted citations** (every planted citation→definition pair is
      linked) AND **zero false-positive links/blocks** from the distractors. A
      single-footnote toy fixture does not satisfy this criterion.
- [ ] An uncited / unlinkable PDF footnote degrades to a trailing `footnote`
      block (no inbound relation), matching v0 orphan-footnote behavior.
- [ ] DOCX fixture round-trips a heading, a body paragraph, and a footnote
      sourced from `word/footnotes.xml` (not the public API). The reader **skips
      the `separator` / `continuationSeparator` pseudo-footnotes** Word always
      writes (the `w:footnote` entries with `w:type` set, ids ≤ 0); the fixture
      includes them and the test asserts **zero spurious `footnote` blocks** from
      them. `w:footnoteReference/@w:id` is resolved to the matching
      `w:footnote/@w:id` (these are footnote-local ids, independent of
      `endnotes.xml`). Endnotes (`word/endnotes.xml`) are out of scope for v1
      (see Non-goals).
- [ ] XLSX fixture round-trips N data rows into N `record` blocks with correct
      header→value mapping, using cached values (no formula strings). The fixture
      **includes a date/datetime cell**, and the test asserts `json.dumps` of the
      resulting `Block.attrs` (and of the end-to-end chunk `meta`) succeeds — i.e.
      every cell value is coerced to a JSON-safe scalar (dates → ISO-8601 string),
      so ingestion's `json.dumps(c.meta)` cannot raise on a real spreadsheet.
- [ ] One golden fixture + extractor unit test per default format, hermetic (no
      network), green under the pinned library versions.
- [ ] Heavy deps stay out of the lock entirely: `uv.lock` contains the default
      PDF backend (`pypdfium2`)/`python-docx`/`openpyxl` and **no** `docling`,
      `pymupdf`, or any of their transitive ML/AGPL stack. (`docling`/`pymupdf`
      are *not* uv dependency-groups — a group would still be written into the
      universal `uv.lock`; they install out-of-band via
      `requirements-docling.txt` / a `make docling` target.) Verified by grepping
      the committed `uv.lock`.
- [ ] Default `[project.dependencies]` are permissively licensed (the PDF backend
      is `pypdfium2`, Apache/BSD); if `pymupdf` is ever introduced it stays behind
      the same opt-in out-of-band install as `docling`, never in the default set.
- [ ] `docling` adapter is documented with a smoke test that is **skipped** when
      docling isn't installed (`pytest.importorskip`); not part of the merge gate.

## Dependencies

- [canonical-document-model](../02-canonical-document-model/README.md) — **hard
  prerequisite.** This spec's PDF-footnote-via-`footnote_ref` acceptance criterion
  requires spec 02's relation-based chunker (`_attach_footnotes` walking
  relations, not the `[^id]` token) to be in place. Do not start PDF footnote
  work until 02 lands.
- A real CI workflow does not exist yet (`.github/` is empty; CI is
  [ci-hardening](../07-ci-hardening/README.md)). "Per-format tests in CI" is
  satisfied today by `make test` / `uv run pytest`; the CI wiring is 07's job.

## Open questions

- **OQ1 — provenance storage. (storage half RESOLVED.)** Verified on
  `origin/feat/hybrid-ingestion` (PR #2): `db/init.sql` **already declares**
  `meta JSONB NOT NULL DEFAULT '{}'::jsonb` on `documents` plus a
  `documents_meta_idx` GIN index, and `app/ingest.py` persists chunk metadata via
  `json.dumps(c.meta)` into that column. So PDF page/bbox locators **do** have a
  column to land in (via chunk `meta`); no new migration is needed here. The
  remaining open half is *who surfaces a block's `Locator`/`attrs` into chunk
  `meta`* — that is spec 02's chunker rewrite, not this spec. This spec only
  *produces* locators/attrs in-memory. **Consequence (see OQ6):** because
  `json.dumps` is the persistence path, every value this spec puts in
  `Block.attrs` must be JSON-serializable or ingestion raises at insert time.
- **OQ2 — XLSX sheet & size policy.** Which sheets ingest (first / all / named)?
  What is the row cap to avoid a 100k-row sheet exploding into 100k chunks and an
  embedding-cost blow-up? Default proposal: all sheets, configurable cap, skip
  fully-empty rows.
- **OQ3 — heading inference for PDF.** Font-size heuristic vs. fixed mapping —
  acceptable to emit everything as `paragraph` for v1 and defer heading
  detection? (Reading-order reconstruction is already a non-goal.)
- **OQ4 — fixture authoring.** How are binary `.pdf`/`.docx`/`.xlsx` fixtures
  generated and checked in reproducibly (script that builds them vs. committed
  binaries)? Affects golden-test determinism across library versions. **Caveat
  for XLSX:** `openpyxl` with `data_only=True` returns the *cached* formula
  result, which only exists if a real spreadsheet application last saved the file.
  A fixture written programmatically by openpyxl has **no cached value** for
  formula cells (reads as `None`). So the XLSX golden fixture must either contain
  only literal values, or be a committed binary saved by a real spreadsheet app —
  do not assert formula-derived values from a script-built openpyxl fixture.
- **OQ5 — binary `raw` contract (PR #2 dependency). RESOLVED against the PR #2
  branch.** Verified on `origin/feat/hybrid-ingestion`: `app/ingest.py`
  `chunks_for_file(source, fmt, raw: bytes)` is fed `path.read_bytes()` and passes
  `raw` **undecoded** straight to `get_extractor(fmt).extract(raw, source)`; only
  the text extractors call `_text()` to decode. PDF/DOCX already route through
  `chunk_layout` and XLSX (in `layout.TABULAR`) through `chunk_records`, so the
  "no router changes" claim holds and binary backends do receive bytes. The
  `LayoutExtractor` protocol there is `extract(self, raw: str | bytes, source)`.
  **Residual risk (Low):** this is the branch HEAD, not merged `main`; re-confirm
  the signature if the seam changes before PR #2 merges.
- **OQ6 — JSON-serializability of `attrs` (RESOLVED: coerce at the producer).**
  Chunk `meta` is persisted via `json.dumps(c.meta)` in `app/ingest.py`, so any
  non-JSON value an extractor puts in `Block.attrs` (XLSX date cells are the live
  case; PDF/DOCX attrs are already strings/numbers) would crash ingestion at
  insert time. Decision: extractors emit only JSON-safe scalars in `attrs`
  (dates → ISO-8601 strings, otherwise `str(v)` for non-primitives). Enforced by
  the XLSX acceptance criterion above.
- **OQ8 — block `order` / document order (DOCX, Low, open).** The DOCX sketch
  emits `footnote` blocks **first** (so citing paragraphs can reference them by id),
  then body paragraphs, then tables — i.e. the block *list* is not in document
  reading order, and no `Block.order` is set. The relation-based chunker links
  footnotes by `footnote_ref` regardless of list position, so the acceptance
  criteria hold; but any order-sensitive consumer (and the PDF "orphan → *trailing*
  block" parity) wants real reading order. *Resolution at implementation:* set
  `Block.order` to document order on every producer (footnotes ordered after the
  body), or build the list in reading order and resolve ids in a second pass. Not a
  blocker for v1's criteria; tracked so it isn't silently dropped.
- **OQ9 — multi-line/merged block font size (PDF, Low, accepted).** In the sketch,
  `_blocks()` records a block's `size` from its **first** line and does not
  recompute it after merging subsequent lines, and `_is_definition`/`_block_type`
  read that `size`. For the single-column fixture this is fine (a footnote
  definition's lines share one small size); the implementer should use the block's
  median glyph size if mixed-size merges appear in real corpora. Accepted for v1.
- **OQ7 — superscript line-grouping interaction (PDF, RESOLVED in design.md).**
  Block reconstruction groups glyphs into lines by baseline proximity
  (`±0.3 × body`), but a true superscript citation is raised by ≈0.3–0.4 × font
  size and can be split into its own one-glyph "line", which would defeat the
  citation test (a lone raised glyph has no body baseline to be measured against).
  The citation detector must measure the raise against the **body baseline of the
  owning text line/block**, not the isolated run's own baseline. See design.md
  §1.2 / §1.4 and §7.

## Risks & mitigations

- **Geometry footnote linkage is heuristic and brittle (highest risk).** Bottom-
  margin / superscript detection will mislabel page numbers, running headers, and
  line numbers. *Mitigation:* bound scope to single-column text-layer PDFs;
  require **zero false positives** on the golden fixture over recall; always fall
  back to the orphan-`footnote` path; specify the full algorithm + a labeled
  fixture in `design.md` before coding.
- **python-docx footnote assumption.** The public API doesn't expose footnotes;
  naive use silently drops them. *Mitigation:* read `word/footnotes.xml` directly;
  cover with the DOCX fixture test.
- **DOCX separator pseudo-footnotes (false positives).** Word always writes
  `separator` / `continuationSeparator` `w:footnote` entries (ids ≤ 0); a naive
  iteration turns them into spurious `footnote` blocks for *every* real document.
  *Mitigation:* filter them by `w:type`; the DOCX fixture includes them and the
  test asserts zero spurious footnote blocks (acceptance criterion).
- **Golden tests drift across library versions.** pypdfium2/openpyxl text output
  changes between releases. *Mitigation:* pin exact versions, assert on
  structure/relations (ids, types, links) rather than exact whitespace, and pin
  fixture-generation tooling.
- **Dependency weight / install footprint.** Even the "default" backends add
  wheels; `docling` pulls a large ML stack. *Mitigation:* install `docling`
  out-of-band (a `requirements-docling.txt` + `make docling`), **not** as a uv
  group — a group would write the whole ML tree into the universal `uv.lock`
  regardless of install. Verify the committed lock excludes it (acceptance
  criterion).
- **PDF backend licensing.** `pymupdf`/MuPDF is AGPL-3.0/commercial; making it a
  default dependency imposes AGPL obligations on the whole stack. *Mitigation:*
  default to `pypdfium2` (Apache/BSD) for page/bbox/text; gate `pymupdf` behind
  the same opt-in out-of-band install as `docling` if ever needed. Write the
  footnote-linkage algorithm in `design.md` against the chosen library's geometry
  API (pdfium block/char boxes), not assumed cross-library identical.
- **XLSX embedding cost.** Each ingested row becomes a `record` block and
  ultimately an embedding; a 10k-row cap is still up to 10k embed calls per
  document. *Mitigation:* the `XLSX_MAX_ROWS` cap bounds the blast radius and the
  drop is logged; revisit the default if real corpora are wider than expected.
- **XLSX non-JSON cell values (ingestion crash).** `openpyxl` returns
  `datetime`/`date`/`time` objects for date cells; placed raw into `attrs.row`
  they crash `json.dumps(c.meta)` at insert, breaking ingestion of any spreadsheet
  with a date column. *Mitigation (OQ6):* coerce cell values to JSON-safe scalars
  in the producer (dates → ISO-8601, else `str`); fixture includes a date cell and
  the test asserts `json.dumps` succeeds.
- **PDF superscript split from its line (missed citations).** Baseline line-
  grouping can isolate a raised superscript glyph into its own "line", defeating
  the raised-vs-baseline citation test and silently dropping a citation (a recall
  miss against the zero-FP/100%-recall bar). *Mitigation (OQ7):* measure the raise
  against the owning block's body baseline, not the isolated run; the ≥2-page
  fixture exercises real superscripts so a regression reddens the gate.
- **Accepted risk:** docling adapter ships without CI enforcement (opt-in,
  unpinned). It can rot silently between releases; acceptable because it is a
  documented escape hatch, not a supported default.
- **Accepted risk:** binary/image-only (scanned) PDFs are out of scope and will
  produce empty/degenerate docs; surfaced as a clear "no text layer" condition,
  not OCR.

## Test & rollout plan

- **Unit (merge gate):** one hermetic extractor test per default format against a
  committed golden fixture, asserting canonical-schema validity, block
  types/ids, locators (PDF), and the footnote `footnote_ref` relation (PDF/DOCX).
  Negative test: orphan footnote → trailing block, no false-positive links.
- **Integration:** ingest each fixture end-to-end through `chunk_layout`/
  `chunk_records` and assert the footnote is duplicated into its citing chunk and
  carried in chunk `meta` (relation-based, per spec 02).
- **docling:** a smoke test gated on the `docling` group being installed
  (`pytest.importorskip`); excluded from the default `pytest` run.
- **Rollout:** no feature flag needed — backends are inert until `register()`d and
  a matching `fmt` is ingested. No data migration owned here (see OQ1). Ships
  behind the spec-02 canonical model; do not merge PDF footnote linkage ahead of
  02.

## References

- [Feature roadmap](../../ROADMAP.md)
- [Specs index](../README.md)
- [Canonical document model](../../CANONICAL_MODEL.md)
