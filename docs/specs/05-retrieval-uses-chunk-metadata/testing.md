# Testing & verification plan — Retrieval uses chunk metadata

How every acceptance criterion in [`README.md`](README.md) is proven, and how it
gates merge. The proof strategy is deliberately lopsided: the **deterministic
unit test on `render_context()` is the load-bearing proof**; the eval gate
protects against regression; the new golden case is a softer end-to-end smoke
check riding on a non-deterministic model.

## Test layers

| Layer | Where | Needs | Runs |
|---|---|---|---|
| Unit — carrier threading | `tests/test_retrieval_meta.py` (new) | nothing (or a fake conn) | every PR, fast |
| Unit — `render_context()` | `tests/test_render_context.py` (new) | nothing (no LLM, no DB) | every PR, fast |
| Integration / eval gate | `app/evals.py` via `tests/test_evals.py::test_quality_gate` | Postgres + gateway, ingested corpus | `make test` locally; CI `eval-gate` job |
| Regression diff | `python -m app.evals` before/after | same as gate | manual / CI step (see "Gating") |

The two unit layers are the new, deterministic guards this spec adds. The eval
layer already exists; this spec adds one case to it.

## How each acceptance criterion is proven

### AC1 — typed carrier; `meta` end to end, no 2-tuple survives
**Proof: unit + the type checker + the suite.** A unit test (`tests/test_retrieval_meta.py`)
asserts `dense`/`sparse` return `RetrievedChunk` with `.meta` a dict (NULL row →
`{}`), and that `rrf`/`rerank` preserve `.meta` for an id. Because the carrier is
a `NamedTuple`, any seam still doing a 2-wide positional unpack fails loudly
(arity / `AttributeError`) at that site — caught by this unit test + the eval
gate exercising the live path. Sketch (fake conn returning rows incl. a NULL meta):

```python
# tests/test_retrieval_meta.py  (idiom: cf. tests/test_evals.py)
from app.retrieval import RetrievedChunk, rrf, rerank

def test_rrf_preserves_meta():
    dense = [RetrievedChunk(1, "a", {"section": "S"}), RetrievedChunk(2, "b", {})]
    sparse = [RetrievedChunk(2, "b", {}), RetrievedChunk(1, "a", {"section": "S"})]
    fused = rrf(dense, sparse)
    assert all(isinstance(c, RetrievedChunk) for c in fused)
    by_id = {c.id: c.meta for c in fused}
    assert by_id[1] == {"section": "S"}            # meta survived fusion

def test_rerank_passes_chunks_through():
    cands = [RetrievedChunk(1, "a", {"section": "S"}), RetrievedChunk(2, "b", {})]
    assert rerank("q", cands, top_n=1) == cands[:1]   # identity, still RetrievedChunk
```
For `dense`/`sparse` the SQL-boundary `r[2] or {}` coercion is proven with a fake
cursor whose `fetchall()` returns a row with `None` in column 2; assert the
resulting `RetrievedChunk.meta == {}`.

### AC2 — primary feature proof: `render_context()` (deterministic, no LLM)
**Proof: `tests/test_render_context.py`** — the full file is in
[`examples/test_render_context.py`](examples/test_render_context.py). One
assertion block per sub-criterion:

- **(a)** `meta["section"]="A > B"` → line contains `(A > B)`.
- **(b)** meta with no displayable key (`{"doc": …}`, `{}`) → a line
  **byte-identical** to `[{id}] {content}`, and a *list* renders byte-identical to
  the old `"\n\n".join(...)` expression (whole-context level, design.md §4).
- **(c)** a `page` key → `p.{n}` (after section: `(S, p.7)`); no `page` → no
  `(p.)` and no raise.
- **(d)** `meta=None` (a NULL DB row) → renders exactly like the no-key case and
  does **not** raise `AttributeError`.

This is the proof that survives a model swap — it asserts the *format contract*,
which is the actual feature. It must pass on every PR.

### AC3 — `[id]` convention still works; prompt still instructs `[id]`
**Proof: (a)** AC2 already pins that the `[{id}]` prefix is present on every line
in both the provenance and no-provenance branches. **(b)** A trivial assertion
that the system prompt string in `app/agent.py` still contains `[id]` (cheap
guard against accidentally dropping the citation instruction while editing the
prompt). Belt: the existing golden cases (which score `[id]`-style answers) stay
green through the eval gate.

### AC4 — eval gate green on existing `golden.jsonl`; no per-case regression
**Proof: two parts.**
1. **Mean gate (enforced):** `tests/test_evals.py::test_quality_gate` asserts
   `report["passed"]` (`mean ≥ THRESHOLD = 0.7`) over `evals/golden.jsonl`. This
   is the merge gate and already exists; nothing new to write.
2. **Per-case regression (manual / CI step — the gate hides this):** `app/evals.py:run()`
   fails only on the **mean**, so a single existing case can drop materially and
   still pass. Capture per-case scores before the change and after, and assert no
   existing case dropped:
   ```bash
   git stash && python -m app.evals > /tmp/before.txt        # baseline (step-1 of Sequencing)
   git stash pop && python -m app.evals > /tmp/after.txt     # with provenance
   diff <(sort /tmp/before.txt) <(sort /tmp/after.txt)       # inspect per-case deltas
   ```
   The strongest mitigation is structural: the no-section path is byte-identical
   (AC2b), so the all-unstructured prompts the existing cases produce do not
   shift at all. Tightening the gate to *enforce* per-case floors is
   [06-eval-set-maturity](../06-eval-set-maturity/README.md)'s job, explicitly
   out of scope here.

### AC5 — new golden case exercises section citation end to end
**Proof: a new line in `evals/golden.jsonl`** (shape in
[`examples/golden_case.jsonl`](examples/golden_case.jsonl)). Two prerequisites,
both verified before relying on the case (design.md §6, §7):

1. **Fixture present:** `data/sample.md` ingested *with its `meta`* into
   `documents` before evals run. Verify:
   ```sql
   SELECT count(*) FROM documents WHERE source='sample' AND meta ? 'section';  -- > 0
   ```
   Owned by PR #2 if it adds `sample.md` to the default ingest set; otherwise by
   the fallback in [`examples/eval_setup.py`](examples/eval_setup.py).
2. **Keyword actually proves citation:** the keyword is a section *leaf*
   distinctive to `sample.md` and **absent** from `corpus.jsonl` (so it cannot
   pass for the wrong reason). Verify the leaf is not already in the corpus:
   ```bash
   grep -i "<chosen-leaf>" data/corpus.jsonl    # must print nothing
   ```
   Do **not** use `Observability` (already in `corpus.jsonl` and an existing
   case). The keyword in the example file is a clearly-marked placeholder because
   `sample.md` does not exist until PR #2.

Because the harness scores only `keyword`/`judge` (no per-case assertion hook),
the citation proof is encoded as the keyword. This case is the softer check;
AC2 is the hard proof.

## Fixtures needed

- **None for the unit layers** — `RetrievedChunk`s are hand-built in-test; no DB,
  no gateway, no network. This is the point of extracting `render_context()`.
- **For the eval layer:** the running stack (Postgres + gateway) the gate already
  needs, **plus** `data/sample.md` ingested with `meta` (the AC5 prerequisite).

## How it gates merge

- **Local:** `make test` → `uv run pytest -q` runs `test_render_context.py`,
  `test_retrieval_meta.py`, and `test_evals.py::test_quality_gate`. A failed
  format contract or a sub-0.7 mean fails the build.
- **CI:** the eval gate lands with **PR #3** (`.github/workflows/ci.yml`,
  hardened by [07-ci-hardening](../07-ci-hardening/README.md)). Its `eval-gate`
  job stands up `pgvector/pgvector` + `litellm`, ingests, and runs
  `python -m app.evals`; the required `eval-gate-result` summary job turns that
  into the merge-blocking check. The fast unit tests run in the always-on `lint`/
  test path so they gate even fork PRs that skip the secret-gated eval job. (No
  `.github/` exists in the repo yet — it arrives with PR #3; until then the local
  `make test` gate stands in.)
- **Rollout:** no migration owned here, no feature flag (additive, backward-
  compatible). Ships once PR #2 is merged and the gate is green. Rollback is a
  straight revert (carrier back to 2-tuple) since no data is written.
