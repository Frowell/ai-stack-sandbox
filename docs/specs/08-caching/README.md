---
title: Caching (semantic + prompt)
slug: caching
area: gateway
tier: Later
size: M
status: Backlog
depends_on: []
issue:        # set to the GitHub issue number when created
---

# Caching (semantic + prompt)

> **Area** `gateway` · **Tier** `Later` · **Size** `M` · **Status** `Backlog` · **Depends on:** —

## Summary

Cut provider cost and latency on repeated and near-duplicate model calls by adding
toggleable cache layers behind the gateway seam (prompt caching is independent;
exact-match and semantic are two modes of one Redis `cache_params.type` switch — see
the mode-exclusivity caveat in design §2):
(1) **prompt caching** — keep a stable, cacheable prefix (system prompt + tools)
so the provider's own prefix cache *discounts input tokens* (it does **not** serve
a stored response; the model still generates and output is still billed);
(2) **exact-match response caching** — serve the stored response byte-for-byte
when a new request hashes identically to a prior one (LiteLLM `type: redis`); and
(3) **semantic caching** — serve a previously-generated *response* when a new
request is embedding-similar to a prior one, above a tuned threshold (LiteLLM
`type: redis-semantic`). All three live in LiteLLM/Redis config, not application
code. Exact-match and semantic caching are what actually cut cost on *repeated*
and *near-duplicate* calls respectively; prompt caching only trims the input-token
bill on the stable prefix. Because semantic caching can return an *approximate*
match, it ships **off by default**, gated behind the eval suite, and is the
highest-risk layer.

## Problem / Motivation

Every call hits a provider; repeated/near-duplicate requests and stable prompt
prefixes cost full price and latency. Today `app/gateway.py` sends every chat and
embedding call straight through with no response reuse, and the only cache in the
system is the ad-hoc per-query embedding cache in `app/retrieval.py`.

## Goals

- Gateway semantic cache (LiteLLM `redis-semantic`), **off by default**, with a
  configurable similarity threshold and TTL.
- Gateway **exact-match response cache** (LiteLLM `type: redis`) so a byte-identical
  repeated request is served from Redis without a provider call. This — not prompt
  caching — is what makes a repeated identical query cheap and fast.
- A deliberate prompt-caching strategy (stable-prefix discipline) that works with
  the **currently-configured provider** (OpenAI auto prefix-cache today; explicit
  `cache_control` breakpoints when the Claude block in `litellm_config.yaml` is
  active). **Caveat (accepted risk):** today's agent prompt (`app/agent.py:generate_node`)
  is a ~20-token system instruction with no tools/few-shot, and OpenAI auto
  prefix-caching only engages at ≥1024 stable prefix tokens — so prompt caching
  delivers ~zero benefit until the stable prefix is grown (instructions + tools +
  few-shot moved ahead of the variable context). Treat prompt caching as
  groundwork, and measure cached-prefix tokens before claiming savings.
- Cache-hit rate, hit/miss, and estimated savings as first-class metrics on the
  existing OTEL observability seam.
- **Fail-open**: if Redis is unavailable, every request is a cache miss and the
  hot path is unaffected (same contract as `app/retrieval.py`'s optional cache).

## Non-goals

- A bespoke cache store (use the gateway/Redis already present).
- Caching across virtual keys / tenants (see [Budgets & virtual keys](../10-budgets-and-virtual-keys/README.md));
  this spec namespaces the cache **per key** rather than sharing entries.
- Replacing or rewriting the `app/retrieval.py` embedding cache. We only align its
  **key strategy**: that cache keys on `f"emb:{hash(text)}"`, and Python's builtin
  `hash()` is salted per-process (`PYTHONHASHSEED`) for strings, so its keys do not
  survive a restart and never collide across processes — i.e. it barely caches today.
  Alignment means adopting a **stable digest** (e.g. `sha256(text)`) and a shared
  key-prefix convention so the gateway cache and the retrieval cache don't tread on
  each other in the same Redis DB. (Tracked as an open question, not a rewrite.)

## Proposed design

Three behaviours, configured independently in `gateway/litellm_config.yaml`
under `litellm_settings` (the file already reserves this spot: *"Caching,
guardrails, and logging callbacks attach here in a real deployment"*).

**How the seam is realized in LiteLLM (concrete).** Response caching is a
first-class LiteLLM proxy feature: a `litellm_settings.cache: true` flag plus a
`cache_params` block that selects **one** backend via `cache_params.type` and
points it at the existing `redis` service. The app keeps calling the `chat` /
`embeddings` aliases through `app/gateway.py` — no caller names a cache, exactly
like the provider-swap seam. Two wiring facts the current stack does **not** yet
satisfy and this slice must add:
- **The `litellm` service is not connected to Redis today.** `docker-compose.yml`
  gives `app` a `REDIS_URL` but the `litellm` container has no Redis env at all.
  Response caching needs the proxy itself to reach Redis, so the slice adds
  `REDIS_HOST` / `REDIS_PORT` (or a `REDIS_URL`) to the `litellm` service env and
  references them from `cache_params`. (This is the same wiring [spec 10](../10-budgets-and-virtual-keys/README.md)
  needs for rate-limit counters — coordinate so it is added once.)
- **`redis-semantic` needs an embedding model for the lookup.** It is configured
  to reuse the existing `embeddings` alias, so the semantic lookup routes back
  through the same gateway (and counts against the same provider bill — see the
  net-ROI open question).

The concrete, illustrative YAML for both modes is in
[`examples/litellm_config.caching.yaml`](examples/litellm_config.caching.yaml);
the compose wiring is in [`examples/docker-compose.caching.yaml`](examples/docker-compose.caching.yaml);
the deeper rationale (mode-exclusivity, eval-bypass mechanism, key construction)
is in [`design.md`](design.md).

### 1. Prompt caching (low risk, ship first — token discount only)
- Prompt caching **does not return a stored response** — it only discounts the
  input tokens of a stable prefix on an otherwise-live provider call. It cannot
  satisfy "served from cache, byte-identical" (that is layer 2).
- The app structures messages for this: a fixed `system` prompt with retrieved
  context in the `user` message (`app/agent.py:generate_node`). For prefix caching
  to bite, the **stable** content (instructions, tools, few-shot) must come first
  and vary the least, **and exceed the provider's minimum** (OpenAI: ≥1024 tokens).
  The current ~20-token system prompt is far below this, so today this layer is
  inert — see the accepted-risk note under Goals.
- **OpenAI (current default `openai/gpt-4o-mini`)**: automatic prefix caching, no
  config or code change — verify it engages (`usage.prompt_tokens_details.cached_tokens`)
  and is reported.
- **Anthropic (commented block)**: requires `cache_control` breakpoints. Inject
  via LiteLLM rather than app code to preserve the "app code never names a
  provider" seam contract.

### 2. Exact-match response caching (low risk, deterministic)
- `litellm_settings.cache: true` with `cache_params.type: redis` keys on a hash of
  the full request (model + messages + cache-affecting params). A byte-identical
  repeat is served from Redis with no provider call — the source of the
  "byte-identical, materially lower latency" acceptance demo.
- Unlike semantic caching this is **always correct** (the request is identical), so
  it can be on without the eval gate, subject to the same per-key namespacing and
  fail-open requirements below.
- **Mode-exclusivity caveat (open question, see below):** LiteLLM selects the Redis
  cache backend via a **single** `cache_params.type` field — `redis` *or*
  `redis-semantic`, not both at once. So exact-match and semantic are **not** two
  simultaneously-stacked Redis layers; they are two values of one switch. The
  saving grace is that `redis-semantic` mode *subsumes* exact match: a byte-identical
  repeat embeds to cosine ≈ 1.0 and hits above any threshold ≤ 1.0. The intended
  states are therefore: **default** `type: redis` (exact-only, always correct,
  no eval gate); **opt-in** `type: redis-semantic` (covers exact repeats *and*
  near-duplicates, eval-gated). Confirm during expansion that LiteLLM does not also
  accept a list of types; if it does, the layers really are independent. Prompt
  caching (provider-side, §1) is genuinely independent of this switch.

### 3. Semantic caching (higher risk, ship behind eval gate, off by default)
- `litellm_settings.cache: true` with `cache_params.type: redis-semantic`, a
  `similarity_threshold`, a `ttl`, and the Redis connection (the examples use
  `host`/`port` from `REDIS_HOST`/`REDIS_PORT`; a single `redis_url` is the
  equivalent alternative) pointing at the existing `redis` service. Semantic cache
  requires an embedding model for the lookup — reuse the `embeddings` alias.
- **Correctness contract (the core risk):** a semantic hit returns a *different*
  request's answer. This is only safe when (a) the threshold is high enough that
  the eval suite shows **no score regression** with caching on, and (b) entries
  expire. Disabling on a regression is part of the rollout, not an afterthought.
- **Namespacing:** cache key/namespace must include the virtual key (and model +
  cache-affecting params) so one caller never receives another's cached response.
  **Feasibility caveat (open question):** the gateway runs **stateless with a single
  master key** today (`docker-compose.yml`: *"Stateless here (no DB)… add a Postgres
  URL to persist virtual keys / budgets"*), and LiteLLM's default cache key hashes
  the request **without** the caller's key. So (a) there is only one key to namespace
  by right now, and (b) per-key namespacing may not be a stock config knob — it must
  be verified, and if LiteLLM does not support it natively the fallback is a custom
  cache-key hook or an app-side namespace prefix. Until virtual keys exist
  ([spec 10](../10-budgets-and-virtual-keys/README.md)), the namespace is a no-op and
  the cross-key isolation criterion is tested at the unit level (key-construction),
  with the live two-key isolation test deferred to a DB-backed gateway.

### 4. Eval isolation (mandatory)
The eval gate (`app/evals.py`) runs both the agent **and** the LLM-judge through
the same gateway. With response caching on, a cached pre-change answer or judge
verdict would **mask a real regression**, defeating the gate. The eval run MUST
bypass the cache. **Mechanism note:** the per-request `cache: {"no-cache": true}`
hint cannot be threaded through cleanly today — `app/evals.py` calls `ask()`, whose
LangGraph path (`generate_node → chat()`) accepts no per-call kwargs, and
`app/gateway.py:chat()` forwards `**kwargs` to the OpenAI SDK where `cache` would
have to be wrapped in `extra_body`. The **recommended** mechanism is therefore a
process-level switch: the eval-gate CI job stands up the gateway with a
**caching-off config** (`gateway/litellm_config.eval.yaml`, identical `model_list`,
no `cache: true`) by pointing its `docker run … --config` at that file. **Note:**
nothing in LiteLLM or the app actually *reads* a `CACHE_DISABLED` env var — the
bypass is realized purely by *which config file is mounted*, so `CACHE_DISABLED` is
at most a human-facing label that selects the config in the CI job, not a runtime
flag. Do **not** ship a `CACHE_DISABLED` toggle that nothing consumes. Whichever
config is mounted, an automated test proving eval runs are not served stale is an
acceptance gate. This applies to all three layers (exact-match and semantic alike).

### 5. Metrics
LiteLLM tags responses with cache-hit status, but `app/gateway.py:chat()` currently
returns only `resp.choices[0].message.content` and **discards** the response object
(headers, `usage`, `_hidden_params`) that carry cache metadata. Surfacing per-request
`cache.hit` (bool), `cache.layer` (`prompt`|`exact`|`semantic`), and
`cache.tokens_saved` as span attributes in `app/observability.py` therefore requires
either (a) widening `chat()` to expose that metadata to the caller, or (b) scraping
LiteLLM `/metrics` out-of-band. Per-request span attributes need (a); a hit-rate
dashboard can use (b) alone. Compute estimated $ savings from a per-model price
table. Hit-rate is a dashboard metric, not just a log line.

**Savings accounting must count *output* tokens too (correctness).** An exact/semantic
response-cache hit serves a *stored completion*, so it avoids both the input **and the
output** token cost — and for chat models the output rate is usually the larger of the
two. A price table that prices input tokens only (as the first-draft `pricing.py` sketch
did) **understates** the headline savings KPI for the layer that saves the most. The
price table must therefore carry per-model **input and output** rates: on an
exact/semantic hit, `cost_saved = input_rate·prompt_tokens + output_rate·completion_tokens`;
on a prompt-cache discount, only the cached-prefix input delta is saved (no output
saving, since the model still generated). `cache.layer` itself is **derived from the
response, not from a hand-synced app setting where possible** — see the layer-label
drift caveat below.

**Layer-label drift (accepted risk).** Distinguishing an *exact* from a *semantic* hit
cannot always be read off the response — LiteLLM may only stamp a generic `cache_hit`
flag, so the `exact`/`semantic` label is inferred from the configured `cache_params.type`.
That couples the metric's label to a value the app must learn from the gateway. Resolve
by having the app read the configured type from a **single source** (the same env that
selects the gateway config, e.g. `CACHE_LAYER`/derived from the mounted config) rather
than a second hand-set constant; if they drift, only the *label* is wrong (`hit`,
`tokens_saved`, and `$` are still correct). Confirm during expansion whether the pinned
image exposes the cache *type* on the hit so the label can be derived directly.

## Repository layout (new / changed)

```
gateway/
  litellm_config.yaml          # CHANGED: + litellm_settings.cache + cache_params
                               #          (default type: redis; redis-semantic opt-in)
  litellm_config.eval.yaml     # NEW (CI-only): same model_list, caching OFF —
                               #   the eval-gate job's `docker run` points --config here
app/
  gateway.py                   # CHANGED (only if per-request cache attrs): widen chat()
                               #   to return cache metadata via .with_raw_response, not
                               #   just the message string
  observability.py             # CHANGED: helper to attach cache.* span attributes
  pricing.py                   # NEW (optional): per-model price table for tokens_saved → $
  config.py                    # CHANGED (only if per-request cache attrs): + cache_layer_name
                               #   (the "exact"|"semantic" label, derived from the mounted
                               #   config / CACHE_LAYER env — see metrics §5 layer-label drift)
                               #   used by derive_cache_meta(); examples reference settings.cache_layer_name
  retrieval.py                 # CHANGED (key-strategy alignment only): emb: key uses
                               #   sha256(text), not builtin hash() — see non-goals
docker-compose.yml             # CHANGED: wire REDIS_HOST/REDIS_PORT into the litellm
                               #   service (today only `app` has REDIS_URL)
.env.example                   # CHANGED: + cache toggles/threshold/TTL (documented).
                               #   NOTE: no CACHE_DISABLED runtime flag — eval bypass is
                               #   by mounting litellm_config.eval.yaml, not an env var.
.github/workflows/ci.yml       # CHANGED (coordinate with spec 07): eval-gate job runs
                               #   the gateway with caching disabled (eval config / env)
evals/                         # unchanged code; re-baselined with semantic ON (testing.md)
tests/
  test_caching_*.py            # NEW: exact-hit, fail-open, key-construction, eval-bypass,
                               #   metrics — see testing.md
```

The illustrative content of each new/changed file is in
[`examples/`](examples/) (a spec, not wired-in code); the per-criterion proof
plan is in [`testing.md`](testing.md); the deeper rationale is in
[`design.md`](design.md).

## Acceptance criteria

- [ ] Cache hit/miss, **layer** (`prompt`|`exact`|`semantic`), and estimated savings
      are visible as OTEL span attributes / metrics (not just stdout). If exposed as
      per-request span attributes, `app/gateway.py:chat()` is widened to surface the
      response's cache metadata (it currently returns only the message string).
- [ ] A repeated **identical** query is served from the cache (demoed) with
      materially lower latency and **no provider call**; the served response is
      byte-identical to the origin response. In the **default** `type: redis` mode
      this is the exact-match layer; in `redis-semantic` mode the same identical
      repeat must still hit (cosine ≈ 1.0) — both are demonstrated.
- [ ] A near-duplicate query is served from the **semantic** cache (`redis-semantic`)
      only when above the configured threshold (demoed), and the full eval suite
      (`make eval` / `app/evals.py`) shows **no score regression** with semantic
      caching enabled.
- [ ] The exact-vs-semantic mode-exclusivity question is resolved during expansion:
      either confirmed that LiteLLM exposes one `cache_params.type` switch (default
      `redis`, opt-in `redis-semantic` which subsumes exact), or that a multi-type
      config lets the two run as truly independent layers. The chosen model is
      documented before semantic is enabled anywhere.
- [ ] Prompt caching is **measured, not assumed**: the demo shows
      `cached_tokens` > 0 on a repeated stable prefix, OR the spec records that the
      current prefix is below the provider minimum and prompt caching is deferred
      until the prefix is grown (accepted risk).
- [ ] **Eval runs bypass every cache layer** — the eval-gate job mounts the
      caching-off config (`litellm_config.eval.yaml`), since per-request `no-cache`
      cannot be threaded through `ask()`→`generate_node`→`chat()`. Proven by a test
      with a **positive control**, and the control **must run against the SEMANTIC
      config, not exact** (subtle but load-bearing): the exact-match key hashes
      `model + messages + params` and `generate_node` embeds the retrieved context
      *inside the user message*, so a corpus re-ingest changes the message → changes
      the exact key → even a caching-ON *exact* gateway returns a fresh answer.
      Exact-match therefore cannot mask a corpus-change regression; only the
      semantic cache (approximate match) can. So the pair is: under
      **semantic-ON** the mutate-corpus-and-rerun serves a **stale** answer
      (a1 == a2) **and** under the eval (caching-OFF) config it returns a **fresh**
      answer (a1 != a2). Pin `temperature: 0` **in the gateway `model_list`**
      (`litellm_params.temperature: 0`) — not via an `ask()` kwarg (same blocked path
      as `no-cache`) — so the caching-OFF difference is attributable to the corpus
      change, not LLM non-determinism. Exact-match in the eval config is bypassed by
      construction (no `cache_params` ⇒ nothing stored); the evidence an exact cache
      *would* serve a stored response is AC-2 (identical-repeat byte-identical hit).
- [ ] Semantic caching is **off by default** and toggled purely via
      `litellm_config.yaml` / env — no `app/` code change to enable/disable.
- [ ] **Fail-open verified empirically:** with Redis stopped, requests still succeed
      (every call a miss). Fail-open is **LiteLLM-controlled here** (not an app
      try/except like `app/retrieval.py`), so it is proven by test, not assumed by
      analogy.
- [ ] Cache-key construction **includes the virtual key** (unit-tested now). Live
      two-key isolation (request under key A never returns key B's entry) is tested
      once a DB-backed gateway / virtual keys exist; until then namespacing is a
      documented no-op (single master key).
- [ ] Documented invalidation rules: TTL expiry, prompt-prefix change busts prompt
      cache, and a documented way to bust **exact + semantic** entries on document
      re-ingestion (data staleness, not just prefix changes).

## Dependencies

- None to start for the **local** unit + live tests (Redis + LiteLLM already in
  `docker-compose.yml`; the eval-bypass and fail-open tests stand up their own
  gateway via `docker run`).
- **Ordering (soft, CI-integration only):** there is **no `.github/workflows/`
  directory yet** — [CI hardening (spec 07)](../07-ci-hardening/README.md) creates
  `ci.yml` and owns the `eval-gate` job. The acceptance criterion "eval runs bypass
  every cache layer" is *wired into CI* by pointing that job's `docker run --config`
  at `litellm_config.eval.yaml`, so the CI integration of AC-6 **depends on spec 07
  landing first** (the README's "eval-gate job" / ".github/workflows/ci.yml CHANGED"
  references are to spec-07-owned files). The mechanism itself is provable locally
  without CI via the `gateway_on_eval_config` fixture.
- **Soft:** per-key namespacing aligns with [Budgets & virtual keys](../10-budgets-and-virtual-keys/README.md);
  build the namespace hook so it's a no-op until virtual keys exist.
- **Ordering prerequisite (hard, only if both ship):** if [guardrails](../09-guardrails/README.md)
  PII redaction is enabled, redaction (`pre_call`) MUST run before the cache key is
  computed, or raw PII is cached and redaction-equivalent prompts miss each other.
  See the PII risk above. No constraint while guardrails are off.

## Open questions

- **Similarity threshold**: what value yields zero eval regression on the current
  golden set? Needs empirical tuning during expansion (start conservative, e.g.
  ≥0.95 cosine, and relax only if evals stay green).
- **Data staleness vs. semantic cache**: RAG answers embed retrieved doc content;
  re-ingestion can make cached answers wrong. Is a global TTL sufficient, or do we
  need an ingestion-triggered namespace bump (cache version key)? **Feasibility
  caveat:** the proposed "bump `cache_params.namespace` when `app/ingest.py` runs"
  is **not** implementable as an in-process app action — the gateway config is
  mounted **read-only** and `cache_params` is read at proxy start, so changing the
  namespace requires a **gateway config edit + restart/redeploy** (an ops action),
  and having `app/ingest.py` reach into the cache would violate the "app code never
  names the cache" seam. Realistic options: (a) **default = short TTL** (no live
  bump; staleness bounded by `ttl`); (b) a deploy-time namespace derived from a
  corpus/version stamp so a fresh ingest + redeploy gets a clean namespace; (c) if
  LiteLLM exposes a runtime namespace API, an out-of-band ops hook (not app code)
  flips it. Pick (a) now; document (b)/(c) as the escape hatch. The README's earlier
  "ingestion-triggered bump" should be read as this ops-action, not an automatic
  `ingest.py` side effect.
- **Net ROI**: a semantic lookup itself costs one embedding call + a vector
  search. At low hit rates this can be *slower and more expensive* than no cache —
  measure break-even hit-rate before defaulting it on anywhere.
- **Param sensitivity**: should temperature/max_tokens be part of the semantic key,
  or is matching on prompt-embedding alone acceptable given `drop_params: true`?
- **Exact + semantic simultaneity** — *resolved during expansion (with a
  pinned-image caveat).* LiteLLM's `cache_params.type` is a **single scalar** in
  the documented config schema (`redis` *or* `redis-semantic`, also `local`/`s3`/
  `qdrant-semantic`/`disk`) — it is not a list, so exact-match and semantic are
  **two values of one switch**, not stacked layers. The saving grace holds:
  `redis-semantic` subsumes exact match (a byte-identical repeat embeds to
  cosine ≈ 1.0). Intended states: **default** `type: redis`; **opt-in**
  `type: redis-semantic`. *Caveat:* this is read from the LiteLLM config schema,
  not executed against the pinned `main-stable` image in this expansion — the
  single line to confirm against the actual pinned image is `cache_params.type`
  (see [design.md §2](design.md)). Prompt caching (§1) is provider-side and
  independent of this switch either way.

## Risks & mitigations

- **Semantic false-hit returns a wrong answer** (Critical) → off by default; high
  threshold; eval-gated; TTL bounds blast radius.
- **Eval gate corrupted by caching** (Critical) → eval runs bypass the cache;
  enforced by a test (acceptance criterion).
- **Cross-key/tenant leakage** (High) → per-key namespacing; no shared entries.
  *Caveat:* native LiteLLM per-key namespacing is unverified and only one (master)
  key exists today — see the feasibility caveat in design §3; live test deferred to
  spec 10 / DB-backed gateway, unit-test key construction now.
- **Stale answers after re-ingestion** (High) → **short TTL is the primary
  mitigation** (applies to exact-match cache too, not just semantic). The
  namespace-bump escape hatch is an **ops action** (config edit + gateway restart),
  not an automatic `ingest.py` side effect — see the feasibility caveat under Open
  questions; `ingest.py` cannot mutate the read-only mounted gateway config at
  runtime.
- **Redis outage takes down hot path** (High) → fail-open, **verified by test**;
  unlike `retrieval.py` this is LiteLLM-controlled, so it must be proven, not assumed.
- **Negative ROI at low hit rate** (High) → measure break-even before enabling.
- **PII cached before redaction / callback ordering** (High, cross-spec with
  [guardrails](../09-guardrails/README.md)) → guardrail PII redaction runs at
  LiteLLM `pre_call` (it mutates the outbound payload — see
  [09-guardrails/design.md §7](../09-guardrails/design.md)). If the cache key/lookup
  is computed on the **pre-redaction** payload, (a) raw PII becomes part of cache
  keys / stored entries (a data-at-rest concern in Redis), and (b) two requests that
  redact to the *same* prompt miss each other. The cache must key on the **post-
  redaction** payload, i.e. redaction must run **before** the cache hook. This
  ordering is **not guaranteed by default** and must be verified/configured when
  both features land. Until guardrails ship, no redaction occurs and the cache keys
  raw content — acceptable in the single-tenant sandbox, but this ordering is a
  **blocking prerequisite before guardrails + caching are both on in any tenant
  deployment.** (Tracked jointly with spec 09.)
- **Accepted risk:** prompt caching specifics differ by provider; we support the
  active provider only and re-verify when the gateway block is switched.
- **Accepted risk / known-inert:** with the current ~20-token system prompt and no
  tools/few-shot, prompt caching yields ~zero benefit (below OpenAI's 1024-token
  prefix minimum). Shipped as groundwork; real savings need a grown stable prefix.
- **Accepted risk:** the existing `app/retrieval.py` embedding cache keys on
  `hash()` (per-process salted) and is effectively non-persistent; aligning to a
  stable digest is in scope as key-strategy alignment, not a rewrite.

## Test & rollout plan

- **Unit/integration:** (1) identical-query hit returns origin bytes from the
  exact-match cache with no provider call; (2) Redis-down fail-open (asserted, since
  LiteLLM-controlled); (3) cache-key construction includes the virtual key
  (unit-level; live two-key isolation deferred to DB-backed gateway); (4) eval-run
  cache bypass via the process-level switch; (5) metrics/attributes emitted (requires
  surfacing cache metadata from `chat()` or scraping `/metrics`).
- **Eval gate:** run `app/evals.py` with semantic caching **on** in a dedicated CI
  job — merge blocked if score regresses vs. caching-off baseline.
- **Rollout:** prompt caching + exact-match response caching first (low risk;
  exact-match can be on by default since identical-request hits are always correct,
  subject to namespacing + TTL). Semantic caching ships **off**, enabled per-environment via
  config after threshold tuning, watched on the hit-rate/eval dashboard, and rolled
  back by a one-line config flip. No schema/migration required (Redis only).

## References

- [Feature roadmap](../../ROADMAP.md)
- [Specs index](../README.md)
- [Budgets & virtual keys](../10-budgets-and-virtual-keys/README.md) — per-key cache namespacing; shares the `litellm`↔Redis wiring
- [Guardrails](../09-guardrails/README.md) — also attaches callbacks at the gateway; **ordering matters** (PII redaction must run before any cache key is computed — see [09-guardrails/design.md §7](../09-guardrails/design.md))
- [CI hardening](../07-ci-hardening/README.md) — defines the `eval-gate` job (inline `docker run --config`) these tests run under; the eval-bypass config is mounted there
- Expanded package in this directory: [`design.md`](design.md) · [`examples/`](examples/) · [`testing.md`](testing.md)
- Touch points: `gateway/litellm_config.yaml` · `app/gateway.py` · `app/retrieval.py` (existing embedding cache) · `app/evals.py` (eval-gate interaction) · `app/observability.py` (metrics seam) · `docker-compose.yml` (`litellm`↔Redis wiring)
- LiteLLM caching: `litellm_settings.cache` + `cache_params` (`type: redis` exact-match, `type: redis-semantic` semantic); per-request `cache: {"no-cache": true}` hint
- Anthropic prompt caching: `cache_control: {type: "ephemeral"}` breakpoints (model-specific minimum prefix — 2048 tokens on Sonnet 4.6, 4096 on Opus 4.x; `cache_creation_input_tokens` / `cache_read_input_tokens` in `usage`). OpenAI: automatic prefix caching ≥1024 tokens, `usage.prompt_tokens_details.cached_tokens`.
