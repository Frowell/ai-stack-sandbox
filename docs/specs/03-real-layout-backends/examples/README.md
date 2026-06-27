# Examples — illustrative only

> **These files are a specification, not wired-in code.** They sketch *how* the
> design in [`../README.md`](../README.md) and [`../design.md`](../design.md) would
> land against the real files in `app/`, `db/`, `pyproject.toml`, and the
> `Makefile`. They are intentionally **not** importable from the app and must
> **not** be copied verbatim into `app/` as part of expanding this spec —
> implementation happens in a separate PR (after spec 02 merges). Import paths and
> signatures match the codebase (`app/layout.py`'s `register`/`LayoutExtractor`
> seam, PR #2's `chunk_layout`/`chunk_records`) so the gap to real code is small
> and obvious.
>
> The backends emit the **canonical `Document`** from spec 02
> ([`../../02-canonical-document-model/README.md`](../../02-canonical-document-model/README.md)),
> not PR #2's `LayoutDoc`. The sketches import `Document`/`Block`/`Relation`/
> `Locator`/`BlockType`/`SCHEMA_VERSION` from `app.layout` as the home spec 02
> assigns to those types; if spec 02 lands them in a different module, adjust the
> one import line.

**Files present in this directory** (the three extractor sketches — one per
default backend):

| File | Illustrates | Real target |
| --- | --- | --- |
| `pdf_extractor.py` | `pypdfium2` page/bbox extraction + geometry footnote linkage + false-positive rejection | `app/layout.py` (`PdfExtractor`) |
| `docx_extractor.py` | `python-docx` body + `word/footnotes.xml` reader with the separator filter | `app/layout.py` (`DocxExtractor`) |
| `xlsx_extractor.py` | `openpyxl` rows→records, all sheets, `data_only`, row cap | `app/layout.py` (`XlsxExtractor`) |

**Specified inline, not as separate example files.** The remaining artifacts this
spec needs are small enough to live in prose/code-blocks in the spec body rather
than as standalone files; they are intentionally *not* present here:

| Artifact | Where it is specified | Real target |
| --- | --- | --- |
| import-time `register("pdf", PdfExtractor())` calls | each extractor's module docstring + `design.md` §0 | bottom of `app/layout.py` |
| `XLSX_MAX_ROWS` / `XLSX_SHEETS` settings | `design.md` §8 (config table) | `app/config.py` |
| default-deps change + dropping the `pymupdf` extra | `design.md` §4.2 (TOML block) | `pyproject.toml` |
| out-of-band `requirements-docling.txt` / `make docling` | `design.md` §4.2 | repo root + `Makefile` |
| reproducible fixture builder + fixture contract | `design.md` §5 (fixture table) | `tests/fixtures/` builder |
| acceptance-criteria tests | `testing.md` (proof table + example test) | `tests/test_layout_backends.py` |

The matching verification plan is in [`../testing.md`](../testing.md).
