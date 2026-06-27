# Caching — test & verification plan

How **each** acceptance criterion in [`README.md`](README.md) is proven, what
fixtures it needs, and how it gates merge through the project's eval/CI gate.
Concrete example tests in the project idiom are in
[`examples/test_caching.py`](examples/test_caching.py).

## Test tiers (match the existing project shape)

The repo runs `uv run pytest` (`make test`) and `tests/test_evals.py` is the
merge gate today (`conftest.py` puts the repo root on `sys.path`). Caching tests
split into:

- **Unit (offline, deterministic, default run).** No stack, no provider key. Key
  construction, `cache.layer` derivation, pricing math, retrieval key alignment.
  These run in `lint`/`unit` and on **every** PR including forks (no secret).
- **Live (needs the stack + provider key).** Marked `@pytest.mark.live`; stand up
  `litellm` + `redis` (+ `postgres` for the agent path) and make real calls.
  Exact-hit, fail-open, eval-bypass, prompt-cache measurement, metrics end-to-end.
  Skipped in the default unit run and on fork CI; run in the secret-gated job (the
  same gating model the eval gate uses — see [spec 07](../07-ci-hardening/README.md)).
- **Eval (the gate itself).** `tests/test_evals.py` / `make eval`, run twice:
  once caching-OFF (the merge gate) and once semantic-ON (regression check).

Declare the `live` marker in `pyproject.toml` `[tool.pytest.ini_options]` and skip
it when `GATEWAY_BASE_URL`/`OPENAI_API_KEY` are absent, so the default
`uv run pytest` stays self-contained.

## Fixtures needed

| Fixture | What it does | Used by |
|---|---|---|
| `gateway_up` | the running `litellm` proxy with `litellm_config.caching.yaml` (exact-match) | exact-hit, metrics |
| `gateway_semantic` | proxy with the `redis-semantic` block + a set threshold; **`temperature: 0` pinned in `model_list`** for the eval-bypass positive control | near-duplicate, semantic regression, eval-bypass positive control |
| `gateway_on_eval_config` | proxy started with `litellm_config.eval.yaml` (caching OFF); **`temperature: 0` pinned in `model_list`** so the caching-OFF leg is deterministic | eval-bypass |
| `redis_stopped` | stops the `redis` container for the test body, restarts after | fail-open |
| `fresh_redis` | flushes the cache DB before the test so latency/hit assertions are clean | exact-hit, near-duplicate, eval-bypass positive control |
| `mutate_corpus` | edits `data/corpus.jsonl` + re-runs `app.ingest` to change retrieved context, then **restores the original file in teardown** (it is a tracked repo file — never leave it mutated) | eval-bypass, invalidation |
| `count_cached_tokens` | reads `usage.prompt_tokens_details.cached_tokens` from a raw response | prompt-cache measurement |

A provider call costs money, so live tests use the smallest prompts that exercise
the path; the eval suite already bounds its own cost via the golden set size.

## Per-criterion proof

### AC-1 — cache hit/miss, layer, savings visible as span attributes / metrics
- **Type:** unit (layer derivation) + live (end-to-end span).
- **Unit:** `derive_cache_meta()` maps `{cache_hit, cached_tokens, configured_type}`
  → `cache.layer` ∈ `exact|semantic|prompt|miss` and `cache.hit`
  (`test_cache_layer_derivation`).
- **Live:** issue a repeated call through the agent and assert the `generate` span
  carries `cache.hit`, `cache.layer`, `cache.tokens_saved`, `cache.cost_saved_usd`
  (captured via an in-test OTEL span exporter). Proves the chosen mechanism —
  **widening `chat()`** with `.with_raw_response` (examples/`app_gateway.py`) —
  actually surfaces the metadata `chat()` currently discards. (Dashboard-only
  hit-rate via `/metrics` scraping is the alternative and is not required for this
  per-request AC — see design.md §6.)
- **Gate:** unit part required on every PR; live part in the secret-gated job.

### AC-2 — identical query served from cache, faster, no provider call, byte-identical
- **Type:** live (`test_identical_query_is_cache_hit_and_byte_identical`).
- **Proof:** two identical calls; second has `cache.hit == True`, `cache.layer ==
  "exact"` (default `type: redis`), returns **byte-identical** content, and is
  materially faster (assert hit latency < 0.5× miss). "No provider call" is shown
  by the latency gap plus the cache-hit flag; optionally assert against a request
  counter / mock provider. **Both modes:** repeat under `gateway_semantic` and
  assert the identical repeat still hits (cosine ≈ 1.0) — demonstrating
  `redis-semantic` subsumes exact match.
- **Gate:** secret-gated live job.

### AC-3 — near-duplicate served from semantic only above threshold; no eval regression
- **Type:** live + eval.
- **Near-duplicate:** under `gateway_semantic`, a paraphrase above the configured
  `similarity_threshold` hits (`cache.layer == "semantic"`); a clearly-different
  question misses. Sweep around the threshold to confirm it gates.
- **No regression (the load-bearing one):** run `app/evals.py` with semantic
  caching ON and assert `report["mean_score"]` is **not below** the caching-OFF
  baseline (and still ≥ `THRESHOLD` 0.7). This is a **separate, non-merge-path
  job** (semantic is off by default; it costs real calls) — see
  examples/`ci_eval_gate.snippet.yaml`. It is the empirical answer to the
  threshold open question: tune `similarity_threshold` until this job is green.
- **Gate:** semantic-regression job blocks **enabling semantic**, not general
  merge. Document the measured threshold before semantic is turned on anywhere.

### AC-4 — exact-vs-semantic mode-exclusivity resolved
- **Type:** documentation + one live confirmation.
- **Proof:** [design.md §2](design.md) records the resolution (single
  `cache_params.type` scalar; `redis-semantic` subsumes exact). The live
  confirmation: start the proxy with each `type` and assert behavior; and verify
  the pinned image rejects (or ignores) a *list* value for `type` — the single
  line flagged in design.md §2. Resolution documented **before** semantic is
  enabled anywhere.
- **Gate:** not a runtime gate; a doc-review checklist item on the PR.

### AC-5 — prompt caching measured, not assumed
- **Type:** live measurement (`count_cached_tokens`).
- **Proof:** issue a repeated call with a stable prefix and read
  `usage.prompt_tokens_details.cached_tokens`. **Two acceptable outcomes**, both
  satisfy the AC honestly:
  1. `cached_tokens > 0` on the repeat → prompt caching demonstrably engages, OR
  2. the test records that today's ~20-token system prefix is **below the provider
     minimum** (OpenAI ~1024; Sonnet 4.6 2048; Opus 4.x 4096 — design.md §5) and
     `cached_tokens` stays 0, so prompt caching is **deferred** until the stable
     prefix is grown (accepted, known-inert risk). The test asserts the recorded
     expectation rather than a hard `> 0`, so it does not flake on the inert state.
- **Gate:** the recorded outcome is a PR checklist item; the measurement runs in
  the live job.

### AC-6 — eval runs bypass every cache layer
- **Type:** live (`test_eval_config_serves_fresh_after_corpus_change` +
  `test_positive_control_semantic_on_serves_stale`).
- **Proof:** with the gateway on `litellm_config.eval.yaml` (caching OFF), run the
  agent over a question, mutate the corpus + re-ingest (changes retrieved context →
  changes the answer), run again, and assert the two generations **differ** — a
  changed answer is re-scored, not served stale. This exercises the
  **process-level switch** mechanism (caching-off CI config), since per-request
  `no-cache` cannot thread through `ask()`→`generate_node`→`chat()` (design.md §3).
  The eval config carries **no `cache_params` at all**, so *all three* layers are
  bypassed by construction: there is no stored response for the agent or the judge
  to be served, and prompt caching never returns a stored answer.
- **Positive control — must use the SEMANTIC config, not exact (this is the
  load-bearing subtlety):** a positive control has to prove the cache *would* mask
  the corpus change. The mutate-corpus procedure only does that under
  **`redis-semantic`**. Reason: the **exact-match** key is a hash of
  `model + messages + params`, and the agent embeds the retrieved context **inside
  the user message** (`app/agent.py:generate_node`). A corpus re-ingest changes that
  context → changes the message → **changes the exact-match key**, so even a
  caching-**ON** *exact* gateway returns a *fresh* answer (a1 != a2). Exact-match
  therefore **cannot** mask a corpus-change regression and is the wrong positive
  control. Only the **semantic** cache (approximate match) can still hit across a
  small context change and serve the stale answer. So:
  - **Semantic caching-ON config** (`gateway_semantic`): a1 = `ask(q)`; mutate
    corpus + re-ingest; a2 = `ask(q)`. Assert **a1 == a2** — the near-duplicate
    request still clears the threshold, the stale answer is served, proving the
    cache *would* mask the corpus change.
  - **Eval (caching-OFF) config** (`gateway_on_eval_config`): same procedure.
    Assert **a1 != a2** — no stored response, so the changed context changes the
    answer.
  - **Determinism:** pin `temperature: 0` **in the gateway config's `model_list`**
    (`litellm_params.temperature: 0`) — *not* via an `ask()` kwarg, which cannot
    thread through `ask()`→`generate_node`→`chat()` (the same blocked path as
    `no-cache`, design.md §3). With temperature pinned, the caching-OFF difference
    is attributable to the corpus change, and the semantic stale-hit is the only
    thing that can make the caching-ON pair equal.
- **Exact-match bypass evidence:** the eval config stores nothing, so exact-match is
  bypassed by construction. The separate evidence that an exact cache *would* serve a
  stored response is **AC-2** (identical repeat → byte-identical hit); exact-match
  cannot itself mask a *corpus-change* regression (its key moved), which is exactly
  why the positive control above is run against semantic.
- **Gate:** secret-gated live job. This protects the integrity of the merge gate
  itself — if it fails, the eval gate cannot be trusted, so it blocks merge.

### AC-7 — semantic off by default, toggled purely via config/env, no app change
- **Type:** config diff review + parity.
- **Proof:** the default `gateway/litellm_config.yaml` ships `type: redis` (or no
  cache) — **not** `redis-semantic`. Enabling semantic is a YAML/env edit only;
  grep the PR diff to confirm no `app/` change is required to flip it. A parity
  assertion: agent behavior with semantic OFF matches today's behavior.
- **Gate:** PR-diff checklist item.

### AC-8 — fail-open verified empirically
- **Type:** live (`test_fail_open_when_redis_down`).
- **Proof:** with the `redis_stopped` fixture, a chat call still **succeeds** and
  every call reports a miss. Because fail-open here is **LiteLLM-controlled** (not
  an app try/except like `retrieval.py`), it is proven by test, not assumed by
  analogy (design.md §7).
- **Gate:** secret-gated live job.

### AC-9 — cache-key construction includes the virtual key (unit now; live deferred)
- **Type:** unit now (`test_cache_key_includes_virtual_key`); live deferred.
- **Proof now:** the key-construction helper folds in the caller key so two keys
  over the same request produce **different** cache keys, and `(key, request)` is
  stable so a real repeat can still hit.
- **Deferred:** the live **two-key isolation** test (request under key A never
  returns key B's entry) needs a DB-backed gateway / virtual keys
  ([spec 10](../10-budgets-and-virtual-keys/README.md)); until then the namespace
  is a documented no-op (single master key). State this explicitly in the PR.
- **Gate:** unit part required on every PR; live part tracked as a follow-up tied
  to spec 10.

### AC-10 — documented invalidation rules
- **Type:** documentation + targeted live checks.
- **Proof:** [design.md §8](design.md) documents (a) TTL expiry, (b) prompt-prefix
  change busts prompt caching (automatic, by construction), and (c) a
  re-ingestion namespace bump that busts **exact + semantic** entries (data
  staleness, not just prefix changes). Live spot-checks: a TTL-expired entry
  misses; after a corpus re-ingest + namespace bump, a previously-cached identical
  question misses (so it is not served a stale, now-wrong answer).
- **Gate:** doc-review checklist item; the TTL/namespace spot-checks run in the
  live job.

## How it ties into the CI / eval gate

- **Merge path (every PR, including forks):** unit caching tests run in
  `lint`/`unit`. The eval gate (`tests/test_evals.py`) runs against the
  **caching-OFF** gateway (`litellm_config.eval.yaml`) so a regression can never be
  masked by a cached answer or judge verdict (AC-6). This is the one change to the
  existing eval-gate job — point its `docker run … --config` at the eval config
  (examples/`ci_eval_gate.snippet.yaml`); coordinate the job/step names with
  [spec 07](../07-ci-hardening/README.md), which owns `ci.yml`.
- **Live caching job (secret-gated):** AC-1 (e2e), AC-2, AC-5, AC-6, AC-8, and the
  AC-10 spot-checks. Gated on the provider secret like the eval gate; forks skip it.
- **Semantic-regression job (off the merge path, gated/nightly):** AC-3 — run the
  golden set with `redis-semantic` ON and assert no score regression vs. the
  caching-OFF baseline. Blocks **enabling semantic**, not general merge, and is the
  empirical tuner for `similarity_threshold`.
- **Doc/PR-diff checklist (reviewer-enforced):** AC-4, AC-7, AC-9 (deferral note),
  AC-10 (rules documented).

## Determinism / cost notes

- Unit tests are offline and deterministic — they keep the default `uv run pytest`
  self-contained (no paid dependency), matching the project's existing gate shape.
- Live tests cost real provider calls; keep prompts tiny and gate them on the
  secret. The semantic-regression job is the most expensive (it re-runs the golden
  set) — run it gated/nightly, not on every PR push.
- The eval gate is already non-deterministic (LLM-judge, no seed — see
  [spec 07](../07-ci-hardening/README.md) risks). Caching tests must **not** add to
  that: the regression check compares semantic-ON against a same-run caching-OFF
  baseline, not against a stored constant, so judge variance cancels.
