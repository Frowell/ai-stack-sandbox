# Canonical document model — test & verification plan

> Companion to [`README.md`](README.md) and [`design.md`](design.md). This is how
> **each acceptance criterion is proven** and how the work **gates merge**.
> Illustrative tests live in [`examples/example_tests.py`](examples/example_tests.py)
> (named so pytest does not collect it from `docs/`); port them to
> `tests/test_canonical_model.py` when implementing.

## How this project gates merge (the existing harness)

The repo's merge gate is **the eval suite run under pytest**:

- `tests/test_evals.py::test_quality_gate` calls `app.evals.run()` and asserts
  `report["passed"]` (i.e. `mean_score >= THRESHOLD`, `THRESHOLD = 0.7`).
- `make test` → `uv run pytest -q`; `make eval` → `uv run python -m app.evals`
  (non-zero exit on regression).
- CI: `.github/workflows/ci.yml` **does not exist in the tree yet** — it lands
  with PR #3 (a `lint` job + a secret-gated `eval-gate` job that stands up
  Postgres + the gateway and runs the eval gate), and is hardened by
  [07-ci-hardening](../07-ci-hardening/README.md). This spec's new unit tests run
  in the same `pytest` invocation that the `eval-gate` job already calls, so they
  gate merge **for free** once that workflow exists. Until then they gate locally
  via `make test`.

This feature adds a `tests/test_canonical_model.py` of **offline, deterministic**
unit tests (no network: the canonical types, the v0→v1 mapping, the chunker's
relation-walk, and the version guard are all pure-Python), plus a
**before/after eval delta** check for the no-regression criterion that needs the
live stack.

## Traceability: every acceptance criterion → its proof

| # | Acceptance criterion (README) | Proven by | Kind |
| - | ----------------------------- | --------- | ---- |
| AC-1 | Types + closed `BlockType`/`RelationType` exist; constructing a `Block` outside the enum **raises** | `test_block_rejects_type_outside_enum` | unit |
| AC-2 | md/html/csv emit valid v1 docs with v0→v1 mapping (`body→paragraph`, orphan `notes`→`footnote`); optional fields omitted not faked | `test_v0_kind_mapping` (every *table* kind), `test_v0_unmapped_kind_raises`, `test_orphan_notes_maps_to_footnote_block` (the `notes` case is special-cased in the extractor, **not** in the mapping table), `test_markdown_block_omits_locator_and_order` | unit |
| AC-3a | Chunker attaches footnotes by **walking `footnote_ref`**, not the `[^id]` token; works with the token **absent** from `text` | `test_footnote_linked_by_relation_without_token` | unit (new, key) |
| AC-3b | PR #2 footnote behavior preserved (definition duplicated per citing chunk, ids in `meta`, uncited → trailing chunk) | `test_uncited_footnote_goes_to_trailing_chunk` + **PR #2's existing footnote/layout/chunking tests re-run unchanged** | unit + regression |
| AC-3c | One footnote id-space: `footnote_ref.to_id` and `meta.footnotes` are the same **block-id** namespace (no marker-key leakage); sample JSON corrected | `test_single_footnote_id_space`, `test_sample_json_uses_block_id_space` | unit |
| AC-4a | `schema_version` stamped from a single `SCHEMA_VERSION` constant and surfaced into `documents.meta` | `test_document_stamps_schema_version` + the ingest meta assertions | unit |
| AC-4b | Meta shape consistent across **both** ingest paths; a raw `{source,content}` row is **distinguishable** (empty `blocks`) from a layout chunk | `test_raw_row_meta_distinguishable_from_layout_chunk` | unit |
| AC-4c | Reader **rejects unknown-major** `schema_version` at adapter/manifest boundaries; same-major accepted | `test_version_guard` | unit |
| AC-5 | **No retrieval regression** as a before/after **delta** (`post >= baseline − 0.02`), not just `>= THRESHOLD`; N≥3 runs to bound judge variance | the eval before/after procedure below (integration / gate) | integration |
| AC-6 | Re-ingest is the only rollout step; **no DB migration** | `test_no_db_migration_in_diff` (asserts `db/init.sql` unchanged in the PR diff) + the meta-column precondition documented | unit / review |

## Fixtures needed

- **Mapping fixture:** the full set of PR #2 v0 `kind` strings
  (`heading`, `body`, `table`, `record`) parametrized in `test_v0_kind_mapping`,
  so a backend that emits an unmapped kind fails at the mapping boundary.
- **Footnote docs:** small in-memory `Document`s — (a) cited footnote **without**
  an inline `[^id]` token, (b) one footnote cited from two chunks, (c) an
  uncited footnote. No corpus file needed; these are constructed in-test.
- **Sample JSON:** [`examples/canonical_document.sample.json`](examples/canonical_document.sample.json)
  is the round-trip/id-space regression fixture.
- **Eval baseline (AC-5):** a recorded pre-refactor `mean_score` on the **same
  corpus** with PR #2 already merged (see below). No new golden cases — widening
  the 4-case set is [06-eval-set-maturity](../06-eval-set-maturity/README.md)'s job.

## The no-regression eval gate (AC-5) — the load-bearing nuance

`app/retrieval.py` selects only `id, content` and **never reads `meta`**, so the
eval gate **cannot observe `meta` at all**. Two consequences:

1. **`meta` correctness is guarded by the unit tests above, not by the eval
   gate.** Do not claim the eval gate proves the provenance is right; it can't see
   it.
2. The **only** way this refactor can move the eval score is by changing chunk
   *text* — e.g. footnote attachment via relation-walk diverging from the v0
   token-scan. That is exactly what AC-5 watches.

The gate as written is a **floor** (`mean_score >= 0.7` over only 4 golden cases,
half-weighted by a nondeterministic LLM judge). A floor pass can hide a real drop
(0.92 → 0.72 still "passes"). So AC-5 is a **before/after delta**, not an absolute
floor:

```bash
# 0. PRECONDITION: PR #2 merged (the seam this spec refactors) + a meta column
#    present (fresh volume: `make down` then `make up`, or PR #2's ALTER TABLE).
# 1. BASELINE — on the seam this spec refactors (PR #2 merged), BEFORE the change:
make ingest
for i in 1 2 3; do make eval; done        # N>=3: ask() generation is UNPINNED, so even
                                          # keyword_mean varies run-to-run; average it.
#    record the MEAN keyword_mean over the N runs -> BASELINE_KW
#    (lower-variance than the combined mean, but NOT bit-identical; combined mean is context only)

# 2. Apply the canonical-model refactor, then:
make ingest                                # re-ingest IS the rollout (TRUNCATE + rebuild)
for i in 1 2 3; do make eval; done
#    POST_KW = mean keyword_mean over the N runs (lower-variance than combined;
#    NOT identical across runs — ask()->chat() has no temperature pin)

# 3. GATE: require  POST_KW >= BASELINE_KW - 0.02   (not merely combined >= THRESHOLD,
#    and NOT the judge-noisy combined mean). A real keyword drop is a BLOCKER.
```

**Surfacing the lower-variance signal (required).** `app/evals.py::run()` today
returns only the *combined* `0.5*keyword + 0.5*judge` score per case and a combined
`mean_score`; it exposes **no** keyword-only mean and stores **no** raw answer, so
the keyword-only delta cannot be computed from its output as-is. Before running the
gate, add an **additive, non-behavioral** `keyword_mean` key to `run()`'s return
(the `keyword_score` term is already computed at line 51; just accumulate and
report it). The gate `THRESHOLD` and pass/fail logic are unchanged — this is the
*only* permitted edit to `evals.py` and is the narrowed sense of "evals untouched."
Gate AC-5 on `keyword_mean` (`post >= baseline_keyword_mean − 0.02`). **It is
*lower-variance*, not fully deterministic** — and this distinction is load-bearing.
`keyword_score` is computed over `ask()`'s generated answer, and `ask()`→`chat()`
is called with **no temperature pin** (gateway default), so the answer — and thus
which keywords it contains — still varies run-to-run. The keyword half removes only
the *judge-LLM* call's variance, **not** the answer-generation variance. Therefore:
(a) compute `keyword_mean` as the **mean over the N≥3 runs** for both baseline and
post; (b) ensure the `0.02` tolerance is wide enough to absorb the residual
generation variance you actually observe across the N runs (if the N-run spread of
`keyword_mean` already approaches 0.02, the tolerance is too tight — pin generation
to `temperature=0` for the gate runs to shrink it, or widen the tolerance with that
spread recorded). Report the combined/judge mean for context only — do **not**
gate on it: the judge half adds *further* per-run variance that over a 4-case set
can exceed 0.02 even at N≥3, so gating the combined mean would manufacture false
regressions.
*Residual risk (accepted for v1):* a 4-case golden set can miss a localized
regression on an uncovered topic (widening it is
[06-eval-set-maturity](../06-eval-set-maturity/README.md)'s job).

## A concrete test in the project's idiom

`tests/test_evals.py` is two lines: call `run()`, assert `passed`. The new file
follows the same minimalist, import-the-real-thing idiom — here is the keystone
AC-3a test (full set in [`examples/example_tests.py`](examples/example_tests.py)):

```python
# tests/test_canonical_model.py
from app.chunking import chunk_layout
from app.layout import Block, BlockType, Document, Relation, RelationType


def test_footnote_linked_by_relation_without_token():
    """AC-3a: linkage survives when the [^id] token is absent (PDF-by-geometry)."""
    doc = Document(
        source="d",
        format="markdown",
        blocks=[
            Block(id="b1", type=BlockType.PARAGRAPH, text="body with no inline marker"),
            Block(id="b2", type=BlockType.FOOTNOTE, text="the definition"),
        ],
        relations=[Relation(type=RelationType.FOOTNOTE_REF, from_id="b1", to_id="b2")],
    )
    cite = chunk_layout(doc)[0]
    assert "the definition" in cite.text     # duplicated into the citing chunk
    assert cite.meta["footnotes"] == ["b2"]  # by BLOCK ID, one id-space
```

## What is NOT tested here (honest scope)

- **`meta` surfaced into retrieval/generate / citation UX** — out of scope; see
  [05-retrieval-uses-chunk-metadata](../05-retrieval-uses-chunk-metadata/README.md)
  and [15-governance-and-audit](../15-governance-and-audit/README.md).
- **Real `Locator` values / cross-re-ingest block-id stability** — no producer in
  v1; arrives with [03-real-layout-backends](../03-real-layout-backends/README.md).
- **The version guard against a real multi-version corpus** — there is none in
  v1; the guard is exercised only by `test_version_guard` until a stored manifest
  or third-party adapter exists (accepted forward-compat assertion).
