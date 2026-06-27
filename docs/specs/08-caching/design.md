# Caching — design notes

Deeper notes behind [`README.md`](README.md): the LiteLLM realization, the
mode-exclusivity resolution, the eval-bypass mechanism (and why the obvious one
doesn't work), key construction, metrics plumbing, prompt-caching specifics per
provider, and edge cases. The concrete code is in [`examples/`](examples/)
(illustrative — a spec, not wired-in code).

## 1. Where each layer lives

| Layer | Lives in | App code change? | On by default? | Eval-gated? |
|---|---|---|---|---|
| **Prompt caching** (token discount) | Provider-side; OpenAI auto, Anthropic via LiteLLM-injected `cache_control` | No | n/a (provider behavior) — inert today (prefix < 1024) | No |
| **Exact-match** (`type: redis`) | `litellm_config.yaml` `cache_params` | No | **Yes** (always correct) | No |
| **Semantic** (`type: redis-semantic`) | `litellm_config.yaml` `cache_params` | No | **No** | **Yes** |
| **Metrics** (`cache.*` span attrs) | `app/gateway.py` + `app/observability.py` | Yes (widen `chat()`) **or** scrape `/metrics` | n/a | No |
| **Eval bypass** | CI-only gateway config / env | No app change (recommended path) | n/a | gates the gate |

The first three are pure config behind the gateway seam. The metrics layer is
the only one that may touch `app/`, and only because `chat()` currently throws
away the response object that carries the cache metadata.

## 2. Mode-exclusivity — the single-switch model (resolved, with caveat)

The README's central structural question: are exact-match and semantic two
independent stacked Redis layers, or two values of one switch?

**Resolution from the LiteLLM config schema:** `cache_params.type` is a single
scalar string — one of `redis`, `redis-semantic`, `qdrant-semantic`, `local`,
`s3`, `disk`. It is **not** a list. So the two Redis modes are mutually
exclusive; you cannot run `redis` *and* `redis-semantic` as two simultaneous
layers off one `cache_params` block.

This is fine because **`redis-semantic` subsumes exact match**: a byte-identical
repeat embeds to cosine ≈ 1.0, which clears any `similarity_threshold ≤ 1.0`. So
the two intended states are:

- **Default — `type: redis`** (exact-only, always correct, no eval gate, on by default).
- **Opt-in — `type: redis-semantic`** (covers exact repeats *and* near-duplicates;
  eval-gated; off by default).

**Caveat (the one line to confirm against the pinned image):** this is read from
LiteLLM's documented config schema, not executed against the pinned
`ghcr.io/berriai/litellm` image in this expansion. The one field to verify on the
pinned tag is `cache_params.type` — specifically whether the build silently
accepts a list (which would make the layers genuinely independent). The default
`type: redis` path is unaffected by the answer; only the "can we stack them"
optimization depends on it, and the spec does not rely on stacking.

Prompt caching (§5) is provider-side and orthogonal to this switch — it discounts
input tokens on a live call and never selects a Redis backend.

## 3. The eval-bypass mechanism — why per-request `no-cache` doesn't thread through

The eval gate (`app/evals.py`) runs **both** the agent and the LLM-judge through
the same gateway. With response caching on, a cached pre-change answer or judge
verdict masks a real regression and defeats the gate. Eval runs MUST bypass every
cache layer.

LiteLLM *does* support a per-request opt-out: `cache={"no-cache": true}` in the
request body. The problem is threading it through this codebase:

```
app/evals.py        ask(c["question"])            # no kwargs accepted
  → app/agent.py    GRAPH.invoke({...})           # LangGraph node, no per-call kwargs
    → generate_node chat([...messages...])        # chat() called with NO kwargs
      → app/gateway.py  _client.chat.completions.create(..., **kwargs)
                        # `cache` would have to ride in extra_body={"cache": {...}}
```

`generate_node` calls `chat()` with no kwargs, and `ask()` exposes none — so a
per-request hint cannot reach `create()` without widening the whole call chain.
The **judge** call in `evals.py:judge_score` *does* call `chat()` directly and
could take a kwarg, but the agent path cannot, so a per-request approach would
only half-cover the gate (judge bypassed, agent still served stale). That is
worse than useless.

**Recommended mechanism — a process-level switch, in CI config, not app code:**

The eval-gate CI job already stands up its own gateway. Per the
[CI-hardening spec](../07-ci-hardening/README.md), it runs LiteLLM via an inline
`docker run … --config <file>` step (not `services:`, because LiteLLM needs its
mounted config). So the bypass is: **point that `--config` at a caching-disabled
config** — `gateway/litellm_config.eval.yaml`, identical `model_list`, no
`cache: true`. Zero app change, provably bypasses *all three* layers (exact,
semantic, and — by using a config with no caching — leaves only provider prompt
caching, which never serves a stored response). This is the "dedicated config /
namespace" the README points to, made concrete.

`CACHE_DISABLED=1` is then just the env that selects which config the gateway
loads (the CI job can `cp`/template, or mount the eval config directly). Either
way an automated test (testing.md AC-6) proves a *changed* answer is re-scored,
not served stale, during an eval run.

**Positive control must run against the SEMANTIC config, not exact (load-bearing).**
The AC-6 positive control mutates the corpus to prove the cache *would* mask the
change. That only works under `redis-semantic`. The exact-match key is a hash of
`model + messages + params`, and `generate_node` puts the retrieved context *inside
the user message* — so a corpus re-ingest changes the message, changes the
exact-match key, and a caching-ON *exact* gateway returns a **fresh** answer
(a1 != a2) too. Exact-match cannot mask a corpus-change regression; the
approximate-matching semantic cache can (a near-duplicate request still clears the
threshold and serves the stale answer). The control therefore pairs **semantic-ON**
(`a1 == a2`, stale) against the **caching-OFF** eval config (`a1 != a2`, fresh).
Pin `temperature: 0` in the gateway `model_list` (`litellm_params.temperature: 0`),
not via an `ask()` kwarg — the kwarg cannot thread through `ask()`→`generate_node`→
`chat()`, the same blocked path that motivates the whole config-switch mechanism.
Exact-match in the eval config is bypassed by construction (no `cache_params` ⇒
nothing stored); that an exact cache *would* serve a stored response is shown
separately by AC-2 (identical-repeat byte-identical hit).

**Alternatives considered:**
- *Per-request `extra_body={"cache": {"no-cache": true}}` threaded through* —
  rejected: requires widening `ask()` → `generate_node` → `chat()` to pass kwargs,
  an app-code change to the hot path purely for the eval path, and easy to get
  half-right (judge but not agent).
- *Distinct cache namespace per eval run* (`cache_params.namespace` keyed on a run
  id) — viable but still **writes** entries; a within-run repeat could self-hit.
  A namespace that's guaranteed-empty-and-write-suppressed is just "disabled" with
  extra moving parts. The caching-off config is simpler and strictly safer.
- *Flush Redis between eval runs* — racy under any concurrency and doesn't stop a
  within-run self-hit.

## 4. Cache-key construction and per-key namespacing

**The risk:** a cache entry keyed only on the request content (model + messages +
params) is shared across *all* callers. Once virtual keys exist
([spec 10](../10-budgets-and-virtual-keys/README.md)), caller A could be served
caller B's response — a cross-tenant leak.

**Today's reality (feasibility caveat):** the gateway is stateless with a
**single master key** (`docker-compose.yml`: *"Stateless here (no DB)… add a
Postgres URL to persist virtual keys / budgets"*). So:
1. There is exactly **one** key to namespace by — the namespace is a **no-op**
   until spec 10 lands DB-backed virtual keys.
2. LiteLLM's default cache key hashes the request **without** the caller's key.
   Whether per-key namespacing is a stock config knob on the pinned image is
   **unverified**.

**Design:** build the namespace hook so it is a no-op today and becomes real when
keys exist. Options, in order of preference:
- **(a) Native LiteLLM cache-key control** if the pinned image exposes it
  (a per-key namespace or a cache-key-includes-key setting). Confirm against the
  image; if present, use it.
- **(b) A custom cache-key hook** — LiteLLM allows a custom cache-key function;
  it can prefix the key with `user_api_key_hash`. This is the fallback if (a) is
  absent.
- **(c) App-side namespace prefix** — last resort, since it leaks the namespace
  concept into `app/` and only the metadata-surfacing change should touch `app/`.

**Testing now vs. later:** unit-test the *key construction* (that a key includes
the virtual-key component) today; defer the live **two-key isolation** test
(request under key A never returns key B's entry) to a DB-backed gateway / spec 10.
Until then the cross-key criterion is a documented no-op (single master key).

## 5. Prompt caching — per-provider specifics (and why it's inert today)

Prompt caching discounts the **input tokens** of a stable prefix on an otherwise
live call. It never returns a stored response (that is the exact-match layer).
Two provider behaviors:

**OpenAI (current default `openai/gpt-4o-mini`):** automatic prefix caching, no
config and no code change. It engages only when the **stable prefix ≥ 1024
tokens**, and the discount is reported in `usage.prompt_tokens_details.cached_tokens`.

**Anthropic (commented `anthropic/claude-sonnet-4-6` block):** requires explicit
`cache_control: {"type": "ephemeral"}` breakpoints (max 4 per request) on the
content blocks at the end of the stable prefix. Render order is
`tools → system → messages`, so a breakpoint on the last system block caches
tools + system together. The **minimum cacheable prefix is model-specific** and
this matters for the choice in the commented block:

| Model (the gateway's options) | Min cacheable prefix |
|---|---:|
| `openai/gpt-4o-mini` (auto prefix cache) | ~1024 tokens |
| `anthropic/claude-sonnet-4-6` | **2048 tokens** |
| Anthropic Opus 4.x (if ever swapped in) | 4096 tokens |

> **Verify before relying on the Anthropic rows (accepted risk).** The Claude model
> names above are the *forward-looking placeholders* used in the commented gateway
> block, not the live provider (the active provider is OpenAI). The per-model minimum
> cacheable prefix is provider- and model-specific and changes over time (Anthropic's
> published minimums have historically been ~1024 tokens for larger Claude models and
> ~2048 for the smallest) — so treat these figures as illustrative and **re-confirm
> against the provider's current prompt-caching docs for the exact model when the
> Claude block is actually activated**. Nothing in the OpenAI default path depends on
> these numbers.

Anthropic reports the split in `usage.cache_creation_input_tokens` (written,
~1.25× cost for the 5-minute TTL, 2× for `ttl: "1h"`) and
`usage.cache_read_input_tokens` (read, ~0.1× cost). To preserve the
"app code never names a provider" seam, the `cache_control` breakpoints are
**injected by LiteLLM**, not written in `app/agent.py`.

**Why it's inert today (accepted, known risk).** `app/agent.py:generate_node`
sends a ~20-token system instruction (`"Answer using only the provided
context…"`) with no tools and no few-shot, and puts the *variable* retrieved
context in the user message. That stable prefix is far below every provider
minimum above (1024 / 2048 / 4096), so prompt caching engages **never** until the
prefix is grown — instructions + tools + few-shot moved ahead of the variable
context. Ship prompt caching as groundwork and **measure `cached_tokens` before
claiming savings** (acceptance criterion). Also note the silent-invalidator trap:
nothing dynamic (timestamps, ids) may precede the breakpoint, or the prefix
changes every request and `cached_tokens` stays 0.

## 6. Metrics — surfacing cache metadata that `chat()` currently discards

`app/gateway.py:chat()` returns `resp.choices[0].message.content` and throws away
the response object — which is exactly where LiteLLM puts cache metadata
(response headers like `x-litellm-cache-key` / a `cache_hit` header, and
`usage.prompt_tokens_details.cached_tokens` for prompt caching). Two ways to get
it onto spans:

**Caveat — `_hidden_params` is NOT available over the OpenAI SDK.** `_hidden_params`
is an attribute LiteLLM's **own Python SDK** adds to its response objects; when the
app calls the proxy through the **OpenAI** SDK (as `app/gateway.py` does), the parsed
body is a plain OpenAI `ChatCompletion` and has **no** `_hidden_params`. Over the
OpenAI SDK the cache signal must come from the **response headers** (hence
`.with_raw_response`, which exposes `raw.headers`). The example's
`hidden.get("cache_hit") or headers.get(...)` keeps the `_hidden_params` branch only
as a harmless no-op fallback. **Also confirm the exact hit header against the pinned
image:** keying hit-detection on the mere *presence* of `x-litellm-cache-key` risks a
false positive if that header is emitted on misses too — prefer an explicit
`x-litellm-cache-hit` (or equivalent) boolean header if the pinned build provides one.

- **(a) Widen `chat()`** to expose the cache metadata to the caller. Concretely,
  switch the chat call to the OpenAI SDK's
  `_client.chat.completions.with_raw_response.create(...)` form so both the parsed
  body (`raw.parse()`) and the headers (`raw.headers`) are available, then attach
  `cache.hit` (bool), `cache.layer` (`prompt`|`exact`|`semantic`),
  `cache.tokens_saved` (int), and `cache.cost_saved_usd` (float, from a per-model
  price table) as span attributes on the `generate` span. **Required** for
  per-request span attributes. (This mirrors the [guardrails](../09-guardrails/design.md)
  `with_raw_response` approach — coordinate if both land.)
- **(b) Scrape LiteLLM `/metrics`** out-of-band for a hit-rate dashboard. Needs no
  app change but gives aggregate-only data — no per-request attribution.

The README is explicit: per-request span attributes need (a); a hit-rate
dashboard can use (b) alone. Estimated `$` savings comes from a small per-model
price table (`app/pricing.py`); hit-rate is a dashboard metric, not just a log
line.

**Distinguishing the layer:** an exact/semantic hit shows up as a LiteLLM
cache-hit flag in `_hidden_params` / headers; a prompt-cache discount shows up as
`cached_tokens > 0` on an otherwise-live call. So `cache.layer` is derived:
LiteLLM-cache-hit ⇒ `exact`|`semantic` (by configured `type`); else
`cached_tokens > 0` ⇒ `prompt`; else miss.

## 7. Fail-open — LiteLLM-controlled, so it must be proven not assumed

`app/retrieval.py`'s embedding cache fails open by an explicit app-side
`try/except` around every Redis call. The gateway response cache is **different**:
fail-open is **LiteLLM-controlled** — if Redis is unreachable, LiteLLM treats the
request as a miss and calls the provider. Because the app is not in control of
that behavior, the spec requires it be **proven by test** (Redis stopped → request
still succeeds, every call a miss), not assumed by analogy to `retrieval.py`.

## 8. Data staleness / invalidation (applies to exact-match too, not just semantic)

RAG answers embed retrieved document content. Re-ingestion
(`app/ingest.py` does `TRUNCATE documents … ` then re-inserts) can make a cached
answer **wrong** even when the question is byte-identical — so this is an
exact-match concern, not only a semantic one. Documented invalidation rules:
- **TTL expiry** — `cache_params.ttl` bounds the blast radius of any stale entry.
- **Prompt-prefix change busts prompt caching** — any byte change in the stable
  prefix invalidates it (this is automatic, by construction).
- **Re-ingestion bump for exact + semantic** — a `cache_params.namespace` (or a
  version key) bumped when `app/ingest.py` runs invalidates all response-cache
  entries built on the old corpus. Open question (README): is a global TTL enough,
  or is an ingestion-triggered namespace bump needed? Default conservative: short
  TTL now; wire the bump as the documented escape hatch.

## 9. Net ROI — measure break-even before defaulting semantic on

A semantic lookup itself costs **one embedding call + a vector search** on every
request (hit or miss). At low hit rates this is *slower and more expensive* than
no cache. Plus the lookup embedding routes back through the gateway and bills the
provider. So: measure the break-even hit-rate empirically before enabling semantic
anywhere. Exact-match has no such per-request lookup cost (it is a Redis GET on a
hash), which is the other reason it can be on by default and semantic cannot.

## 10. Edge cases checklist

- **Embeddings path caching.** `app/gateway.py:embed()` also goes through the
  gateway. Exact-match caching of embedding calls is harmless and cheap (identical
  text → identical vector); confirm `cache_params` covers the `embeddings` alias or
  scope it to chat only. Note the `redis-semantic` lookup *uses* the `embeddings`
  alias — don't let semantic-caching the embeddings endpoint recurse.
- **`drop_params: true` interaction.** The gateway already drops provider-incompatible
  params. Decide whether `temperature`/`max_tokens` are part of the cache key
  (param-sensitivity open question). Default: include cache-affecting params in the
  key; if matching on prompt-embedding alone is acceptable, document it.
- **Streaming.** `chat()` is non-streaming, so the cached value is a complete
  response — no partial-stream caching concerns.
- **Empty / missing context.** An empty retrieved context still produces a valid
  request; it caches like any other. No special handling.
- **Within-eval-run self-hit.** Covered by the caching-off eval config (§3) — there
  is no cache to self-hit during an eval run.
