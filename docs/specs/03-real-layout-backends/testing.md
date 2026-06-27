# Real layout backends — test & verification plan

How each [acceptance criterion](README.md#acceptance-criteria) is proven, the
fixtures needed, and how it gates merge. Illustrative extractors live in
[`examples/`](examples/) (`pdf_extractor.py`, `docx_extractor.py`,
`xlsx_extractor.py`); port them to `app/layout.py` and the tests to
`tests/test_layout_backends.py` when implementing.

## The gate today (and what this adds)

There is **no `.github/` Actions workflow** yet — "CI" is `uv run pytest`
(Makefile `test:`), and the literal merge gate is
`tests/test_evals.py::test_quality_gate`. That gate is **unchanged** here.

This feature adds a **hermetic** suite `tests/test_layout_backends.py`, collected
by the same default `uv run pytest`. It needs **no** DB, gateway, or provider key
(it parses committed fixture files in-process), so it gates on every run.

| Layer | Stack needed | Runs in the gate? |
| --- | --- | --- |
| `tests/test_layout_backends.py` (per-format extractor units) | none (hermetic) | **yes**, always |
| integration through `chunk_layout` / `chunk_records` | none (pure) | **yes** |
| `docling` adapter smoke | `docling` installed out-of-band | **no** — `pytest.importorskip("docling")` |

> Depends on **spec 02** (canonical model): the PDF footnote-relation assertions
> require the relation-based chunker (`_attach_footnotes` walking `footnote_ref`,
> not the `[^id]` token). Don't land PDF footnote tests ahead of 02.

## Fixtures (committed, hermetic)

- **`tests/fixtures/footnotes.pdf`** — single-column, text-layer PDF; **≥2
  footnotes whose citations span ≥2 pages**, plus planted **distractors that must
  not become footnotes**: a page number, a running header, a running footer; and
  **one uncited/unlinkable** footnote (for the orphan path). Build it with a
  scripted, version-pinned generator and commit the binary (reproducibility).
- **`tests/fixtures/sample.docx`** — a heading, a body paragraph, **≥1 real
  footnote** in `word/footnotes.xml`, **and** the `separator` /
  `continuationSeparator` pseudo-footnotes Word always writes (`w:type` set, ids
  ≤ 0) so the filter is exercised.
- **`tests/fixtures/sample.xlsx`** — header row + N data rows including a
  **date/datetime cell**. Commit a binary saved by a real spreadsheet app if it
  contains any formula cells (the `data_only=True` cached-value caveat in
  `design.md`/OQ4); otherwise literal values are fine.
- Pin exact `pypdfium2` / `python-docx` / `openpyxl` versions; assert on
  **structure** (block types, ids, relations, locators) — never exact whitespace.

## Acceptance criteria → proof

| # | Criterion | How it is proven |
| --- | --- | --- |
| 1 | `pdf`/`docx`/`xlsx` registered (no `_RequiresBackend`) | `test_extractors_registered` — `layout.get_extractor(f)` returns a real extractor for each, and raises for an unknown format |
| 2 | Each emits a schema-valid canonical `Document`; no router/chunker change | `test_<fmt>_schema_valid` validates closed `BlockType` / stable ids / stamped `schema_version`; a `git diff --stat` check in review confirms only `app/layout.py` + deps changed |
| 3 | Footnoted PDF → `footnote_ref`, no inline token; 100% recall + 0 FP | `test_pdf_footnotes` over `footnotes.pdf`: asserts every planted citation→definition pair is linked (recall = 1.0) **and** none of the distractors (page no./header/footer) produced a `footnote` block or relation (FP = 0); asserts no `[^id]` token in the source PDF text |
| 4 | Uncited PDF footnote → trailing `footnote` block, no inbound relation | same fixture: the unlinkable footnote appears as a `footnote` block with no inbound `footnote_ref` (orphan parity with v0) |
| 5 | DOCX footnote from `word/footnotes.xml`; separators filtered | `test_docx_footnotes`: heading/body/footnote round-trip; **zero** `footnote` blocks from the `separator`/`continuationSeparator` entries; `w:footnoteReference/@w:id` resolves to the matching `w:footnote/@w:id` |
| 6 | XLSX N rows → N `record`s, cached values, JSON-safe `attrs` | `test_xlsx_records`: N records with correct header→value map; **`json.dumps(block.attrs)` and `json.dumps(chunk.meta)` both succeed** with the date cell (date → ISO-8601) — guards the ingestion `json.dumps(c.meta)` crash |
| 7 | One golden fixture + unit test per format, hermetic, pinned | the three tests above run under `uv run pytest tests/test_layout_backends.py` with nothing else running |
| 8 | Heavy deps out of `uv.lock` | `test_lock_excludes_heavy` greps the committed `uv.lock`: contains `pypdfium2`/`python-docx`/`openpyxl`, contains **no** `docling`/`pymupdf`/torch/transformers |
| 9 | Default deps permissively licensed | review check: `[project.dependencies]` PDF backend is `pypdfium2` (Apache/BSD); `pymupdf` only via the out-of-band opt-in |
| 10 | `docling` adapter documented + smoke skipped when absent | `test_docling_smoke` guarded by `pytest.importorskip("docling")`; excluded from the default run |

## Integration (still hermetic)

`test_<fmt>_end_to_end` runs each fixture through `chunks_for_file()` →
`chunk_layout`/`chunk_records` and asserts: the PDF/DOCX footnote definition is
**duplicated into its citing chunk** and carried in chunk `meta` via the
relation path (per spec 02), and XLSX rows become `record` chunks. No DB needed
— assert on the returned `Chunk` objects.

## Example test (project idiom)

Mirrors `tests/test_evals.py`'s plain-pytest style; the load-bearing PDF case:

```python
# Fixture invariants (see "Fixtures" above): PLANTED_CITATIONS citation→definition
# pairs across >=2 pages, PLANTED_DEFINITIONS cited definitions, plus exactly one
# uncited (orphan) footnote. Distractors (page no./header/footer) must yield none.
PLANTED_CITATIONS = 2
PLANTED_DEFINITIONS = 2

def test_pdf_footnotes_zero_fp_full_recall():
    doc = layout.get_extractor("pdf").extract(
        Path("tests/fixtures/footnotes.pdf").read_bytes(), "fn")
    refs = [r for r in doc.relations if r.type == "footnote_ref"]
    fns  = [b for b in doc.blocks if b.type == "footnote"]
    linked_targets = {r.to_id for r in refs}
    # 100% recall AND zero false-positive links: exact count, not >=.
    assert len(refs) == PLANTED_CITATIONS
    assert all("[^" not in b.text for b in doc.blocks)   # no inline token synthesized
    # zero false-positive footnote blocks from distractors: cited defs + 1 orphan only
    assert len(fns) == PLANTED_DEFINITIONS + 1
    # exactly one orphan: a footnote block with no inbound footnote_ref relation
    orphans = [f for f in fns if f.id not in linked_targets]
    assert len(orphans) == 1
```

## Rollout verification

- Merge: backends are inert until `register()`d and a matching `fmt` is ingested
  — no flag. Confirm `app/layout.py` no longer has `_RequiresBackend` for these
  three and the unit suite is green.
- Manual end-to-end: `python -m app.ingest tests/fixtures/footnotes.pdf` (stack
  up) and confirm the footnote rides into its citing chunk.
- `docling`: `make docling` (out-of-band install), then run the skipped smoke
  test to confirm the adapter maps `DoclingDocument` → canonical `Document`.
