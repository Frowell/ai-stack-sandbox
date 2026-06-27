# Design notes — Retrieval uses chunk metadata

Deeper notes behind [`README.md`](README.md): alternatives weighed, interface
sketches, the data-flow / edge-case tables, and the procedure for picking the
golden-case keyword. Nothing here is shipped code — see [`examples/`](examples/)
for the illustrative implementation.

## 1. The carrier type: `NamedTuple` vs `dataclass` vs raw `dict`

Today the carrier is `tuple[int, str]`, unpacked positionally at every seam
(`for doc_id, content in ...`, `rrf`'s `for rank, (doc_id, content) in ...`,
`generate_node`'s comprehension). We must widen it to carry `meta`.

| Option | Pros | Cons | Verdict |
|---|---|---|---|
| **`NamedTuple`** (`RetrievedChunk(id, content, meta)`) | Positional unpacking still works at most sites; `.id/.content/.meta` reads clearer; immutable; zero deps | A site doing `for doc_id, content in lst` silently breaks (now 3-wide) — but that is exactly the site we *want* to find and fix | **Chosen.** Best fit for a pipeline that already unpacks tuples. |
| `@dataclass` | Clearest field access; easy defaults (`meta: dict = field(default_factory=dict)`) | Breaks **all** positional unpacking → more churn; mutable unless `frozen=True` | Viable; rejected only because NamedTuple needs fewer edits in `rrf`/comprehensions. |
| raw `dict` | No new type | Stringly-typed; re-introduces the `None`/missing-key hazard this spec is trying to kill; no IDE/type help | Rejected. |

**Decision: `NamedTuple`.** Declared once in `app/retrieval.py`, imported by
`app/agent.py` for `State.context`. The README's "no 2-tuple survives" rule means
`rrf`'s internal unpack and `generate_node`'s comprehension both move to the
named carrier; the only place a bare positional unpack is *safe* is inside `rrf`
where we control both ends — and even there we switch to `.id/.content/.meta`
for clarity. See [`examples/retrieval.py`](examples/retrieval.py).

> Why not just add a 3rd tuple element and keep it a plain `tuple`? Because the
> README's acceptance criterion is "**no 2-tuple survives in the pipeline**" — a
> named type makes a missed seam a clear `AttributeError`/arity error at the
> nearest site instead of a silent `meta`-shaped value landing in `content`.

## 2. Where `NULL → {}` coercion lives (and why twice)

psycopg3's default `jsonb` loader returns a Python `dict` for a JSON object —
**but a SQL `NULL` decodes to Python `None`, not `{}`**. PR #2 adds `meta` as a
*plain nullable* column (`ALTER TABLE documents ADD COLUMN IF NOT EXISTS meta
jsonb`, no `DEFAULT`), so any row not stamped by the canonical ingester (a
pre-re-ingest row, or a plain `corpus.jsonl` row) has SQL `NULL`.

Two guards, on purpose:

1. **At the SQL boundary** (`dense`/`sparse`: `r[2] or {}`). This is the
   *authoritative* one: it guarantees every `RetrievedChunk.meta` flowing out of
   retrieval is a real `dict`, so `rrf`'s `meta[doc_id] = c.meta` map and
   `render_context` are safe on the live path.
2. **In `render_context`** (`m = chunk.meta or {}`). Belt-and-suspenders for a
   *hand-built* `RetrievedChunk(meta=None)` in a unit test (criterion 2d) that
   never went through the SQL boundary. Without it the helper's own unit test
   could only exercise the path the SQL guarantees, never the `None` it is
   supposed to survive.

The two are not redundant in practice: one protects the production query path,
the other lets the deterministic unit test pin the `None` case without a DB.
No `json.loads` and no psycopg type registration are needed — the default loader
plus the coercion is the whole story.

## 3. Provenance render format

`render_context` turns `meta` into a parenthetical. Options:

- **A. Inline parenthetical** `[{id}] ({section}) {content}` — chosen.
- B. Structured trailer (`[{id}] {content}\n  ↳ section: {…}`) — more tokens, a
  bigger prompt delta, more eval-score risk for no functional gain.
- C. A separate `sources:` block appended after the context — changes the prompt
  shape the judge sees the most; highest regression risk.

**Decision: A.** It survives the existing prompt with the least churn and — for
chunks with no displayable key — collapses to *exactly* today's `[{id}] {content}`,
which is what keeps the all-unstructured prompt byte-identical (README criterion
2b). Revisit only if the judge penalizes verbosity (README open question).

### Provenance derivation rules (the exact contract `render_context` honors)

| meta contents | provenance string | line |
|---|---|---|
| `{"section": "A > B"}` | `A > B` | `[3] (A > B) content` |
| `{"section": "A > B", "page": 7}` | `A > B, p.7` | `[3] (A > B, p.7) content` |
| `{"page": 7}` (section absent) | `p.7` | `[3] (p.7) content` |
| `{"doc": "maturity"}` (no displayable key) | — | `[3] content` |
| `{}` | — | `[3] content` |
| `None` (NULL row) | — | `[3] content` (must not raise) |

Order is deterministic: **section first, then page**. Both keys read by
**truthiness** via `.get()` (a missing key *and* a falsy value — `None`, `0`,
`""` — are both skipped); `page` is *never* indexed as `meta["page"]`, and
`section` is `str()`-coerced before joining so a non-string breadcrumb cannot
raise `TypeError` in `", ".join` (README accepted-risk). The parenthetical (and its single
space before `content`) is emitted **only** when at least one displayable part
exists — otherwise the line is byte-for-byte today's `f"[{doc_id}] {content}"`.

> **Page is a forward-compatible no-op.** No producer emits `page` until spec 03
> (PDF backend) ships a `Locator`. The renderer handles it now so 03 needs zero
> retrieval changes; the unit test pins the no-op (present → `p.N`; absent → no
> `(p.)`, no raise). Real page-citation eval coverage is deferred to 03.

## 4. The whole-context join must match, not just the per-line format

`generate_node` today does `"\n\n".join(f"[{doc_id}] {content}" ...)`. The README
requires the no-section path to be byte-identical **at the whole-context level**,
not merely per line — so `render_context` must reproduce the same `"\n\n"`
separator. If it used `"\n"` or a trailing newline, an all-unstructured prompt
would shift and could move eval scores even though no chunk has a section. The
unit test therefore asserts a *list* of no-key chunks renders identically to the
old expression, not just one line (criterion 2b).

## 5. Pure helper extraction (testability seam)

Today the context string is built inline inside `generate_node`, which also calls
`chat()` — so the format is untestable without the LLM. Extracting
`render_context(chunks) -> str` as a pure, importable function is the load-bearing
testability move: the *primary feature proof* (criterion 2) is a deterministic
unit test on this helper, with **no gateway and no DB**. `generate_node` shrinks
to `ctx = render_context(state["context"])` plus the (updated) prompt. See
[`examples/agent.py`](examples/agent.py).

## 6. Where does `sample.md` get ingested for the eval run? (the real open risk)

The new golden case (criterion 5) can only pass if `sample.md`'s section chunks
are queryable when `python -m app.evals` runs. Today:

- `app/ingest.py` **truncates** `documents` and loads **only** `data/corpus.jsonl`.
- The eval harness (`app/evals.py`) does **not** ingest — it assumes the store is
  already populated.
- The CI eval-gate (PR #3 / spec 07) stands up Postgres + LiteLLM and runs the
  gate; whatever ingest it performs is the environment the golden case sees.

Two ways to make `sample.md` present, in preference order:

1. **PR #2 owns it** — adds `sample.md` to the default ingest set so `make
   ingest` (and the CI gate's setup) loads it alongside `corpus.jsonl`. Cleanest;
   this spec then only adds the golden case. **Confirm with #2's owner.**
2. **This spec owns a small eval-setup step** — if #2 declines, add a step that
   ingests `sample.md` (without truncating away the corpus) before the gate runs.
   `ingest()` currently `TRUNCATE`s, so a naive second call would wipe the corpus;
   the setup must ingest both in one pass or ingest `sample.md` append-only.

This is the top open dependency. It is called out as criterion 5 prerequisite (1)
and in the README open questions; do not write the golden case before it is
resolved or the case will silently fail in CI.

## 7. Golden-case keyword selection procedure (criterion 5 prerequisite 2)

The eval harness scores only `keyword` (substring, case-insensitive) and `judge`
against `golden.jsonl` — there is **no per-case custom assertion hook**. So the
proof "the answer cited the section" must be encoded as a keyword. Two failure
modes to avoid:

- **Pass-for-the-wrong-reason.** `Observability` already appears in
  `corpus.jsonl` (the `observability` row) and in an existing golden case, so an
  answer could contain it without ever reading `sample.md`'s section. **Banned.**
- **Flake.** A common English word as the keyword passes/fails on model whim.

**Procedure** (run once `sample.md` exists, post-PR #2):

```
# 1. List sample.md's section breadcrumbs (leaves) after ingest:
#    SELECT DISTINCT meta->>'section' FROM documents WHERE source='sample';
# 2. For each candidate leaf L, confirm it is ABSENT from corpus.jsonl:
grep -i "L" data/corpus.jsonl        # must return nothing
# 3. Pick a leaf that is distinctive to sample.md, put it in the case's
#    "keywords", and write a "question" whose only correct answer lives under
#    that section so the section name should appear in a cited answer.
```

The deterministic `render_context` unit test (criterion 2) is the *primary*
proof; this golden case is a softer end-to-end smoke check whose pass/fail rides
on a non-deterministic model (README risk: "Brittle golden-case keyword").

Because `sample.md` does not exist in the repo yet (it lands with PR #2), the
keyword in [`examples/golden_case.jsonl`](examples/golden_case.jsonl) is an
**illustrative placeholder** clearly marked as TBD-against-#2.

## 8. Edge cases the renderer must survive (all from real `meta` shapes)

Per spec 02, `meta` is heterogeneous across chunk kinds. All flow through the one
renderer:

| chunk kind | representative `meta` | renders as |
|---|---|---|
| layout paragraph | `{"section": "Mature AI Stacks > Reranking", "blocks": [...]}` | `[id] (Mature AI Stacks > Reranking) content` |
| record/table | `{"columns": [...], "format": "csv"}` (no `section`) | `[id] content` (no `()`) |
| uncited footnotes | `{"section": "uncited footnotes", "footnotes": [...]}` | `[id] (uncited footnotes) content` |
| plain corpus row | `{"doc": "maturity"}` | `[id] content` |
| un-stamped row | `NULL` → `None` | `[id] content`, no raise |

The only dangerous one is the last: `None.get(...)` raises `AttributeError` and
crashes `generate_node` on the **live query path**, not just a test — hence the
double coercion in §2 and criterion 2d.

## 9. Why metadata *filtering* is out of scope here

`retrieve(query, format=…, section=…)` → `WHERE meta @> %s::jsonb` (served by
PR #2's `documents_meta_idx` GIN index) is genuinely useful but: (a) expands the
public `retrieve()` API surface, (b) needs its own eval cases to prove the filter
helps and does not silently drop recall, and (c) turns an S "thread one field"
change into an M API change. Deferred to a follow-up (README open question). This
spec stays additive and behind the existing seam.
