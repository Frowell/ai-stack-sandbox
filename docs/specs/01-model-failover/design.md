# Model failover — design notes

Deeper design notes that back the spec in [`README.md`](./README.md). This file
covers the architecture, the alternatives weighed (and why rejected), interface
sketches, and the edge cases that the [acceptance criteria](./README.md#acceptance-criteria)
and [`testing.md`](./testing.md) turn into assertions. It does **not** ship code —
the illustrative snippets live in [`examples/`](./examples/).

## Where this lives (the seam)

```
                    app code (NEVER names a provider)
   app/agent.py ── generate node ──► app/gateway.chat(messages)
   app/evals.py ── judge ──────────► app/gateway.chat(messages)        ── alias: settings.chat_model ("chat")
   app/retrieval.py ── _cached_embed ► app/gateway.embed(texts)        ── alias: settings.embedding_model ("embeddings")
        │
        │  OpenAI-compatible protocol, base_url=GATEWAY_BASE_URL
        ▼
   ┌─────────────────────── litellm (gateway/litellm_config.yaml) ───────────────────────┐
   │  model_list:                                                                         │
   │    chat ──► [ chat-anthropic (anthropic/claude-sonnet-4-6) ]  ┐ active/active        │
   │            [ chat-bedrock   (bedrock/...claude-sonnet-4-6) ]  ┘ load balance (1)     │
   │    chat-standby ──► [ chat-bedrock (same model, re-aliased for pinning) ]            │
   │  router_settings:                                                                    │
   │    num_retries / allowed_fails / cooldown_time          (3) health + ejection       │
   │    fallbacks: [{"chat": ["chat-standby"]}]              (2) ordered primary→standby  │
   │  litellm_settings: { drop_params: true }                (5) param reconciliation     │
   └─────────────────────────────────────────────────────────────────────────────────────┘
        │                                   │
        ▼                                   ▼
   Anthropic API                       AWS Bedrock
   (claude-sonnet-4-6)                 (anthropic.claude-sonnet-4-6)
```

Everything except the **observability thread** (the `app/gateway.py` +
`app/agent.py` change in §4 of the README) is confined to
`gateway/litellm_config.yaml`, `.env`, and `docker-compose.yml`. The app keeps
calling the `chat` alias and never learns a provider name — that is the whole
point of the gateway seam (see `app/gateway.py` docstring).

## Two mechanisms, deliberately kept distinct

The spec keeps **load balancing (1)** and **ordered fallback (2)** separate
because they answer different questions and have different addressability:

| | Mechanism | Answers | Can it pin one deployment? |
|---|---|---|---|
| (1) | Two `model_list` entries sharing `model_name: chat` | "spread load / survive *one* deployment failing" | **No** — the router picks within the alias |
| (2) | A third entry `chat-standby` + `fallbacks` chain | "the whole `chat` class is down, go *here* next" | **Yes** — `chat-standby` is its own alias |

Pinning matters for two consumers that are not load-balancing concerns:

- the **eval gate**, which must run *against the standby specifically* to prove
  it is a viable served model (`CHAT_MODEL=chat-standby`), and
- the **smoke test**, which must assert *which* deployment served the request.

A single load-balanced alias cannot express either, so `chat-standby` exists as
a re-alias of the same Bedrock deployment (same `model_info.id`, so the
served-deployment label is identical whether the router reached it via (1) or
(2)). This is the "accepted partial overlap" risk in the README: with two
providers under `chat`, the router already fails over between them; `chat-standby`
earns its keep as the pin handle and as the backstop when the entire class is
unhealthy.

## Served-deployment signal: why `model_info.id` must be explicit

LiteLLM auto-assigns each deployment a per-process UUID as its model id. That
UUID changes every time the container restarts, so:

- metric labels and span attributes built from it are not comparable across runs;
- the smoke test cannot assert `x-litellm-model-id == <known value>`.

Setting `model_info: { id: chat-bedrock }` makes the id a **stable,
human-readable string**. Both the Bedrock `model_list` entry under `chat` and the
`chat-standby` entry carry the *same* `id: chat-bedrock`, so "served by Bedrock"
reads identically regardless of which routing mechanism delivered the request.

## Interface sketch — the one app-side change

The README is explicit that today `app/gateway.chat()` discards the served
deployment:

```python
# app/gateway.py — current
def chat(messages: list[dict], **kwargs) -> str:
    resp = _client.chat.completions.create(model=settings.chat_model, messages=messages, **kwargs)
    return resp.choices[0].message.content or ""   # resp.model and headers thrown away
```

Three options were considered for threading the served deployment into the
`generate` span:

1. **Change the return type** of `chat()` to `(content, served_model)` or a
   dataclass. *Rejected:* breaks both call sites (`app/agent.py`,
   `app/evals.py` judge) and ripples the signature change through the codebase
   for an observability-only need.
2. **Smoke test reads the raw response itself** via the OpenAI SDK's
   `with_raw_response`, leaving `chat()` untouched. *Viable, and it is the
   fallback the README documents* — but it makes standby activation queryable
   only inside the test, not in production traces.
3. **`chat()` reads the served deployment and sets it on the current OTel span**
   (no signature change). *Chosen for the illustrative example.* The `generate`
   node already opens a `span("generate", ...)`; `chat()` runs inside it, so
   `gateway.chat()` can read `x-litellm-model-id` / `resp.model` and call
   `set_attribute("gen_ai.response.model", served)` on the active span. Callers
   are unchanged; production traces gain the served deployment.

See [`examples/gateway_served_model.py`](./examples/gateway_served_model.py) and
[`examples/agent_span.py`](./examples/agent_span.py) for the illustrative shape.

### OTel attribute choice: `request.model` vs `response.model`

`app/agent.py` currently hardcodes `gen_ai.request.model: "chat"`. Under OTel
GenAI semantic conventions that is *correct* — the app *requested* the alias
`chat`. The new fact is *which deployment answered*, which is
`gen_ai.response.model`. So the chosen design **keeps** `gen_ai.request.model:
"chat"` (the requested alias) and **adds** `gen_ai.response.model: <served id>`
from the gateway. The README phrasing "replacing the hardcoded
`gen_ai.request.model`" is satisfied in spirit: the hardcoded alias is no longer
the only model attribute on the span; the *served* deployment is now recorded and
queryable.

## Health / ejection semantics (the numbers)

`num_retries: 1`, `allowed_fails: 1`, `cooldown_time: 30` are chosen to make the
smoke test **deterministic**, not because they are production-tuned:

- `allowed_fails: 1` → a single failure crosses the threshold, so the test does
  not have to drive N failures to trigger ejection.
- `cooldown_time: 30` → the dead deployment is removed from rotation for a
  bounded 30s window, long enough that every follow-up request in the test is
  served by the standby (the assertion), short enough that recovery is quick.
- `num_retries: 1` → one retry, which the router routes to a *healthy*
  deployment rather than hammering the dead one inline. This is what converts the
  failure mode from "latency incident" (retry the dead host every request) into
  "ejection" (route around it).

Edge case: if `allowed_fails` were left at its default, the first request would
retry the dead host before crossing the threshold, and the test's "every
follow-up served by standby" assertion would flake on the boundary request.
Pinning the values removes that nondeterminism.

## Kill mechanism (config-only, for the demo)

Acceptance requires forcing the primary to fail **without app code**. Two
documented, config-only mechanisms:

- **Dead `api_base`:** point the primary deployment's `api_base` at an
  unreachable host (e.g. `http://127.0.0.1:1` or `http://localhost:9/v1`). The
  primary's calls fail at connect; the router ejects it and serves the standby.
- **Invalid key for that deployment:** give the primary deployment a bad
  `api_key` so it 401s. Same downstream effect.

The smoke test uses one of these in a **dedicated CI config** (see
[`examples/litellm_config.no-aws.yaml`](./examples/litellm_config.no-aws.yaml)) so
the production config is never mutated. Dead `api_base` is preferred because it
fails fast at connect rather than after a provider round-trip.

## No-AWS path

Bedrock needs an AWS account with per-model access enabled plus creds — too heavy
for a "set `OPENAI_API_KEY` only" sandbox and for CI. The smoke test therefore
runs a **no-AWS** config: primary = a real OpenAI deployment with a dead
`api_base`, standby = a healthy OpenAI deployment. The `model_info.id` labels, the
`router_settings`, and the `fallbacks` chain are identical to production; only
`litellm_params` differ. **One deliberate structural difference:** the no-AWS
config has a *single* (dead) `chat` deployment + `chat-standby`, not production's
active/active *two* `chat` entries. A second *healthy* `chat` deployment would be
served by the router directly (active/active), so the smoke assertion "served ==
standby" would never fire; collapsing `chat` to one dead deployment forces the
ordered-fallback (2) path, which is what the smoke test proves. Bedrock stays as
the documented production example. This keeps the *mechanism* under test without
requiring AWS in CI.

## Embeddings: why it is out of scope here

The pgvector index (`db/init.sql`, `embedding_dim=1536`) stores vectors in **one
model's embedding space**. Failing embeddings over to a *different* model returns
vectors in a *different* space, so `embedding <=> %s::vector` in
`app/retrieval.dense()` silently ranks by a meaningless distance — corrupt
retrievals with no error. The only safe embeddings standby is the *same* model
from a second provider (e.g. `text-embedding-3-small` on both OpenAI and Azure
OpenAI, which share weights). This is recorded as a non-goal + open question
rather than solved here, because the chat path is the higher-leverage, lower-risk
win and embeddings redundancy deserves its own decision.

## Sequencing / rollout

1. Land `router_settings` (`num_retries`/`allowed_fails`/`cooldown_time`) and
   `drop_params` — inert with a single deployment, so safe to ship first.
2. Add the second `chat` deployment + `model_info.id` labels (active/active).
3. Add the `chat-standby` entry + `fallbacks` chain (the pin handle).
4. Land the `app/gateway.py` + `app/agent.py` observability thread.
5. Add config-lint unit test (CI-safe, no network) + the gated smoke test.
6. `.env.example` + `docker-compose.yml` env additions ride alongside.

Default first run still needs only `OPENAI_API_KEY`: the committed config ships
**active/active OpenAI** — two `chat` entries (same `openai/gpt-4o-mini`, distinct
`model_info.id`s) plus an OpenAI `chat-standby` — so the unconditional config-lint
(`test_chat_alias_has_two_plus_same_model_deployments`, which reads the live
`gateway/litellm_config.yaml`) passes without Anthropic/AWS creds. Multi-*provider*
redundancy (Anthropic + Bedrock, `examples/litellm_config.prod.yaml`) is the opt-in
production swap. Two same-provider entries exercise the routing/ejection/fallback
mechanism but do **not** prove cross-provider redundancy — an accepted limitation
of the OPENAI-only default. No DB migration.

## Open edge cases (tracked, not resolved here)

- **Cooldown expiry mid-test:** if a slow CI box stretches the test past
  `cooldown_time: 30`, the primary re-enters rotation and a late request could be
  served by the (still-dead) primary again, failing on a retry. Mitigation in the
  test: keep the follow-up burst tight, or raise `cooldown_time` in the CI
  config. Noted for `testing.md`.
- **Judge co-pinning:** `CHAT_MODEL=chat-standby` also re-points the LLM-judge in
  `app/evals.py` (it calls the same `chat()`). Acceptable because the standby is
  the *same model*, but it means the eval-on-standby run is not a clean
  system-vs-judge isolation. Recorded so it is not a surprise; PR #1 (decoupled
  judge) would let the judge be pinned independently later.
- **Prometheus is Enterprise-gated:** the `/metrics` deployment label and
  fallback counters are LiteLLM Enterprise-only and absent from the OSS
  `main-stable` image. Not an acceptance gate; the OSS `x-litellm-model-id` +
  OTel span is the required path.
</invoke>
