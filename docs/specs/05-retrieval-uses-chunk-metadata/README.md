---
title: Retrieval uses chunk metadata
slug: retrieval-uses-chunk-metadata
area: retrieval
tier: Next
size: S
status: Todo
depends_on: [PR #2]
issue:        # set to the GitHub issue number when created
---

# Retrieval uses chunk metadata

> **Area** `retrieval` · **Tier** `Next` · **Size** `S` · **Status** `Todo` · **Depends on:** PR #2

## Summary

Retrieval currently returns only `(id, content)` (`app/retrieval.py: retrieve()`
→ `list[tuple[int, str]]`), so the generate node can attribute an answer to a
chunk id but cannot tell the reader *where in the source* that chunk came from.
PR #2 (hybrid ingestion) adds a `meta JSONB` column to `documents` and populates
it per chunk. Per `CANONICAL_MODEL.md` v1 the chunk-level `meta` keys are
`schema_version`, `doc`, `format`, `section` (breadcrumb), `footnotes`, `blocks`,
and an optional `page` (note: `columns` is **not** a chunk-`meta` key — it is a
table block's `attrs.columns`; this spec only reads `section` and `page`). This spec
threads that `meta` through the retrieval seam into the generate node so answers
can cite human-readable provenance (section/breadcrumb) in addition to the `[id]`
convention — without regressing the existing eval gate.

## Problem / Motivation

Chunks carry `meta` once PR #2 lands, but retrieval's SQL (`SELECT id, content`
in `dense()` / `sparse()`) and its return type drop it on the floor, and the RRF
fusion / rerank hook / `State.context` / `generate_node` all only carry
`(id, content)`. The generate node therefore can only emit bare `[id]` citations
— opaque to a human reader and useless for "jump to the cited section." We want
the answer to say *"…(see "Mature AI Stacks > Observability")"* using the
`section` breadcrumb the ingester already computes.

## Goals

- Thread chunk `meta` through the full retrieval path
  (`dense`/`sparse` → `rrf` → `rerank` → `retrieve` → `State.context` →
  `generate_node`) without losing it at any seam.
- Make the generate node cite human-readable provenance (`section` breadcrumb,
  and `page` *only if/when* a producer emits it) alongside the existing `[id]`.
- Keep the existing `[id]` citation convention and the eval gate green.

## Non-goals

- Re-ranking or scoring by metadata (rerank stays identity / RRF order).
- **Metadata-based filtering** (`WHERE meta @> …` on format/section) — deferred;
  see Open questions. Keeps this an S change behind the existing seam.
- Adding a `page` field to the data. `page` lives in the canonical `Locator`
  (CANONICAL_MODEL.md v1) and is only produced once the PDF backend exists
  (spec 03). This spec consumes `page` defensively if present but does not
  produce it.
- UI / frontend changes.

## Proposed design

The seam is `app/retrieval.py` (producer) and `app/agent.py` (consumer). The
whole change is "carry one more field through tuples that already flow."

```
                  app/retrieval.py                         app/agent.py
  ┌─────────────────────────────────────────────┐   ┌───────────────────────────┐
  │ dense()  ─┐                                  │   │ retrieve_node             │
  │           ├─► rrf() ─► rerank() ─► retrieve()│──►│   State.context           │
  │ sparse() ─┘                                  │   │     :list[RetrievedChunk] │
  └─────────────────────────────────────────────┘   │        │                  │
       SELECT id, content, meta                       │        ▼                  │
       r[2] or {}  (NULL→{} at SQL boundary)          │  render_context(chunks)   │ ◄── pure, unit-tested
                                                      │        │                  │
   carrier today:  tuple[int, str]                    │        ▼                  │
   carrier after:  RetrievedChunk(id, content, meta)  │  generate_node ─► chat()  │
                                                      └───────────────────────────┘
```

Every arrow above carries `meta` after this change; today it stops at the SQL
`SELECT` and is never selected at all. The only *new* code is the
`RetrievedChunk` type and the extracted, importable `render_context()` helper —
everything else is widening an existing 2-tuple to a 3-field carrier.

**1. Carrier type.** Replace the `tuple[int, str]` carrier with a typed
`RetrievedChunk` (a `@dataclass` or `NamedTuple` of `id: int, content: str,
meta: dict`) declared in `app/retrieval.py` and re-used by `app/agent.py`'s
`State.context`. A NamedTuple keeps tuple-unpacking call sites working; a
dataclass is clearer. Pick one and use it everywhere — do **not** leave mixed
2-tuples and 3-tuples in the pipeline.

**2. SQL.** `dense()` and `sparse()` select `id, content, meta` and **coerce a
NULL `meta` to `{}` at the row boundary** (`r[2] or {}`). Do **not** assume the
column is `NOT NULL`: PR #2 / spec 02 adds `meta` as a *plain nullable* `jsonb`
(the one-time migration is `ALTER TABLE documents ADD COLUMN IF NOT EXISTS meta
jsonb`, no `DEFAULT`), and any row not stamped by the canonical ingester — a
pre-re-ingest row, or a plain `corpus.jsonl` row if PR #2's ingest does not stamp
`meta` on unstructured rows — will have SQL `NULL`, which psycopg3 decodes to
Python `None`. Coercing once at the SQL boundary guarantees `RetrievedChunk.meta`
is always a real dict downstream, so `rrf`/`render_context` never call `.get()`
on `None`.

**3. Fusion.** `rrf()` currently keys scores by id and stashes `text[doc_id]`.
Add a parallel `meta[doc_id]` map (dense and sparse return the same `meta` for a
given id, so last-writer-wins is fine) and emit it in the fused tuples.

**4. Rerank.** `rerank()` signature changes to pass `RetrievedChunk` through
unchanged (still identity / `candidates[:top_n]`).

**5. Generate.** Extract the context rendering into a **pure, importable helper**
`render_context(chunks: list[RetrievedChunk]) -> str` in `app/agent.py` (today the
join is inlined in `generate_node`, which also calls `chat()` and so cannot be
unit-tested without the LLM). `generate_node` calls the helper and passes the
result to `chat()`. The helper builds each context line as
`[{id}] ({provenance}) {content}` where `provenance` is derived from meta:
`meta.get("section")` (by **truthiness** — a missing key *and* an empty/`None`
value are both skipped), optionally `"p.{meta['page']}"` if a `page` key is
truthy, joined (deterministic order: **section first, then page** —
`(section, p.N)` — when both exist) and **omitted entirely when neither exists**
(so the existing unstructured corpus, whose chunks only have `{"doc": …}`,
renders exactly as today — when no displayable key is present the line is exactly
`[{id}] {content}`, no leading `()`). The helper must also reproduce the existing
`"\n\n".join(...)` separator between lines, so the no-section path is
byte-identical at the **whole-context** level, not just per line — otherwise even
the all-unstructured prompt shifts and can move eval scores. The helper guards
`meta or {}` defensively even though the SQL boundary already coerces, so a
hand-built `RetrievedChunk(meta=None)` in a unit test cannot raise. Update the
system prompt to: keep citing `[id]`, and additionally name the section/page in
prose when the context provides it.

> psycopg3 decodes a `jsonb` column to a Python `dict` automatically (default
> json loader) — **except** a SQL `NULL`, which decodes to Python `None`, not
> `{}`. Because PR #2 adds `meta` as a nullable column (see SQL note above),
> `RetrievedChunk.meta` is a real dict only after the `r[2] or {}` coercion; with
> that coercion there is no `json.loads` and no type registration needed.

**Schema / config / API changes.** None beyond consuming the `meta` column PR #2
adds. No migration owned by this spec. `retrieve()`'s public return type changes
(2-tuple → `RetrievedChunk`); update every caller (`agent.py`, any test/eval).

### Call sites that must change (exhaustive)

- `app/retrieval.py`: `dense`, `sparse`, `rrf`, `rerank`, `retrieve`, new
  `RetrievedChunk` type.
- `app/agent.py`: `State.context` type, `retrieve_node` (no logic change), new
  pure `render_context()` helper, `generate_node` (calls helper + updated system
  prompt).
- `tests/` and `app/evals.py` callers if any unpack the 2-tuple directly. Note
  `app/ingest.py` only loads `corpus.jsonl` today; the section-citation golden
  case needs `sample.md` ingested too (see Open questions / Acceptance criteria).

### Sequencing (land in this order)

The change is small but type-rippling; sequence it so the suite stays green at
each step rather than landing a half-threaded pipeline:

1. **Carrier + SQL, no behavior change.** Add `RetrievedChunk` and thread it
   through `dense`/`sparse`/`rrf`/`rerank`/`retrieve`, coercing `r[2] or {}`.
   `render_context` does not exist yet; `generate_node` still inlines its join
   but now unpacks `c.id, c.content` from the carrier. The eval gate must be
   byte-identical here (nothing reads `meta` yet) — this isolates the type change
   from the prompt change. **Capture the per-case eval scores here as the baseline
   artifact** (run with `temperature=0` if used) that steps 3–4 diff against.
2. **Extract `render_context()` + unit-test it.** Pull the inline join out of
   `generate_node` into the pure helper, keep output byte-identical for
   no-displayable-key meta, and land the deterministic unit tests (the primary
   proof — criterion 2). Still no prompt/provenance change.
3. **Turn on provenance + prompt.** Make `render_context` emit `(section[, p.N])`
   and update the system prompt to name the section in prose. Run the eval gate
   and diff per-case scores against the step-1 baseline.
4. **Add the golden case + its fixture.** Only after `sample.md` is confirmed
   ingested into the eval environment (see Open questions). This step can only
   land once PR #2 is on `main`.

Steps 1–2 are pure refactors guarded by the existing gate; steps 3–4 are the
behavior change and are where the per-case regression diff (Risks) matters. See
[`design.md`](design.md) for the alternatives behind each choice and
[`testing.md`](testing.md) for how each acceptance criterion is proven and gated.

## Acceptance criteria

- [ ] `retrieve()` returns a typed carrier (`RetrievedChunk`) with `id`,
      `content`, and `meta`; `dense`/`sparse`/`rrf`/`rerank` all carry `meta`
      end to end (no 2-tuple survives in the pipeline).
- [ ] **(primary feature proof — deterministic, no LLM)** A unit test on the pure
      `render_context()` helper asserts: (a) a chunk with `meta["section"]="A > B"`
      produces a line containing `(A > B)`; (b) a chunk whose meta has no
      displayable key (`{"doc": …}`) **or only falsy values** (e.g.
      `{"section": ""}`, treated by truthiness, not key-presence) produces a line
      **byte-identical** to today's
      `[{id}] {content}` (no `()`), and a list of such chunks renders
      byte-identical to the old `"\n\n".join(...)` output at the whole-context
      level; (c) a chunk with a `page` key produces `p.{n}` and a chunk
      **without** one emits no `(p.)` and does not raise; (d) a chunk whose
      `meta` is `None` (NULL in the DB) renders exactly like the no-key case and
      does **not** raise `AttributeError`. This is the
      load-bearing proof; the golden case below is a softer end-to-end smoke check
      whose pass/fail depends on a non-deterministic model.
- [ ] The existing `[{id}]` citation convention still works and the system
      prompt still instructs `[id]` citation.
- [ ] The eval gate (`python -m app.evals`, `THRESHOLD = 0.7`) passes on the
      existing `evals/golden.jsonl`. The gate enforces only the **mean** score, so
      regression is checked manually/in-CI by diffing **per-case** scores
      before/after (see Risks) — no single existing case may drop. **The
      `render_context()` unit test (criterion above) is the authoritative
      regression proof; the eval per-case diff is advisory because the eval path
      is non-deterministic** (the app/`gateway.chat()` calls pass no
      `temperature`/`seed`, so scores vary run-to-run). To make the diff
      meaningful, capture a baseline at step 1 (below) and run the eval path with
      `temperature=0` for the diff (pass it via `chat(..., temperature=0)` on the
      eval path, or accept the diff as advisory-only and lean on the unit test).
- [ ] A new golden case in `evals/golden.jsonl` exercises section citation
      end-to-end. Two prerequisites must be wired or the case cannot pass:
      (1) **the eval environment must contain the ingested fixture** — `data/sample.md`
      must be loaded *with its `meta`* into `documents` before evals run (today
      `app/ingest.py` truncates and loads only `data/corpus.jsonl`, and the eval
      harness does not ingest; add `sample.md` to the ingest set or to the
      eval/CI setup so its section chunks exist at query time); (2) **the keyword
      must actually prove section citation** — do **not** use `Observability`
      (it already appears in `corpus.jsonl` and in an existing golden case, so the
      answer would match for the wrong reason). Pick a section whose breadcrumb
      leaf is distinctive to `sample.md` and absent from `corpus.jsonl`, and put
      that leaf string in the case's `keywords`. (The harness only scores
      `keyword`/`judge` against `golden.jsonl` fields — there is no per-case custom
      assertion hook, so the proof must be encoded as a keyword.)

## Dependencies

- **PR #2 (hybrid ingestion)** — hard prerequisite. Provides the `meta JSONB`
  column (`db/init.sql`), the chunk-level ingester, the `meta` keys
  (`schema_version`/`doc`/`format`/`section`/`footnotes`/`blocks`/`page?` per
  `CANONICAL_MODEL.md`), and the `data/sample.md` fixture.
  This spec cannot start until #2 is on `main`; nothing in `meta` exists before
  then (today's schema is `id, source, content, embedding, fts`).

## Open questions

- **Metadata filtering** (`retrieve(query, format=…, section=…)` →
  `WHERE meta @> %s::jsonb`, served by the `documents_meta_idx` GIN index PR #2
  adds). Genuinely useful but expands the public `retrieve()` API and needs its
  own eval cases. *Decision: defer to a follow-up; not in this S spec.*
- **Provenance render format.** `[{id}] ({section})` vs a structured trailer.
  *Leaning:* inline parenthetical, because it survives the existing prompt with
  the least churn. Revisit if the judge penalizes verbosity.
- **Where does `sample.md` get ingested for the eval run?** Cleanest is for PR #2
  to add `sample.md` to the default ingest set so `make ingest` (and the CI
  eval-gate's setup) loads it alongside `corpus.jsonl`. If #2 does not, this spec
  must own a small eval-setup step that ingests it. *Decision: prefer #2 owns it;
  confirm with #2's owner before implementation, else add the step here.*

## Risks & mitigations

- **Return-type change ripples / partial threading.** Changing `retrieve()`'s
  return type can leave a stale 2-tuple unpack somewhere (e.g. in `rrf` or a
  test) that fails only at runtime. *Mitigation:* one carrier type used at every
  site (see exhaustive list); rely on the unit + eval suite to catch a missed
  site; prefer `NamedTuple` so positional unpacking keeps working.
- **Eval-gate regression from prompt/format change.** Adding provenance to the
  context and prompt can shift the judge/keyword scores below 0.7.
  *Mitigation:* the no-section path is byte-identical to today (criterion above);
  run the gate before/after and diff per-case scores, not just the mean.
- **Non-deterministic regression diff.** The before/after per-case diff is the
  stated regression check, but `gateway.chat()` passes no `temperature`/`seed`
  (verified: no temperature/seed anywhere in `app/` or `gateway/`), so two runs of
  the *same* prompt differ — the diff conflates the prompt change with sampling
  noise, which can both hide a real per-case drop and false-alarm on noise.
  *Mitigation:* the deterministic `render_context()` unit test is the authoritative
  proof (criteria a–d). For the eval diff, run the eval path with `temperature=0`
  (or N repeats and compare means), and capture the step-1 baseline scores as a
  committed/CI artifact so the after-change run diffs against a fixed reference
  rather than a fresh, equally-noisy baseline. Treat the diff as advisory, not a
  hard gate (per-case floors are spec 06).
- **Gate granularity hides per-case drops.** `app/evals.py: run()` fails only on
  `mean < THRESHOLD`; a single existing case can regress materially and still pass
  under a healthy mean. *Mitigation:* the before/after per-case diff above is the
  real regression check and is **manual / a CI step**, not enforced by the gate
  itself. (Tightening the gate to per-case floors is spec 06's job, not this S
  change — out of scope here.)
- **Brittle golden-case keyword.** Substring keyword scoring (`k in answer`) plus
  a non-deterministic model means a poorly chosen section keyword either flakes or
  passes for the wrong reason. *Mitigation:* the deterministic `render_context()`
  unit test is the primary proof; the golden keyword must be a section leaf unique
  to `sample.md` (criterion above).
- **Empty / heterogeneous / NULL meta.** Record/table chunks (no `section`),
  `notes` chunks
  (`section: "uncited footnotes"`), plain corpus rows (`{"doc": …}` only), **and
  rows whose `meta` is SQL `NULL`** (psycopg3 → `None`) all flow through the same
  renderer. A `None` meta is the dangerous case: `None.get("section")` raises
  `AttributeError` and crashes `generate_node` on the live query path, not just a
  test. *Mitigation:* coerce `NULL → {}` once at the SQL boundary (`r[2] or {}`),
  guard `meta or {}` again in `render_context`, derive provenance with `.get()`,
  omit the parenthetical when no displayable key is present, and never index
  `meta["page"]` unguarded. Criterion (d) above pins the `None` case.

## Accepted risks

- We consume `meta` shape as PR #2 / `CANONICAL_MODEL.md` v1 defines it
  (chunk keys `schema_version`, `doc`, `format`, `section`, `footnotes`, `blocks`,
  optional `page`). This spec only **reads** `section` and `page`. Per the
  canonical model `section` is a **string** breadcrumb (e.g.
  `"Mature Stacks > Observability"`) and `page` is an **int**; `render_context`
  assumes those types and `str()`-coerces defensively (so a list-valued `section`
  from a future producer renders without raising, even if not pretty). If PR #2's
  keys or the `section`/`page` value types change before merge, this spec's
  renderer and the golden case must follow. Owner of #2 to flag key renames.
- We consume `meta` as a **nullable** column (PR #2's migration is `ADD COLUMN IF
  NOT EXISTS meta jsonb`, no `NOT NULL`/`DEFAULT`), so this spec owns the
  `NULL → {}` coercion rather than relying on a DB constraint. Accepted: if a
  later spec tightens the column to `NOT NULL DEFAULT '{}'` the coercion becomes
  belt-and-suspenders, which is fine. We do **not** add that constraint here (it
  would be a migration this S spec is scoped to avoid, and spec 02 already treats
  the column as nullable).
- `page` citation is shipped as a forward-compatible no-op; it is genuinely
  untestable until spec 03 (PDF backend) produces a `page`/`Locator`. Accepted:
  the criterion verifies the *no-op*, deferring real page-citation coverage to 03.

## Test & rollout plan

- **Unit:** assert `dense`/`sparse`/`rrf`/`rerank`/`retrieve` carry `meta`;
  assert the **pure `render_context()` helper** (extracted out of `generate_node`
  so no LLM call is needed) (a) includes the section breadcrumb when
  `meta["section"]` is set, (b) is byte-identical to the old `[{id}] {content}`
  format (including the `"\n\n"` join across a list) when meta has no displayable
  key, (c) does not emit `(p.)` when `page` is absent and does not raise, and (d)
  treats `meta=None` (a NULL row) exactly like the no-key case without raising
  `AttributeError`.
- **Integration / eval gate:** run `python -m app.evals` on existing
  `golden.jsonl` (per-case before/after diff, not just mean) plus the new
  `sample.md` section-citation case. The case requires `sample.md` to be ingested
  into `documents` first (see the Open question on ingest ownership); confirm the
  fixture's section chunks are queryable before relying on the case.
- **Rollout:** no migration owned here; no feature flag needed (additive,
  backward-compatible rendering). Ships once PR #2 is merged and the eval gate is
  green. Rollback is a straight revert (return type back to 2-tuple) since no
  data is written.

## Spec contents

- [`README.md`](README.md) — this spec (summary, design, acceptance criteria).
- [`design.md`](design.md) — alternatives considered, interface sketches,
  data-flow + edge-case tables, the golden-keyword selection procedure.
- [`examples/`](examples/) — **illustrative** code (not wired in): the
  `RetrievedChunk` carrier + threaded `retrieval.py`, the extracted
  `render_context()` + `generate_node`, the unit test, and the golden-case /
  eval-setup snippets.
- [`testing.md`](testing.md) — the test & verification plan: how each acceptance
  criterion is proven and how it gates merge via the eval/CI gate.

## References

- [Feature roadmap](../../ROADMAP.md) · [Specs index](../README.md)
- [Canonical document model](../../CANONICAL_MODEL.md) — `meta` / `Locator` (page)
- Depends on **PR #2** (hybrid ingestion): `meta jsonb` column, chunk ingester,
  `data/sample.md`. Sibling: [02-canonical-document-model](../02-canonical-document-model/README.md)
  defines the `meta` key shape this spec consumes.
- Gate ties into **PR #3 / [07-ci-hardening](../07-ci-hardening/README.md)**
  (`.github/workflows/ci.yml` `eval-gate`) and `tests/test_evals.py`. Per-case
  floors that would make the regression diff enforced are
  [06-eval-set-maturity](../06-eval-set-maturity/README.md)'s job, not this spec.
- Seams: `app/retrieval.py`, `app/agent.py`; schema in `db/init.sql` (post-PR #2).
