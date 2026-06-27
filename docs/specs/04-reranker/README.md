---
title: Reranker
slug: reranker
area: retrieval
tier: Next
size: S
status: Todo
depends_on: []
issue:        # set to the GitHub issue number when created
---

# Reranker

> **Area** `retrieval` · **Tier** `Next` · **Size** `S` · **Status** `Todo` · **Depends on:** —

## Summary

Replace the identity `rerank()` hook in `app/retrieval.py` with a real,
config-selectable reranker. Two backends sit behind one interface: a local
cross-encoder (in-process) and a hosted reranker (Cohere/Voyage) reached
**through the gateway** via LiteLLM's `/rerank` endpoint. The reranker scores the
fused RRF candidate pool and returns the top-N, never crashes retrieval (degrades
to RRF order on any error), and emits a child span with latency and cost. We
prove it helps with a **retrieval-quality metric** (hit@k / MRR over labelled
gold doc ids), not the end-to-end answer score, which is too coarse on this corpus.

> **Companion docs:** [`design.md`](design.md) (alternatives, interface sketches,
> sequence/edge cases), [`examples/`](examples/) (illustrative, codebase-specific
> code — a spec, not wired-in), and [`testing.md`](testing.md) (how each
> acceptance criterion is proven and gates merge).

## Problem / Motivation

`rerank()` is identity (RRF order); we claim a rerank stage but don't have one, and can't show it helps.

## Goals

- A real reranker behind the existing `rerank()` hook -- cross-encoder (local) or hosted (Cohere/Voyage) **through the gateway seam**; provider selected by config, not code.
- The reranker is **fail-open**: any backend error/timeout/empty input degrades to the current RRF order so retrieval (the hot path) never goes down.
- A **reported** retrieval-quality delta vs identity, using a retrieval metric over labelled gold doc ids.
- Per-call latency (both backends) and cost (hosted) visible in a dedicated span.

## Non-goals

- Training a custom reranker.
- Re-ranking by chunk metadata (that is [05-retrieval-uses-chunk-metadata](../05-retrieval-uses-chunk-metadata/README.md)).
- Tuning RRF `k`, pool size, or embedding model.
- Making the local cross-encoder go "through the gateway" — local is in-process by definition; only the hosted backend uses the gateway seam.

## Proposed design

**Interface (unchanged signature).** Keep
`rerank(query, candidates: list[tuple[int, str]], top_n) -> list[tuple[int, str]]`
so `retrieve()` is untouched except that the call is wrapped in a child span.
Backend is selected **per call** by reading `settings.rerank_backend` inside the
dispatcher (not bound at import), so tests can exercise `none`/`local`/hosted
without reimporting the module; default `none` preserves today's identity
behaviour. (Heavyweight per-backend resources — e.g. the local cross-encoder — are
still loaded lazily once and cached in a module global; only the *selection* is
per-call.)

**Config naming convention.** Follow the existing `Settings` pattern: a
**lowercase** dataclass attribute populated from an **UPPER_SNAKE** env var (e.g.
`chat_model` ← `CHAT_MODEL`). So the new attributes are `settings.rerank_backend`
← `RERANK_BACKEND`, `settings.rerank_model` ← `RERANK_MODEL`, etc. The dispatcher
and tests reference the lowercase attribute; only the env var is upper-case.

**Test seam — `Settings` is `@dataclass(frozen=True)`.** A direct
`monkeypatch.setattr(settings, "rerank_backend", "local")` raises
`FrozenInstanceError` and will *not* work. Tests must instead either (a) replace
the module-level reference the dispatcher reads —
`monkeypatch.setattr("app.retrieval.settings", dataclasses.replace(settings, rerank_backend="local"))`
— or (b) `monkeypatch.setenv("RERANK_BACKEND", ...)` and have the dispatcher read
env directly. Option (a) is preferred (keeps one config source); the dispatcher
must therefore read `app.retrieval.settings` by module attribute, not capture a
local alias at import, for the patch to take effect.

**Config (new fields in `app/config.py`, all env-driven):**
- `RERANK_BACKEND` = `none` (default) | `local` | `cohere` | `voyage`
- `RERANK_MODEL` — backend-specific and **overloaded by design**, mirroring how
  `chat_model`/`embedding_model` default to the gateway *alias*, not a provider id:
  - **hosted** (`cohere`/`voyage`): this is the **gateway alias** the app POSTs as
    the request `model` — default `rerank`, matching `model_name: rerank` in
    `litellm_config.yaml`. The provider-specific id (`cohere/rerank-english-v3.0`)
    lives **only** in `litellm_config.yaml`, never in the app. Posting the raw
    provider id (e.g. `rerank-english-v3.0`) would not match any `model_name` and
    LiteLLM would `400` straight into the fail-open path — i.e. the hosted backend
    would *silently never rerank*. The app keeps the alias so a Cohere→Voyage swap
    is a `litellm_config.yaml` + gateway-key edit with **no app env change**.
  - **local**: this is the actual cross-encoder id
    (`cross-encoder/ms-marco-MiniLM-L-6-v2`) — there is no gateway, so the app
    needs the real model id. An unset/empty value here → `CrossEncoder("")` →
    fail-open (documented misconfig).
- `RERANK_POOL` — how many of the **fused** candidates to actually score, default
  10. Note the two distinct "pool" sizes: `retrieve(pool=10)` is the per-source DB
  `LIMIT` (dense and sparse each return ≤10), so RRF can emit up to ~20 unique
  fused candidates. The reranker scores only `fused[:RERANK_POOL]` and returns the
  best `top_n` of those. `RERANK_POOL` is independent of `retrieve`'s `pool` arg;
  if `RERANK_POOL > len(fused)` it simply scores all of them.
- `RERANK_TIMEOUT_S` — the **hosted** call budget, passed as the `httpx` request
  timeout; a timeout raises and trips the fail-open wrapper. **Scope note:** this
  bounds only the hosted (network) backend. The local cross-encoder is a
  synchronous, CPU-bound `predict()` call that a wall-clock budget cannot
  interrupt without threads/signals — out of scope here. Local latency is bounded
  instead by `RERANK_POOL` (number of pairs scored), not by `RERANK_TIMEOUT_S`.

**Backends:**
- `local` — a cross-encoder (`sentence-transformers` `CrossEncoder`). Heavy deps
  (torch, sentence-transformers) go behind an **optional uv dependency-group**, not
  a PEP-621 extra: this repo sets `[tool.uv] package = false` with no
  `[build-system]`, so `pip install '.[...]'` cannot work. Add a `rerank-local`
  group under `[dependency-groups]` (alongside the existing `dev` group) and install
  with `uv sync --group rerank-local`; the default `uv sync` stays lean so the
  image/cold-start is unaffected. **Import lazily, inside the backend, on first
  rerank call** — never at module import — so (a) selecting `local` without the
  group installed degrades via the fail-open path instead of crashing app startup,
  and (b) the model loads once (cache it in a module global) only when actually used.
- `cohere` / `voyage` — reached through the gateway. The app's existing seam
  (`app/gateway.py`, an OpenAI client) has **no** rerank method, and `/rerank` is
  not an OpenAI SDK route, so add a thin `gateway.rerank(query, documents, model,
  top_n, timeout)` that POSTs to `{gateway_base_url}/rerank` with the gateway master
  key using **`httpx`** (already present transitively via `openai`; do not add
  `requests`). The POST `model` field is the gateway **alias** (`rerank`, i.e.
  `settings.rerank_model or "rerank"`), **not** a provider model id — same as `chat`
  sends `chat`; this is what lets LiteLLM route the request and keeps provider choice
  in the gateway config. Map the response — LiteLLM normalizes Cohere/Voyage to
  `{"results": [{"index": i, "relevance_score": s}, ...]}` — back to candidate ids
  by `index` into the `documents` list passed in, then **defensively slice to
  `top_n`** (`out[:top_n]`): some providers ignore/cap `top_n`, and the dispatcher
  must guarantee the same result count as `none`. Add a `rerank` model entry to
  `gateway/litellm_config.yaml` (`litellm_params.model: cohere/...`,
  `api_key: os.environ/COHERE_API_KEY`). The provider API key lives in the gateway
  env, never in the app — same decoupling as `chat`/`embeddings`.

**Fail-open wrapper:** the `rerank()` dispatcher catches **every** failure class —
backend exception, timeout, empty/short input, an unknown `RERANK_BACKEND` value,
**and a missing/unimportable dependency** (e.g. `RERANK_BACKEND=local` without the
`rerank-local` group, which raises `ImportError` on the lazy import) — then
logs/annotates the span and returns `candidates[:top_n]` (RRF order). So both a
provider outage *and* a misconfigured/under-provisioned deployment degrade ranking
quality but never break retrieval. (A startup log line on first fall-back makes the
misconfig visible rather than silent.)

**Observability:** wrap each rerank in `span("rerank", ...)` with attributes
`rerank.backend`, `rerank.model`, `rerank.candidates`, `rerank.top_n`,
`rerank.fell_back` (bool), and for hosted `gen_ai.usage.*`/cost if LiteLLM returns
it. (There is no OTel GenAI convention for rerank yet; use these custom keys.)
Cost/usage values must be set as `float`/`int` — OTel attributes are typed, and the
existing `span()` helper already drops `None`, so emit a number or omit the key.

**Eval / measurement:** add a small retrieval harness that, per gold question,
checks whether the labelled relevant document(s) land in the top-N, and reports
hit@k / MRR for `RERANK_BACKEND=none` vs the chosen backend. **Label by `source`,
not by integer DB id.** The DB `id` is `BIGSERIAL` and `ingest()` does `TRUNCATE
documents RESTART IDENTITY`, so today ids are deterministic (1..N in
`corpus.jsonl` order) — but that couples the gold labels to corpus *ordering*, so
reordering or extending the corpus would silently break them. `source` (e.g.
`"retrieval"`, `"gateway"`) is the stable semantic key; the harness resolves
`source → id` at eval time via a `SELECT id, source` and scores against the
resolved ids. This requires gold labels, which we add to the eval set as part of
this spec (see Test plan).
The end-to-end answer score in `app/evals.py` stays as the merge gate but is **not**
the rerank metric — on a 7-doc corpus with 4 questions it will not move measurably.

### Components & where they live

| Component | File | Change |
| --- | --- | --- |
| Config fields | `app/config.py` | add `rerank_backend`, `rerank_model`, `rerank_pool`, `rerank_timeout_s` to the frozen `Settings` dataclass, env-driven |
| Dispatcher + fail-open wrapper | `app/retrieval.py` | replace the identity `rerank()`; read `app.retrieval.settings` per call, wrap in `span("rerank", ...)`, catch every failure class |
| Local backend | `app/retrieval.py` (`_rerank_local`) | lazy `from sentence_transformers import CrossEncoder`, model cached in a module global |
| Hosted backend | `app/gateway.py` (`rerank()`) | thin `httpx.post` to `{gateway_base_url}/rerank` with the gateway master key |
| Gateway route | `gateway/litellm_config.yaml` | add a `rerank` model entry (`cohere/...` or `voyage/...`), key from gateway env |
| Local deps | `pyproject.toml` | new `[dependency-groups] rerank-local` (uv group, not a PEP-621 extra) |
| Eval harness + gold labels | `evals/` (new `evals/retrieval_gold.jsonl` + a small harness) | `source`→`id` resolution, hit@k / MRR for `none` vs backend |

The seam boundary is unchanged: `retrieve()` still calls `rerank(query, fused,
top_n)`; everything new lives **behind** that call (and behind the gateway, for
hosted).

### Sequencing (retrieve hot path)

```
retrieve(query, top_n=4, pool=10)
  └─ span("retrieve")
       ├─ dense(conn, query, 10)   ─┐
       ├─ sparse(conn, query, 10)  ─┤→ rrf(...)  → fused (≤~20 unique)
       └─ rerank(query, fused, top_n=4)
            └─ span("rerank", backend=…, model=…, candidates=…, top_n=…)
                 ├─ guard: len(fused) <= 1  → return fused[:top_n]   (no backend)
                 ├─ select backend = settings.rerank_backend (read per call)
                 │     none   → return fused[:top_n]                  (identity)
                 │     local  → _rerank_local(query, fused[:pool], top_n)
                 │     hosted → gateway.rerank(query, docs, model, top_n, timeout)
                 └─ except (any error / timeout / ImportError / unknown backend):
                       set rerank.fell_back=true; log once; return fused[:top_n]
```

The only edit to the existing hot path is that the `rerank(...)` call now resolves
a real backend; `dense`/`sparse`/`rrf` are untouched. On every error path the
return value is byte-for-byte the current identity behaviour (`fused[:top_n]`).

### Implementation sequencing (PR shape)

1. Config fields + dispatcher skeleton with `none` (identity) + fail-open wrapper + span. *(No behaviour change at default.)*
2. Hosted `gateway.rerank()` + `litellm_config.yaml` entry + contract test (mocked `httpx`).
3. Local backend behind the `rerank-local` uv group, lazy import.
4. Eval harness + `evals/retrieval_gold.jsonl`; report hit@k / MRR in the PR.

## Acceptance criteria

- [ ] `rerank()` reorders the candidate pool via the chosen backend; backend is selected by `RERANK_BACKEND` config, with **zero application code changes** to switch providers (a hosted Cohere→Voyage swap is a `litellm_config.yaml` + gateway-key edit, with **no app env change** — the app POSTs the gateway alias `rerank`, never a provider model id, exactly like `chat`/`embeddings`).
- [ ] The hosted backend POSTs `model = settings.rerank_model or "rerank"` (the gateway alias matching `model_name: rerank`), proven by the contract test asserting the request body's `model` is the alias, not a provider id. (Posting a raw provider id would 400 into permanent silent fail-open.)
- [ ] The hosted mapped result is sliced to `top_n` even if the provider returns more than `top_n`. For the same query, `none`, `local`, and a normally-responding hosted backend return the **same count** (`min(top_n, len(candidates))`). The **one documented exception** is the hosted edge case where a provider returns *fewer* than `top_n` results (design.md, "Hosted returns fewer results than requested"): we deliberately do **not** pad, so the count can be lower — never higher. Any fail-open path restores the `none` count exactly.
- [ ] `RERANK_BACKEND=none` reproduces today's exact identity (RRF-order) behaviour — the change is opt-in.
- [ ] New config fields follow the existing convention (lowercase attribute ← UPPER_SNAKE env, e.g. `settings.rerank_backend` ← `RERANK_BACKEND`), and the dispatcher reads them via the module-level `settings` object (not an import-time alias) so a `dataclasses.replace`-based patch of the frozen `Settings` takes effect in tests.
- [ ] **Fail-open:** with a hosted backend selected but the provider unreachable / timing out / returning an error, `retrieve()` still returns results in RRF order (no exception escapes), and the span records `rerank.fell_back=true`. Demonstrated by a test that forces the failure.
- [ ] **Fail-open on misconfig:** `RERANK_BACKEND=local` with the `rerank-local` group **not** installed (and an unknown `RERANK_BACKEND` value) falls back to RRF order with `rerank.fell_back=true` rather than crashing app import/startup. Covered by a test.
- [ ] Empty or single-candidate input is handled without calling the backend and without error.
- [ ] The local heavy deps install via `uv sync --group rerank-local` (a uv dependency-group, since the project is non-packaged); the default `uv sync` does **not** pull torch/sentence-transformers.
- [ ] `RERANK_POOL` scores at most the fused candidate list (`fused[:RERANK_POOL]`) and returns `top_n`; `none` and the configured backend return the same number of results for the same query (subject to the single hosted "provider returned fewer" exception noted above — we do not pad).
- [ ] Gold labels in the eval set key on `source` (resolved to id at eval time), not hard-coded integer ids.
- [ ] The rerank **unit/contract tests are hermetic** — collected by the default `uv run pytest` and individually runnable (e.g. `uv run pytest tests/test_rerank.py`) with **no** live provider key, gateway, or Postgres; the dispatcher, fail-open, and hosted-mapping paths are exercised with stubs/mocks. (Note: the pre-existing `tests/test_evals.py::test_quality_gate` in the same suite still requires the full stack, so a *bare* `uv run pytest` without the stack is not expected to pass end-to-end — that non-hermetic gate is unchanged and out of scope.)
- [ ] A retrieval-quality delta vs identity is **reported** (hit@k and/or MRR over labelled gold doc ids) for at least one real backend, computed by the harness — not hand-asserted, not the end-to-end answer score.
- [ ] A dedicated `rerank` span carries `rerank.backend`, `rerank.model`, `rerank.candidates`, `rerank.top_n`, `rerank.fell_back`, and hosted cost/usage when available.
- [ ] The local cross-encoder's heavy deps are an optional uv dependency-group (`uv sync --group rerank-local`); the default install/build and cold-start are unchanged.
- [ ] The existing eval merge gate (`uv run pytest`) still passes with the default config.

## Dependencies

- None **hard**. Showing the quality delta needs gold doc-id labels on the eval
  set; this spec adds a minimal set of labels itself rather than blocking on
  [06-eval-set-maturity](../06-eval-set-maturity/README.md). A larger, more
  statistically meaningful delta is a follow-up there.
- Soft: LiteLLM proxy must expose the rerank route for a supported provider
  (Cohere/Voyage/Jina/etc.); confirm the deployed gateway version supports it and
  the exact path — LiteLLM serves both `/rerank` and `/v1/rerank`; pick one and
  pin it in `gateway.rerank()` (and assert it in the contract test) so a future
  gateway upgrade can't silently 404 into the fail-open path.

## Open questions

- Which hosted provider is the reference backend for the reported delta — Cohere `rerank-english-v3.0` or Voyage `rerank-2`? (Affects which API key the gateway needs in CI.)
- Should the merge gate run the hosted-backend delta (needs a live key + spend) or only the local cross-encoder, with the hosted path covered by a mocked/contract test? Leaning: hermetic mocked contract test in the gate, hosted/local deltas run on demand. (Note: there is no `.github/` Actions workflow in this repo yet — "CI" today is `uv run pytest` per the README/Makefile; treat the rerank gate additions as pytest cases, not a new workflow.)
- Is hit@k or MRR the headline metric, and at what `k` (top_n=4)?

## Risks & mitigations

- **Provider outage on the hot path** — retrieval feeds every agent answer and the eval gate. *Mitigation:* fail-open wrapper + timeout; outage degrades ranking quality, never availability.
- **Cost/latency creep** — hosted rerank adds a network round-trip and per-query cost; local cross-encoder adds CPU latency. *Mitigation:* small pool (default 10); `RERANK_TIMEOUT_S` bounds the hosted call (it does **not** bound the synchronous local backend — pool size does); cost/latency in spans; backend off by default.
- **Heavy local dependency (torch/sentence-transformers)** bloats the image and cold start. *Mitigation:* optional `rerank-local` uv dependency-group + lazy import; not installed and not imported unless the operator opts in to the local backend.
- **Measurement is noise on a tiny corpus** — 7 docs, and the retrieval gold set added here (`examples/retrieval_gold.jsonl`) has **6 questions** (distinct from `evals/golden.jsonl`'s 4 answer-score questions; the "4 questions" caveat elsewhere refers to that answer-score gate). A "delta" over 6 questions may be statistically meaningless. *Accepted risk:* report the metric as a demonstrated mechanism, not a benchmark; defer a real benchmark to eval-set maturity.
- **Eval harness reports a 0.000 delta when run with the default `RERANK_BACKEND=none`** — `__main__` computes `evaluate("none")` vs `evaluate(settings.rerank_backend)`, so with the default both sides are `none` and the delta is trivially zero. This is a runner footgun, not a bug. *Accepted:* the harness docstring documents `RERANK_BACKEND=cohere uv run python -m evals.retrieval_eval`; when porting, consider printing a one-line warning if the resolved treatment backend is `none`.
- **A `rerank` span is now emitted on every `retrieve()`, including at the default `none` backend** (`rerank.backend=none`, `rerank.fell_back=false`). The **return value** is byte-for-byte identical to today (AC "none reproduces identity" is about the result, not the trace), but trace output gains one child span per query at default. *Accepted:* the span is cheap (no exporter unless `OTEL_EXPORTER_OTLP_ENDPOINT` is set) and is the point of the observability goal.
- **Lazy-init race on the local model.** `_local_model` and `_warned_fallback`
  are unguarded module globals; two concurrent first-rerank calls could load the
  cross-encoder twice (transient extra memory, last write wins) and emit the
  warning twice. *Accepted risk:* both outcomes are benign and self-healing; a
  `threading.Lock` around the lazy load is a cheap follow-up if local is run under
  real concurrency. The hosted path has no shared mutable state.
- **Span-exporter test hygiene.** `test_span_records_fell_back` attaches a
  `SimpleSpanProcessor`/`InMemorySpanExporter` to the **process-global**
  `TracerProvider`. Add it inside a fixture that removes it (or use a fresh
  provider) so it does not accumulate across the suite and leak spans into other
  tests. Captured as a test-implementation requirement (see testing.md), not a
  product risk.
- **Weak provider-key assertion.** `assert "COHERE_API_KEY" not in str(captured)`
  only proves a literal string is absent, not that no provider secret leaks; the
  real guarantee is architectural (the key lives in the gateway env, never reaches
  the app). *Accepted:* the assertion is a smoke check, not the guarantee.
- **Pre-existing:** the query-embedding cache keys on `hash(text)`, which is salted per process (`PYTHONHASHSEED`), so it never hits across restarts. Out of scope here; flagged so a rerank cache doesn't copy the pattern.

## Test & rollout plan

- **Unit (hermetic — no DB/gateway/key):** dispatcher routing per `RERANK_BACKEND`; `none` == identity; empty/single-candidate guards; fail-open path returns RRF order and sets `rerank.fell_back` when the backend raises/times out, when the local group is absent (`ImportError`), and when `RERANK_BACKEND` is unknown (force each via a stub/monkeypatched `settings`).
- **Contract (hosted, mocked):** mock the gateway `/rerank` `httpx` response (`{"results":[{"index","relevance_score"}]}`); assert the app reorders candidates by returned `index`/relevance, slices to `top_n`, and never includes the provider key in the request to the app-facing seam (only the gateway master key goes out).
- **Eval/measurement:** add gold `source` labels to a retrieval eval set (sibling to `evals/golden.jsonl`, since `golden.jsonl` has no doc labels today); the harness resolves `source → id` and prints hit@k / MRR for `none` vs the chosen backend. Reported in the PR, not asserted as a hard threshold (corpus too small).
- **Rollout:** ship behind `RERANK_BACKEND=none` (default) so merging is a no-op; enable `local`/hosted via env. No DB migration. Hosted requires the gateway `rerank` model entry + provider key in gateway env.

## References

- [Design notes](design.md) — alternatives, interface sketches, sequence/edge cases
- [Examples](examples/) — illustrative, codebase-specific code (a spec, not wired in)
- [Testing plan](testing.md) — acceptance-criteria coverage + the merge gate
- [Feature roadmap](../../ROADMAP.md)
- [Specs index](../README.md)
- [Eval-set maturity](../06-eval-set-maturity/README.md) — larger gold set / real benchmark follow-up
- [Retrieval uses chunk metadata](../05-retrieval-uses-chunk-metadata/README.md) — sibling retrieval feature (metadata rerank is out of scope here)
- [Canonical document model](../../CANONICAL_MODEL.md)
