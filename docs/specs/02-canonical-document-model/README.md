---
title: Canonical document model
slug: canonical-document-model
area: ingestion
tier: Next
size: M
status: Todo
depends_on: ["PR #2"]
issue:        # set to the GitHub issue number when created
---

# Canonical document model

> **Area** `ingestion` · **Tier** `Next` · **Size** `M` · **Status** `Todo` · **Depends on:** PR #2

## Summary

Promote the implicit `LayoutDoc`/`Element` shape introduced by PR #2 into the
explicit **v1 canonical `Document`** defined in [`CANONICAL_MODEL.md`](../../CANONICAL_MODEL.md):
a closed `BlockType` enum, stable per-document block IDs, a `schema_version`,
optional `Locator` provenance, and footnotes expressed as a `footnote_ref`
**relation** instead of an inline `[^id]` token. This is a refactor of the
extraction seam — chunking, footnote linkage, and storage stop branching on
source format — landed so that PR #3's PDF backend has a real contract to emit
into. No retrieval-quality change is intended; the eval gate is the guardrail.

## Problem / Motivation

Extractors normalize to an *implicit* `LayoutDoc`/`Element` shape with a free-string `kind` and token-based footnotes -- fine for md/html, but it won't survive geometry-only backends (PDF) or third-party IRs.

## Goals

- Implement the v1 contract in `CANONICAL_MODEL.md`: closed `BlockType`, `schema_version`, stable block IDs, optional `Locator`, footnotes as a `footnote_ref` relation.

## Non-goals

- `caption_of`/`contains`/`cross_ref` as required output.
- Structured table cells; multi-modal blocks (deferred per the spec).

## Proposed design

The full schema lives in [`CANONICAL_MODEL.md`](../../CANONICAL_MODEL.md); this is
the implementation seam and migration path. Deeper notes — the relation-walk
algorithm, the two id-spaces, alternatives, and edge cases — are in
[`design.md`](design.md); illustrative code in [`examples/`](examples/); the proof
plan in [`testing.md`](testing.md).

**Architecture at a glance.**

```
   md/html/csv extractor ─┐        ┌─ (PR #3) pdf/docx extractor
        app/layout.py     │        │      app/layout.py
                          ▼        ▼
        ┌──────────────────────────────────────────────┐
        │   canonical Document  (THE SEAM, v1)          │  schema_version · source ·
        │   blocks: Block[]  ·  relations: Relation[]   │  format · meta
        └──────────────────────────────────────────────┘
              │ chunk_layout() walks footnote_ref          ▲ reject unknown major
              ▼ app/chunking.py                            │ (adapter / stored manifest)
        list[Chunk(text, meta)] ── app/ingest.py ──▶ documents(…, meta jsonb)
                                                            │
                                          app/retrieval.py UNTOUCHED (selects id, content)
```

**Components.**

| Component | Module | Change |
| --------- | ------ | ------ |
| Canonical types + enums + `SCHEMA_VERSION` + version guard | `app/layout.py` (PR #2) | **new** dataclasses/enums; enforce-at-construction |
| v0→v1 `kind` mapping | `app/layout.py` extractors | `body→paragraph`; orphan `notes`→`footnote`; raise on unmapped |
| Footnote linkage by relation-walk | `app/chunking.py` (PR #2) | replace `[^id]` token scan with `footnote_ref` walk |
| `meta` stamping on both ingest paths | `app/ingest.py` | write `schema_version`/`doc`/`format`/`section`/`footnotes`/`blocks`/`page?` |
| Retrieval / gateway / agent / `db/init.sql` | — | **untouched** |
| Eval harness | `app/evals.py` | **gate logic untouched**; one *additive* `keyword_mean` key for the AC-5 lower-variance delta (see Acceptance criteria) |

**Seam.** The canonical `Document` is the extractor↔chunker interface. Touched
modules (all introduced by PR #2): `app/layout.py` (extractors emit `Document`
instead of `LayoutDoc`), `app/chunking.py` (`chunk_layout` walks relations),
`app/ingest.py` (stamps `meta`). Retrieval, the gateway, and the agent are
untouched; the eval harness keeps its gate logic untouched and gains only one
additive `keyword_mean` key for the AC-5 lower-variance delta.

- **Types.** Add `Document`, `Block`, `Locator`, `Relation`, and the closed
  `BlockType` / `RelationType` enums (dataclasses + a `SCHEMA_VERSION = "1"`
  constant). Construction validates `type` against the enum and rejects unknowns.
- **v0→v1 mapping (in the extractors).** `body → paragraph`; `record`/`table`
  unchanged; the orphan-`notes` chunk → a `footnote` block with no inbound
  `footnote_ref`. Backends map *into* the closed set; they never invent kinds.
- **Block IDs.** Assigned per extraction pass, unique and stable *within one
  `Document` instance* (e.g. `b{index}`), which is all relations and chunk `meta`
  need. IDs are **not** promised stable across re-extraction or source edits —
  see Open questions.
- **Ordering.** The `blocks` array is the single source of truth for reading
  order. `order` is advisory/redundant and, if present, must equal the array
  index; the chunker reads the array, never re-sorts by `order`.
- **Footnotes as relations.** `chunk_layout` attaches a footnote by walking
  `footnote_ref` edges for the blocks in a chunk, not by scanning for `[^id]`.
  The marker may remain in `text` (rendering only) and the definition is still
  duplicated into every citing chunk; uncited `footnote` blocks collect into a
  trailing chunk.
- **One footnote id-space.** `footnote_ref.to_id` targets the **footnote block
  id**, and `meta.footnotes` carries those same **block ids** — *not* the v0
  `[^otel]` marker keys. (`CANONICAL_MODEL.md`'s `documents.meta` example
  previously mixed namespaces — relation `to_id: "b3"` but
  `meta.footnotes: ["otel"]`; the block-id space wins, and that example **has now
  been corrected** to `meta.footnotes: ["b8","b9"]`.) If a
  human-readable key is wanted for citation UX it goes in `attrs`, not in the id.
  This keeps relation targets and chunk `meta` in a single namespace so the
  chunker never has to translate between marker keys and block ids.
- **`schema_version` handling.** Extractors stamp `SCHEMA_VERSION`. A reader
  rejects an unknown **major** (string compare of the leading integer) rather
  than mis-parse. In v1 the only cross-version boundary is (a) a third-party IR
  adapter and (b) a stored canonical-JSON manifest read by `ingest`; the
  in-process producer/consumer share the constant, so the guard is asserted at
  those two boundaries.
- **Storage.** Per-chunk provenance is written to the existing `documents.meta`
  jsonb column (added by PR #2): `schema_version`, `doc`, `format`, `section`,
  `footnotes` (ids), `blocks` (ids), optional `page`. `meta` is jsonb so the new
  keys need **no DB migration**; rollout is a full re-ingest (`ingest()` already
  does `TRUNCATE … RESTART IDENTITY`). *Caveat:* "no migration" is true only for
  the jsonb **keys** — it assumes the `meta` **column** itself already exists from
  PR #2. `db/init.sql` is `CREATE TABLE IF NOT EXISTS` and runs once on first
  volume init, and `TRUNCATE` does not add columns, so on a *pre-existing* DB
  volume the column will be absent and inserts writing `meta` will fail. The
  operational precondition is therefore: deploy with a column already present
  (fresh volume via `docker compose down -v`, or a one-time `ALTER TABLE
  documents ADD COLUMN IF NOT EXISTS meta jsonb` shipped by PR #2). This spec
  asserts the precondition; it does not introduce a new column.

**Sequencing.** (1) Land the v1 types + closed enums + `SCHEMA_VERSION` + version
guard in `app/layout.py` with construction-time enforcement. (2) Wire the v0→v1
mapping into the md/html/csv extractors (enumerate every v0 `kind`). (3) Switch
`app/chunking.py`'s footnote attachment from the `[^id]` token scan to the
`footnote_ref` relation-walk, preserving duplication / `meta` ids / trailing
uncited chunk. (4) Stamp `meta` on both ingest paths in `app/ingest.py`. (5)
Capture the pre-refactor eval baseline, re-ingest, re-run, and gate on the
before/after delta (see Acceptance criteria / [`testing.md`](testing.md)). Each
step is independently testable; nothing downstream of the seam (retrieval,
gateway, agent) changes.

**Out of scope (deferred).** Surfacing `meta` into retrieval/generate so answers
can cite section+page — `retrieval.py` currently selects only `id, content`; the
citing UX is the governance/audit roadmap item, not this spec. Also deferred:
`caption_of`/`contains`/`cross_ref` as required output, structured table cells,
multi-modal blocks, and real `Locator` values (those land with PR #3's PDF
backend; md/html/csv omit `locator`).

## Acceptance criteria

- [ ] `Document`/`Block`/`Locator`/`Relation` types and the closed
  `BlockType`/`RelationType` enums exist; constructing a `Block` with a type
  outside the enum raises (closed-taxonomy is enforced, not advisory).
- [ ] md/html/csv extractors emit valid v1 `Document`s with the v0→v1 mapping
  applied (`body→paragraph`, orphan `notes`→`footnote` block); token-only/empty
  optional fields (`locator`, real `order`) are omitted, not faked.
- [ ] Chunker attaches footnotes by walking `footnote_ref` relations, not the
  `[^id]` token; PR #2's footnote behavior is preserved (definition duplicated
  into each citing chunk, footnote ids in chunk `meta`, uncited definitions in a
  trailing chunk). PR #2's footnote tests still pass and a new test asserts
  linkage works when the `[^id]` token is *absent* from `text`.
- [ ] `schema_version` is stamped from a single `SCHEMA_VERSION` constant and
  surfaced into `documents.meta`; the meta shape is **consistent across both
  paths** — layout-extracted chunks and back-compatible unstructured
  (`{source,content}`) corpus rows both carry `schema_version` (unstructured rows
  carry it with empty `blocks`/no `relations`). Consumers MUST treat empty
  `blocks` as "no canonical structure for this row," **not** as "a valid v1 doc
  with zero blocks": `schema_version` on a raw row asserts meta-shape
  compatibility only, not that the row was produced by the canonical extractor.
  A test asserts a raw row's `meta` is distinguishable (empty/absent `blocks`)
  from a layout chunk's.
- [ ] A reader rejects an unknown-**major** `schema_version` (raises, not
  mis-parse) at the adapter and stored-manifest boundaries; a same-major doc is
  accepted. Covered by a unit test.
- [ ] **No retrieval regression:** the eval gate (`make eval` / `app/evals.py`)
  passes after re-ingest. Note this gate guards **chunk-text equivalence only**:
  `retrieval.py` selects `id, content` and never reads `meta`, so the gate cannot
  observe `meta` at all. The only way this refactor can move the eval score is by
  changing chunk *text* (e.g. footnote attachment via relation-walk diverging from
  the v0 token-scan); `meta` correctness is therefore guarded by the unit tests
  below, **not** by the eval gate. Note also the gate as written is a *floor*
  (`mean_score >= THRESHOLD` over a 4-case golden set, half-weighted by a
  nondeterministic LLM-judge), so "passes" alone does **not** prove quality is
  unchanged — a drop
  from ~0.9 to 0.72 still passes. Therefore the criterion is a **before/after
  delta**: record the pre-refactor `mean_score` on the same corpus, re-ingest,
  re-run, and require the post-refactor mean to be within a stated tolerance
  (no worse than `baseline − 0.02`), not merely above `THRESHOLD`. **Make the delta
  lower-variance, not judge-noisy:** `app/evals.py::run()` currently returns only the
  *combined* `0.5*keyword + 0.5*judge` score per case (it stores neither a
  keyword-only mean nor the raw answer), so a clean keyword-only delta is **not
  computable from its output today**. Resolution: surface a keyword-only mean via a
  small **additive, non-behavioral** change to `run()` (a new `keyword_mean` key; the
  gate logic and `THRESHOLD` are unchanged) and gate the delta on *that* term. The
  "`app/evals.py` untouched" claim above is hereby narrowed to **"no change to the
  gate threshold or pass/fail logic"** — an additive metric is permitted and is the
  only edit to that file. The `0.02` tolerance applies to the **lower-variance
  keyword half**; the judge half is reported for context but is **not** the gate
  signal (its per-run variance over a 4-case set can exceed 0.02 even at N≥3, so
  gating on the combined mean would manufacture false regressions). **Caveat — the
  keyword half is *lower-variance*, not fully deterministic:** `keyword_score` is
  computed over `ask()`'s answer and generation (`chat()`) is called with **no
  temperature pin**, so the answer (and its keyword hits) still vary run-to-run; the
  keyword half removes only the *judge-LLM* call's variance, not the generation
  variance. So compute `keyword_mean` as the **mean over N≥3 runs** (baseline and
  post), make sure the `0.02` tolerance is wide enough to absorb the residual
  generation spread you actually observe (pin generation to `temperature=0` for the
  gate runs to shrink it if the spread approaches the tolerance), and still run N≥3
  to report judge drift. A real keyword-delta drop is a blocker, not a tuning task.
- [ ] Re-ingest is the only rollout step: `ingest()` rebuilds `documents`; no DB
  migration is required (new keys live in the existing `meta` jsonb).

## Dependencies

- PR #2

## Open questions

- **Block ID stability scope.** v1 promises IDs stable *within one extraction
  pass* only — enough for relations and chunk `meta`. Cross-re-ingest stability
  (needed if governance/audit ever wants a durable pointer to a specific block)
  would require content-derived IDs (e.g. hash of normalized text + path) and is
  **deferred** to the governance item. Accepted risk for v1: a re-ingest can
  renumber blocks; nothing downstream currently persists a block id beyond a
  single rebuild.
- **`Locator.char_start/char_end` base.** Offsets into raw bytes vs normalized
  text are undefined. Deferred: md/html/csv omit `locator`; the question is
  settled by PR #3 (PDF) when there is a real producer. Decision recorded there.
- **`schema_version` validation trigger.** v1 has no real multi-version corpus,
  so the reject-unknown-major guard is exercised only by tests until a stored
  manifest or third-party adapter exists. Accepted as a forward-compat assertion.
- **PR #2 modules do not yet exist in the tree (verified).** As of this writing
  the repo has **no** `app/layout.py`, **no** `app/chunking.py`, **no** `meta`
  column in `db/init.sql`, and `app/ingest.py` ingests a flat `{source,content}`
  corpus into `(source, content, embedding)` with no chunking/footnotes. The
  claims this spec makes about *current* code are accurate (`ingest()` does
  `TRUNCATE … RESTART IDENTITY`; `retrieval.py` selects only `id, content`), but
  every `LayoutDoc`/`Element`/footnote/`meta`-writing detail this spec refactors
  lands in PR #2 and is therefore **described, not observed**. Accepted risk:
  PR #2's merged shape may differ from the description here. *Mitigation (binding):*
  before implementation starts, re-read PR #2's merged `layout.py`/`chunking.py`/
  `ingest.py` and re-baseline this spec's seam list, v0→v1 mapping table, and
  `meta` key set against what actually shipped. If the v0 `kind` set or the flat
  vs. layout ingest branching differs from the assumptions above, revise this spec
  first.

## Risks & mitigations

- **Silent retrieval regression** (the refactor changes chunk boundaries/meta).
  *Mitigation:* the eval gate must pass post-re-ingest **as a before/after delta,
  not just an absolute floor** — the gate is `mean >= 0.7` over only 4 cases with
  a nondeterministic judge, so an absolute pass can hide a real quality drop (see
  the No-retrieval-regression acceptance criterion). Gate the delta on the
  **lower-variance `keyword_mean`** (an additive return key added to
  `app/evals.py::run()`; it removes the judge-LLM call's variance but **not**
  `ask()`'s unpinned generation variance, so average it over N≥3 — it is not
  bit-identical across runs; the combined `mean_score` is judge-noisy and its
  per-run variance over 4 cases can exceed the 0.02 tolerance, so it is reported for
  context but is **not** the gate). Capture the pre-refactor `keyword_mean` and
  block on a regression beyond tolerance; treat any drop as a blocker, not a
  tuning task. *Residual risk:* a 4-case golden set is too small to detect a
  localized regression on a topic it doesn't cover — widening the golden set is
  the `06-eval-set-maturity` spec's job, not this one (accepted for v1).
- **Blocked on PR #2.** All touched modules (`layout.py`, `chunking.py`, the
  `meta`-writing `ingest.py`) and the footnote tests ship in PR #2, which is *in
  review*, not merged. *Mitigation:* declared in `depends_on`; design/examples/
  tests for this spec can be written in parallel, but implementation starts only
  after PR #2 merges. If PR #2's shape changes in review, re-baseline this spec.
- **Closed enum breaks an unmapped v0 kind.** If a backend produced a `kind` not
  in the mapping table, construction now raises. *Mitigation:* enumerate every v0
  `kind` in a mapping test; fail loudly at extraction, not silently downstream.
- **Meta-shape drift between paths** (layout vs unstructured corpus rows).
  *Mitigation:* single `SCHEMA_VERSION` constant + a test asserting both paths
  produce a `meta` containing `schema_version`.
- **Re-ingest read-availability gap.** `ingest()` does `TRUNCATE … RESTART
  IDENTITY` then re-inserts, so `documents` is empty between truncate and the last
  insert; concurrent retrieval returns nothing during that window. This is
  pre-existing v0 behavior, not introduced here. *Accepted for v1:* the sandbox
  re-ingests offline; a zero-downtime swap (ingest into a shadow table, then
  atomically rename) is deferred and out of scope.
- **Scope creep into retrieval/generate.** The provenance-citing payoff tempts a
  retrieval change. *Mitigation:* explicitly out of scope (see Proposed design);
  this spec stops at writing `meta`.

## Test & rollout plan

**Verification**

- *Unit:* enum rejects unknown `BlockType`; v0→v1 mapping for every v0 `kind`;
  `footnote_ref` linkage with the `[^id]` token removed from `text`;
  `footnote_ref.to_id` and `meta.footnotes` use the same block-id namespace (no
  marker-key leakage); orphan `notes`→`footnote` block with no inbound ref;
  reject unknown-major `schema_version`; both ingest paths stamp `schema_version`
  into `meta` and a raw row is distinguishable from a layout chunk (empty
  `blocks`).
- *Regression:* re-run PR #2's existing footnote/layout/chunking tests unchanged.
- *Integration / gate:* capture the **pre-refactor** `mean_score` first (run
  `make eval` on `main` **with PR #2 already merged** — i.e. the seam this spec
  refactors, not pre-PR-#2 `main` — immediately before starting this refactor),
  then `make ingest` + `make eval` after, and require `post >= pre − 0.02` (not
  merely `>= THRESHOLD`); this is the
  evidence that the refactor preserved retrieval quality. Run the eval N≥3× to
  bound LLM-judge variance. Wire it into the CI eval gate (PR #3) so a regression
  blocks merge.

**Rollout**

- Not feature-flagged: it is an internal extraction-seam refactor with no API or
  config surface. Ship behind the normal eval-gated PR.
- Migration: **none** — new keys are additive in the existing `documents.meta`
  jsonb. The only operational step is a re-ingest (`ingest()` truncates and
  rebuilds), which is the existing, idempotent ingestion path.
- Rollback: revert the PR and re-ingest; no schema or data-shape lock-in.

## References

- [Feature roadmap](../../ROADMAP.md)
- [Specs index](../README.md)
- [Canonical document model schema](../../CANONICAL_MODEL.md)
- [Design notes](design.md) · [Examples (illustrative)](examples/README.md) · [Testing plan](testing.md)
- Depends on: PR #2 (hybrid ingestion). Downstream: [03-real-layout-backends](../03-real-layout-backends/README.md) (first real `Locator` producer), [05-retrieval-uses-chunk-metadata](../05-retrieval-uses-chunk-metadata/README.md) and [15-governance-and-audit](../15-governance-and-audit/README.md) (consume `meta`), [18-multi-modal-ingestion](../18-multi-modal-ingestion/README.md). CI gate: [07-ci-hardening](../07-ci-hardening/README.md).
