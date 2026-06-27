# Design notes — Real layout backends

Deeper design behind [`README.md`](README.md). The README fixes *what* ships and
*why*; this file pins the load-bearing mechanics that are too detailed for the
spec body: the PDF footnote-linkage algorithm (the highest-risk piece), the DOCX
`footnotes.xml` reader, the XLSX policy, the packaging mechanics (why a uv extra
is *not* a hiding place either), alternatives considered, and the fixture-authoring
contract. Illustrative code for the three default backends lives in
[`examples/`](examples/) (`pdf_extractor.py`, `docx_extractor.py`,
`xlsx_extractor.py`); the packaging, config, and fixture-builder details are
specified inline in this file (§4, §8, §5) rather than as separate example files
(see [`examples/README.md`](examples/README.md)); the proof plan is in
[`testing.md`](testing.md).

This spec lands **after** spec 02 (canonical document model). The interface the
backends emit into is therefore spec 02's `Document`/`Block`/`Relation`, not PR
#2's `LayoutDoc`/`Element`. Where this matters the sketches use the canonical
types and name the dependency.

---

## 0. Grounding: the seam as it exists today

Verified against `origin/feat/hybrid-ingestion` (PR #2) and the post-02 contract
in [`../../CANONICAL_MODEL.md`](../../CANONICAL_MODEL.md):

- `app/layout.py` — `register(fmt, extractor)`, `get_extractor(fmt)`,
  `canonical(fmt)`, the `_ALIASES` map (already maps `pdf`/`docx`/`xlsx`), and
  `TABULAR = {"csv", "xlsx"}`. PDF/DOCX/XLSX are `_RequiresBackend(...)` stubs
  that raise `NotImplementedError` from `extract`.
- `LayoutExtractor` protocol: `extract(self, raw: str | bytes, source) -> ...`.
  `_text(raw)` decodes text formats; **binary backends never call it** and parse
  `raw: bytes` directly (PR #2 feeds `path.read_bytes()` through undecoded — see
  README OQ5).
- `app/ingest.py` `chunks_for_file(source, fmt, raw)` routes:
  `fmt in TABULAR → chunk_records(...)`; `fmt in {"markdown","html","pdf","docx"}
  → chunk_layout(...)`; else unstructured. So **XLSX must stay in `TABULAR`**
  (it is) and PDF/DOCX ride the `chunk_layout` branch. No router edit is needed —
  that is the whole point of the seam.
- `app/chunking.py` — `chunk_layout(doc)` (heading-anchored, table-whole, footnote
  attachment) and `chunk_records(doc, batch=1)` (one chunk per row). Spec 02
  rewrites their *internals* to walk the canonical model; this spec does **not**
  touch them.
- `db/init.sql` on the PR #2 branch already declares `meta JSONB NOT NULL DEFAULT
  '{}'::jsonb` plus `documents_meta_idx`. This **resolves the storage half of
  README OQ1**: once PR #2 merges, PDF page/bbox locators have a column to land in
  via chunk `meta`. The remaining OQ1 question (who owns surfacing locators into
  `meta`) stays with spec 02; this spec only *produces* locators.

The backends are inert until `register("pdf", PdfExtractor())` runs at import time,
replacing the stub. Nothing else in `app/` changes.

---

## 1. PDF footnote linkage — the load-bearing algorithm

This is the one piece that can silently corrupt output, so it gets a written
algorithm, a geometry API it is bound to, and a hard accuracy bar. It is scoped to
**single-column, text-layer PDFs** (non-goals: OCR, multi-column reading order).

### 1.1 Geometry source (`pypdfium2`, Apache-2.0/BSD)

The algorithm is written against pdfium's text page, **not** assumed portable to
pymupdf (see README license decision). Concretely (high-level + the one raw call
pypdfium2 doesn't wrap):

```python
import pypdfium2 as pdfium
import pypdfium2.raw as pdfium_c

pdf = pdfium.PdfDocument(raw)            # raw: bytes
page = pdf[i]
width, height = page.get_size()          # points; origin BOTTOM-left, y grows UP
tp = page.get_textpage()
n = tp.count_chars()
ch   = tp.get_text_range(j, 1)           # the glyph at index j
l,b,r,t = tp.get_charbox(j)              # char bbox in page points
size = pdfium_c.FPDFText_GetFontSize(tp.raw, j)   # font size in points (raw API)
```

Key facts that drive the heuristics:

- **Origin is bottom-left, y grows upward.** The *bottom margin* is **small** `y`.
  (Easy to invert; the fixture's running footer guards against it.)
- `get_charbox` gives per-glyph boxes; box **height** ≈ glyph cap height and
  `FPDFText_GetFontSize` gives the nominal point size. We use *both*: size for the
  body-vs-small decision, the box's vertical offset for super/subscript.
- pdfium has no "block" primitive; blocks are reconstructed (§1.2).

### 1.2 Reconstructing blocks (single-column assumption)

1. Collect `(glyph, l, b, r, t, size)` for every char on the page.
2. **Lines**: sort by descending baseline `b`; group glyphs whose baselines are
   within `±0.3 × median_size`; order each line left→right by `l`.
3. **Blocks**: group adjacent lines into a block when the vertical gap between them
   is `≤ 1.5 × median_line_height` and they share roughly the same left edge
   (single-column). A larger gap starts a new block.
4. Each block → a `Block{type, text, locator: Locator(page=i+1, bbox=[x0,y0,x1,y1])}`.
   `bbox` is the union of the block's char boxes. `type` defaults to `paragraph`;
   heading inference is §1.5.

`median_size` (the page **body** size) is the median glyph size over the page,
which is robust to a handful of small footnote/superscript glyphs.

### 1.3 Footnote *definition* detection (bottom band)

A footnote definition is a block that is **all** of:

- **Geometrically low**: its `bbox` top is below a per-page threshold
  `y_foot = footer_band(page)` — the bottom region after the last body-sized line.
  Compute it as the y of the lowest body-sized line minus a gap, not a fixed
  percentage, so it adapts to short pages.
- **Small font**: block median size `< 0.9 × median_size`.
- **Marker-led**: its text starts with a footnote marker token — a superscript or
  leading digit/symbol from the marker alphabet (`1 2 3 … ¹ ² ³ * † ‡`), captured
  by `_MARKER` (see examples). The marker is normalized (`¹ → 1`) to a key.

Contiguous small-font bottom lines are merged into one definition block per
marker. The block is emitted as `Block{type: "footnote", attrs:{marker: "1"}}`.

### 1.4 Footnote *citation* detection (superscript in body)

Within a body-sized block, a citation is a run of glyphs that is:

- **Smaller**: glyph size `< 0.8 × median_size` (or box height below the same
  ratio), and
- **Raised**: the glyph's box bottom `b` sits **above** the *block body baseline*
  by `≥ 0.2 × line_height` (true superscript, not a subscript or a small-caps
  run), and
- **Marker-shaped**: matches the marker alphabet.

The run's normalized marker is the citation key, and the enclosing block is the
citing block.

**Baseline reference (critical — interaction with §1.2 line grouping).** A real
superscript is raised by ≈0.3–0.4 × font size, which can **exceed** the
`±0.3 × median_size` baseline tolerance §1.2 uses to group glyphs into lines. The
superscript can therefore be split into its *own* one-glyph "line" (or attached to
the line above). If "raised" were measured against that isolated run's own
baseline, the test would always read `0` and the citation would be **silently
missed** — a recall failure against the 100%-recall bar. Therefore the "raised"
comparison is made against the **body baseline of the owning block** (the median
`b` of the block's body-sized glyphs), not the isolated run's baseline:

```
body_baseline = median(g.b for g in block_glyphs if g.size >= SMALL * body)
raised        = (g.b - body_baseline) >= RAISE * line_height
```

Equivalently, §1.2 may use a *looser* vertical tolerance (e.g. `≤ 0.5 × body`,
combined with horizontal adjacency) when merging a small raised glyph into the
body line it abuts, so the superscript stays in its citing line. Either approach
satisfies the constraint; the fixture's real superscripts (raised, not inline)
are the regression guard.

### 1.5 Heading inference (deferrable — README OQ3)

A block whose median size `> 1.15 × median_size` (or bold weight if exposed) and
that is short (≤ ~12 words) → `Block{type:"heading", level}` with `level` bucketed
by size tiers. **v1 may legitimately emit everything as `paragraph`** and defer
heading detection (OQ3); the footnote criterion does not depend on headings. If
emitted, `level` is advisory.

### 1.6 Linking and the orphan fallback

```
for each page:
    defs  = {marker -> footnote_block}      # §1.3
    cites = [(citing_block, marker), ...]   # §1.4
    for (block, marker) in cites:
        if marker in defs:
            relations.append(Relation("footnote_ref", block.id, defs[marker].id))
        # else: unlinkable citation -> leave the glyph in text, no relation
    # definitions with no incoming citation stay as footnote blocks with no
    # inbound footnote_ref  ==  v0 orphan-footnote behavior (trailing chunk)
```

Linkage is **within a page** for v1 (a citation on page 3 binds to a definition on
page 3). The fixture spans ≥2 pages to prove the per-page linker fires correctly
on each page; cross-page definitions (continued footnotes) are out of scope.

**No inline `[^id]` token is synthesized** into the PDF block text — the relation
is the only linkage carrier. This is exactly the case spec 02's relation-walking
chunker exists for, and the reason a token-based v0 chunker would have failed here.

### 1.7 False-positive rejection (the zero-FP bar)

The fixture plants distractors that a naive bottom-band rule would mislabel; each
must be rejected:

| Distractor | Why it looks like a footnote | Rejection rule |
| --- | --- | --- |
| **Page number** | low, small-ish, leading digit | Single short numeric token, horizontally centered or in the outer corner, **no** body block above it sharing its marker → not a footnote. |
| **Running header** | repeats every page | Same text at the same `y` across ≥ N pages (top band) → drop as boilerplate before §1.3. |
| **Running footer** | low + small (the trap) | Same text at the same low `y` across ≥ N pages → boilerplate, even though it sits in the footer band. |
| **Line numbers** | small digits down a margin | A column of numeric tokens at a near-constant `x` with regular `y` spacing → margin furniture, not citations. |

Boilerplate detection runs **across pages first** (collect text+y per page, mark
runs repeated on ≥ ⌈pages/2⌉ pages), so a repeated footer is removed before the
per-page footnote pass even sees it. The accuracy bar (README criterion) is
**100% recall on planted citations AND zero false-positive links/blocks** from
these distractors. A single-footnote toy fixture cannot exercise the cross-page
boilerplate rule and so does **not** satisfy the criterion.

### 1.8 Degradation

- **No text layer** (scanned PDF): `tp.count_chars() == 0` for all pages → emit a
  `Document` with zero/one empty block and surface a clear "no text layer"
  condition (logged warning). Not OCR (non-goal). The accepted risk is recorded
  in the README.
- **Unlinkable footnote**: orphan path (§1.6) — trailing `footnote` block, no
  inbound relation. Asserted by its own acceptance criterion.

---

## 2. DOCX — footnotes are not on the public API

`python-docx` (pure-Python, MIT) exposes paragraphs/tables but **not** footnotes.
The adapter therefore does two things:

1. **Body via the high-level API.** `DocxDocument(io.BytesIO(raw))`; map
   `paragraph.style.name` (`"Heading 1".."Heading 6"` → `heading` with `level`,
   else `paragraph`); `doc.tables` → whole-text `table` blocks.
2. **Footnotes via the zip part.** Open `word/footnotes.xml` directly (either
   `zipfile.ZipFile(io.BytesIO(raw))` or `doc.part.package`), iterate
   `w:footnote` elements under the `w:` namespace.

### 2.1 The separator pseudo-footnote trap

Word **always** writes two synthetic entries at the top of `footnotes.xml`:

```xml
<w:footnote w:type="separator" w:id="-1"> … </w:footnote>
<w:footnote w:type="continuationSeparator" w:id="0"> … </w:footnote>
```

A naive `for fn in footnotes` turns these into spurious `footnote` blocks **for
every real-world document**. Rejection rule: **skip any `w:footnote` that has a
`w:type` attribute, or whose `w:id` ≤ 0.** Real footnotes have `w:id ≥ 1` and no
`w:type`. The DOCX fixture includes both pseudo-footnotes and the test asserts
**zero** spurious blocks (acceptance criterion).

### 2.2 Resolving citations to relations

`w:footnoteReference w:id="N"` runs live in body paragraphs and are also not on
the public API. Walk each paragraph's lxml element
(`paragraph._p.findall(".//w:footnoteReference", ns)`), read `@w:id`, and emit a
`footnote_ref` from the paragraph's block to the `footnote` block whose `@w:id`
matches. These ids are **footnote-local** (independent of `endnotes.xml`), so no
cross-part id translation is needed.

### 2.3 Endnotes are out of scope (v1)

`word/endnotes.xml` is a separate part with the *same* adapter shape; v1 reads
footnotes only (README non-goal). Adding endnotes later is a second part + the
same separator filter, not a redesign.

---

## 3. XLSX — rows to records, with a cost cap

`openpyxl` (MIT). `load_workbook(io.BytesIO(raw), data_only=True, read_only=True)`:

- **`data_only=True`** returns the *cached* formula result. This has a sharp edge
  the fixture contract must honor (§5 / README OQ4): a workbook **written by
  openpyxl itself never has cached formula values** (reads as `None`); only a file
  last saved by a real spreadsheet app does. The XLSX golden fixture therefore
  uses **literal values only**, or is a committed binary saved by a real app — we
  do **not** assert formula-derived values from a script-built fixture.
- **`read_only=True`** streams rows so a wide sheet doesn't fully materialize.
- **Sheet policy (README OQ2 default):** ingest **all** sheets; first non-empty
  row of each sheet is its header; each later row → one `record` block; skip
  fully-empty rows.
- **Row cap:** `XLSX_MAX_ROWS` (config, default **10 000**) bounds embedding cost.
  Rows beyond the cap are dropped with a logged warning, not silently chunked. The
  cap is *per document* across all sheets.

Each record → `Block{type:"record", text:" | ".join(f"{h}: {v}"), attrs:{"row":
{header→value}, "sheet": ws.title}}`. The header per sheet is carried in `attrs`
(sheets can have different columns), and `chunk_records` batches them into chunks
(`batch` rows per chunk) exactly as it does for CSV today.

- **JSON-safe cell values (README OQ6 — load-bearing).** `attrs` is persisted via
  `json.dumps(c.meta)` in `app/ingest.py`. `openpyxl` returns
  `datetime.datetime`/`date`/`time` for date cells (very common), and these are
  **not** JSON-serializable — a raw one crashes ingestion at insert. The extractor
  coerces every cell value before it enters `attrs.row`:

  ```python
  def _json_safe(v):
      if v is None or isinstance(v, (str, int, float, bool)):
          return v
      if isinstance(v, (datetime.datetime, datetime.date, datetime.time)):
          return v.isoformat()
      return str(v)           # Decimal, etc. -> string, never a raw object
  ```

  The `text` field already stringifies (`f"{h}: {v}"`), so only `attrs.row` needs
  the coercion. The XLSX fixture includes a date cell and the test asserts
  `json.dumps(block.attrs)` and the end-to-end `json.dumps(chunk.meta)` both
  succeed.

---

## 4. Packaging — and why a uv *extra* is not a hiding place

The README states the rule (heavy deps out of `uv.lock`); here is the mechanism
and the **grounded correction** to PR #2's `pyproject.toml`.

### 4.1 uv writes *every* declared dependency set into the universal lock

`uv lock` resolves and records the **superset** of `[project.dependencies]`,
**all** `[project.optional-dependencies]` extras, **and** all `[dependency-groups]`
into the single `uv.lock`. `uv sync` then chooses what to *install*, but the lock
already contains everything. Consequence:

- A `[dependency-groups]` `docling` group → its whole ML tree in `uv.lock`. ✗
- **A `[project.optional-dependencies]` extra is no better**: PR #2's current
  `pyproject.toml` declares

  ```toml
  [project.optional-dependencies]
  layout = ["pymupdf>=1.24", "python-docx>=1.1", "openpyxl>=3.1"]
  ```

  Because uv locks extras too, **`pymupdf` (AGPL MuPDF) is already written into
  `uv.lock` via this extra.** That violates this spec's "no AGPL/`pymupdf` in the
  default lock" acceptance criterion *before a single line of PDF code is added.*

### 4.2 The v1 packaging change

1. Promote the three permissive default backends into core deps:

   ```toml
   [project.dependencies]
   # … existing …
   "pypdfium2>=4.30",   # PDF: Apache-2.0/BSD self-contained wheel
   "python-docx>=1.1",  # DOCX: MIT, pure-Python
   "openpyxl>=3.1",     # XLSX: MIT, pure-Python
   ```

2. **Delete the `layout` extra** (or at minimum drop `pymupdf` from it). With the
   defaults in core, the extra is redundant; keeping `pymupdf` in it re-pollutes
   the lock.
3. **`docling` and `pymupdf` live entirely outside the uv project** — a committed
   `requirements-docling.txt` (and, if anyone ever wants the AGPL PDF path,
   `requirements-pymupdf.txt`) installed out-of-band via `uv pip install -r …`,
   fronted by a `make docling` target. Never referenced from `pyproject.toml`,
   never in `uv.lock`.

The acceptance test greps the committed `uv.lock` for `docling`, `pymupdf`,
`torch`, `transformers` → must be absent; and for `pypdfium2`/`python-docx`/
`openpyxl` → must be present (see [`testing.md`](testing.md)).

---

## 5. Fixture authoring (README OQ4)

Determinism across library versions is a real risk; the contract:

| Format | How the fixture is built | Pitfall it avoids |
| --- | --- | --- |
| **PDF** | Committed binary, generated by a **pinned** builder script (`tests/fixtures/make_fixtures.py`, to be written during implementation) using reportlab/an explicit text+geometry layout, so the bottom-band/superscript geometry is known. ≥2 footnotes, citations on ≥2 pages, plus a page number, running header, and running footer as planted distractors. | Hand-drawn PDFs whose glyph geometry drifts; a toy single-footnote PDF that can't test boilerplate rejection. |
| **DOCX** | Script-built with `python-docx`, then the separator/`continuationSeparator` entries are present because Word's part template includes them — if a from-scratch `python-docx` doc omits them, the fixture **adds them explicitly** so the separator-filter test is real. | Asserting the filter against a doc that has no separators to filter. |
| **XLSX** | Script-built with **literal values only** (no formulas), because `data_only=True` would read script-written formulas as `None`. If formula-value coverage is wanted, commit a binary saved by a real spreadsheet app. | Asserting a formula result that is `None` in a script-built workbook. |

Tests assert on **structure** (block types, ids, relations, locators), not exact
whitespace, and run under **pinned** library versions, so a pdfium/openpyxl text
tweak between releases doesn't redden the gate.

---

## 6. Alternatives considered

| Decision | Chosen | Alternatives & why rejected |
| --- | --- | --- |
| **PDF library** | `pypdfium2` (Apache/BSD) | **`pymupdf`/MuPDF**: richer extraction but **AGPL-3.0/commercial** — promoting it to a default dep imposes AGPL on the whole stack and (via uv) on `uv.lock`. Allowed only behind the same out-of-band install as docling. **`pdfminer.six`**: pure-Python, permissive, but slower and its layout objects are heavier to drive than pdfium's char boxes. **`pdfplumber`**: nice API but adds pdfminer underneath. |
| **Footnote linkage carrier** | typed `footnote_ref` relation, no inline token | **Synthesize `[^id]` into PDF text**: re-introduces the v0 token convention spec 02 deliberately removed; breaks for geometry-only sources and double-counts markers. |
| **Linkage scope** | per-page (citation binds to same-page definition) | **Global by marker**: markers reset per page in many documents, causing cross-page mis-links; cross-page continued footnotes are a non-goal. |
| **docling / pymupdf packaging** | out-of-band `requirements-*.txt` + `make` target | **uv `[dependency-groups]`** *and* **uv `[project.optional-dependencies]` extra**: both are written into the universal `uv.lock` regardless of install — they do not keep the ML/AGPL stack out of the committed lock (§4). |
| **XLSX sheet policy** | all sheets, header per sheet, configurable row cap | **First sheet only**: silently drops data in multi-sheet workbooks. **No cap**: a 100k-row sheet → 100k embeddings, an unbounded cost blow-up. |
| **DOCX footnotes** | read `word/footnotes.xml` zip part directly | **python-docx public API**: does not expose footnotes — naive use silently drops every footnote. |
| **DOCX endnotes** | deferred to a later v | Same adapter shape; out of v1 scope to keep the surface small. |
| **PDF heading detection** | font-size heuristic, *deferrable to all-paragraph* | **Required headings in v1**: reading-order/heading reconstruction is brittle and a non-goal; footnote linkage doesn't need it (OQ3). |

---

## 7. Edge cases

- **Inverted y-axis.** pdfium's bottom-left origin makes "bottom margin" = *small*
  y; the running-footer distractor catches a flipped comparison.
- **Multi-glyph markers** (`12`, `†‡`). The marker regex captures a contiguous
  run; normalization joins digits before lookup.
- **Superscript that isn't a footnote** (math exponents, ordinals like `1ˢᵗ`).
  Mitigated by requiring the marker to also resolve to a bottom-band definition;
  an unresolved superscript stays inline with no relation (no false link).
- **Superscript split from its citing line by §1.2 grouping** (README OQ7). A
  raised glyph can exceed the line-baseline tolerance and become its own "line";
  measuring "raised" against that isolated baseline reads `0` and the citation is
  missed. Fixed in §1.4: raise is measured against the owning **block's body
  baseline** (or §1.2 merges abutting small raised glyphs into the body line). The
  fixture uses real (geometrically raised) superscripts so a regression to the
  naive per-line baseline fails the recall bar.
- **Footnote definition wrapping onto two lines.** §1.3 merges contiguous
  small-font bottom lines into one block before marker parsing.
- **Empty / single-row XLSX sheet.** No header or no data rows → zero `record`
  blocks for that sheet; not an error.
- **DOCX table inside a footnote** — out of scope; footnote text is flattened.
- **`schema_version` major mismatch** when an adapter reads a stored canonical
  manifest — rejected per spec 02's reader guard (this spec's producers always
  stamp the current `SCHEMA_VERSION`).

---

## 8. Config additions (proposed)

| Setting | Env var | Default | Notes |
| --- | --- | --- | --- |
| `xlsx_max_rows` | `XLSX_MAX_ROWS` | `10000` | per-document row cap across all sheets; overflow dropped with a logged warning |
| `xlsx_sheets` | `XLSX_SHEETS` | `all` | `all` (default) — reserved for `first`/named-sheet policy if OQ2 reopens |

Defaults preserve "smallest honest version": with no env set, all sheets ingest up
to 10k rows. Added to `app/config.py`'s frozen `Settings` dataclass as
`xlsx_max_rows: int = int(os.environ.get("XLSX_MAX_ROWS", "10000"))` (and an
optional `xlsx_sheets` following the existing `os.environ.get(...)` idiom in that
file — there is no separate `examples/config.py`).
