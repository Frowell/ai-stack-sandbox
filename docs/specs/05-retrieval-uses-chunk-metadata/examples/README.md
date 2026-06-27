# Examples — illustrative only

> **These files are a specification, not wired-in code.** They are not imported by
> the app, not on any build path, and intentionally live under `docs/`. They show
> the *real* signatures, file paths, and shapes the implementation should match so
> a reviewer can judge the design before any code lands. Lines that depend on a
> not-yet-merged artifact (PR #2's `meta` column / `sample.md`) are flagged inline
> with `# DEPENDS-ON-#2`.

| File | Mirrors (when implemented) | Shows |
|---|---|---|
| [`retrieval.py`](retrieval.py) | `app/retrieval.py` | the `RetrievedChunk` NamedTuple + `meta` threaded through `dense`/`sparse`/`rrf`/`rerank`/`retrieve` with `NULL → {}` coercion |
| [`agent.py`](agent.py) | `app/agent.py` | `State.context` retyped, the extracted pure `render_context()` helper, and the slimmed `generate_node` + updated system prompt |
| [`test_render_context.py`](test_render_context.py) | `tests/test_render_context.py` | the deterministic, no-LLM unit test that is the **primary feature proof** (one assertion block per sub-criterion of acceptance criterion 2) |
| [`golden_case.jsonl`](golden_case.jsonl) | a new line in `evals/golden.jsonl` | the end-to-end section-citation smoke case (keyword is an **illustrative placeholder** — pick the real one per `design.md` §7) |
| [`eval_setup.py`](eval_setup.py) | eval/CI setup (or PR #2's ingest) | the *fallback* append-only `sample.md` ingest, only if PR #2 does not add `sample.md` to the default ingest set (`design.md` §6) |

See [`../design.md`](../design.md) for why each shape was chosen and
[`../testing.md`](../testing.md) for how each acceptance criterion is proven and
gated.
