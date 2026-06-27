---
title: Model failover / resilience
slug: model-failover
area: gateway
tier: Next
size: M
status: Todo
depends_on: []
issue:        # set to the GitHub issue number when created
---

# Model failover / resilience

> **Area** `gateway` · **Tier** `Next` · **Size** `M` · **Status** `Todo` · **Depends on:** —

## Summary

Make the `chat` path survive a single provider/region outage by giving its
gateway alias more than one deployment of the **same** model — Anthropic direct
plus Bedrock (`anthropic/claude-sonnet-4-6` + `bedrock/anthropic.claude-sonnet-4-6`,
identical weights, identical behavior contract) — load-balanced active/active,
with an explicit ordered `fallbacks` chain as the backstop. Add per-request and
metric-level visibility into which deployment actually served a request, and tune
LiteLLM retries/cooldowns so a dead deployment is ejected rather than retried on
the hot path. The embeddings path is treated separately and deliberately (see
Open questions) because its failure mode is different and more dangerous than
chat's.

## Problem / Motivation

A single provider/region outage takes the app down even though the gateway seam could route around it. We have no redundancy and no visibility into which deployment served a request.

## Goals

- Same-model, multi-provider redundancy for the `chat` alias in `litellm_config.yaml`
  (Anthropic + Bedrock on `claude-sonnet-4-6` — identical weights) as active/active
  load balancing.
- Ordered `fallbacks` for an explicit primary->standby chain.
- Surface the served deployment per request and in metrics, so standby activation
  is observable rather than silent.
- Configure retries + cooldowns so an unhealthy deployment is ejected from
  rotation instead of being retried on every hot-path request.

## Non-goals

- Cross-*model* fallback to an unvetted model (separate, deliberate decision — a
  different model is a different behavior contract).
- Multi-region data residency (see data-residency).
- Failing **embeddings** over to a *different* embedding model. The stored pgvector
  index lives in one model's embedding space; a different model produces vectors in
  a different space, so a "standby" embedding model silently returns garbage
  retrievals. Embeddings redundancy, if pursued, must use the *same* embedding
  model from a second provider (see Open questions).

## Proposed design

> Companion docs: [`design.md`](./design.md) (architecture diagram, alternatives
> considered, interface sketch, edge cases), [`examples/`](./examples/) (concrete,
> illustrative config + code + tests against this codebase), and
> [`testing.md`](./testing.md) (how each acceptance criterion is proven and gates
> merge).

Lives entirely behind the **gateway seam** (`gateway/litellm_config.yaml` +
`.env` + `docker-compose` env), with one explicitly-scoped, optional app-side
enhancement for observability.

Architecture at a glance (full diagram in [`design.md`](./design.md)):

```
app (chat alias) ─► litellm router ─► chat: { chat-anthropic, chat-bedrock }   (1) active/active
                                   └► chat-standby: { chat-bedrock }           (2) pin + fallback
                       router_settings: retries/allowed_fails/cooldown         (3) ejection
                                        fallbacks: chat ─► chat-standby
```

1. **Active/active load balancing (same model).** Add a second `model_list` entry
   that shares `model_name: chat`. LiteLLM's router balances across all
   deployments of an alias. Both deployments must be the *same* model so behavior
   is unchanged. Give each deployment an explicit `model_info.id` so the
   served-deployment signal is a **stable, human-readable string** rather than the
   per-process UUID LiteLLM auto-generates (unstable ids make `x-litellm-model-id`
   useless for assertions and metric labels):
   ```yaml
   - model_name: chat
     litellm_params: { model: anthropic/claude-sonnet-4-6, api_key: os.environ/ANTHROPIC_API_KEY }
     model_info: { id: chat-anthropic }
   - model_name: chat
     litellm_params: { model: bedrock/anthropic.claude-sonnet-4-6, aws_region_name: os.environ/AWS_REGION_NAME }
     model_info: { id: chat-bedrock }
   ```
   Note that (1) load-balances within a single alias and therefore cannot *pin* a
   request to one specific deployment; pinning (needed by the eval gate and the
   no-AWS smoke test) is only possible via a **distinct alias** as in (2).
2. **Ordered fallback (explicit primary→standby).** Distinct from (1): expose the
   standby deployment under its **own** `model_name` so it is individually
   addressable, then wire an ordered chain in `fallbacks`. The standby entry is the
   *same model* as one of the `chat` deployments, simply re-aliased so it can be
   pinned (load-balancing across the `chat` alias cannot pin to one deployment —
   see the note on (1)). Concretely, the standby is a **third** `model_list` entry:
   ```yaml
   - model_name: chat-standby            # same model as a chat deployment, re-aliased for pinning
     litellm_params: { model: bedrock/anthropic.claude-sonnet-4-6, aws_region_name: os.environ/AWS_REGION_NAME }
     model_info: { id: chat-bedrock }    # same model_info.id => same served-deployment label
   ```
   and the chain is `fallbacks: [{"chat": ["chat-standby"]}]`. (1) handles load
   spread across the `chat` alias; (2) handles "the whole `chat` class of
   deployments is down, go here next." **Where `fallbacks` lives:** keep it in the
   *same* settings block as the other router knobs in (3) (`router_settings`) so all
   failover behaviour is co-located and lint-checkable in one place; LiteLLM also
   accepts it under `litellm_settings`, but mixing the two blocks for related
   behaviour is an avoidable footgun. The config-lint test asserts whichever block
   is chosen actually contains the chain. **Caveat:** lint asserting *presence* does
   not prove the OSS proxy actually *reads* `fallbacks` from that block — the proxy
   has historically honored `fallbacks` under `litellm_settings`, and support under
   `router_settings` must be confirmed against the pinned `main-stable` image. The
   runtime smoke test (acceptance criterion "Ordered fallback") is the real proof
   the chain fires; if it does not fire with `fallbacks` under `router_settings`,
   move it to `litellm_settings`. Do not treat the lint check alone as sufficient.
   - **No-AWS test variant:** in the no-AWS smoke path the standby is a second
     OpenAI deployment, not Bedrock — same `model_list`/`router_settings`/`fallbacks`
     *structure*, just OpenAI `litellm_params`. **Note it deliberately collapses the
     `chat` alias to a single (dead) deployment + `chat-standby`** rather than
     production's active/active two `chat` entries: if a *second healthy* `chat`
     deployment existed, the router would serve from it and the smoke assertion
     "served == standby" would never fire. The single-dead-`chat` shape forces the
     ordered-fallback (2) path, which is exactly what the smoke test proves. Bedrock
     is the documented
     production example. **The broken primary and the working standby must carry
     *distinct* `model_info.id`s** (e.g. `chat-primary` vs `chat-standby-oss`) — the
     entire smoke-test assertion is "served id == standby id", which is meaningless
     if both deployments share an id. (This differs from the production Bedrock
     example, where the standby intentionally *shares* `chat-bedrock` because it is
     the same deployment re-aliased.)
3. **Health / ejection.** Set `router_settings` so a failing deployment is put in
   cooldown and removed from rotation, rather than re-tried inline on every request
   (which would turn an outage into a latency incident). Use concrete, testable
   starting values (tune later): `num_retries: 1`, `allowed_fails: 1`,
   `cooldown_time: 30`. These make the smoke test deterministic — one failure
   crosses the threshold and the deployment is ejected for a bounded window.
4. **Served-deployment visibility.** Two avenues, with different licensing:
   - **OSS-guaranteed (required path):** the gateway returns `response.model` and
     an `x-litellm-model-id` response header on the OSS image. This is the
     authoritative served-deployment signal the smoke test and OTel span rely on.
     **Caveat:** today `app/gateway.chat()` returns only
     `resp.choices[0].message.content` — it discards `resp.model` and the raw
     response, so the served deployment is *not* observable through the current
     `chat()` signature. Reading it requires either (a) the app-side change below,
     or (b) the smoke test using the OpenAI SDK's `.with_raw_response` to read the
     `x-litellm-model-id` header / `resp.model` directly. The test plan uses one of
     these explicitly — it is **not** assumed to fall out of the current `chat()`.
   - **Enterprise-gated (best-effort, not an acceptance gate):** the Prometheus
     callback (`litellm_settings.callbacks: ["prometheus"]`, scraped at `/metrics`)
     that labels the served deployment and counts fallback activations is a
     **LiteLLM Enterprise-only** feature; the OSS `main-stable` image used here does
     not expose it. So Prometheus metrics are documented as the production-shaped
     option but are **not** required to satisfy acceptance (see Open questions /
     Risks).
   - **App-side OTel (the concrete change):** make `app/gateway.chat()` capture the
     served deployment and record it so the agent's `generate` span shows the
     *served* deployment instead of the hardcoded alias `"chat"` (see `app/agent.py`,
     which currently hardcodes `gen_ai.request.model: "chat"`). **Do this without
     changing `chat()`'s return contract.** `chat()` returns a bare `str` today and
     has two callers — `app/agent.py:generate_node` and `app/evals.py:judge_score` —
     both of which use the value as a string; returning a `(content, model)` tuple (or
     similar) silently breaks both call sites. The non-breaking implementation is for
     `chat()` to read `resp.model` / the `x-litellm-model-id` header off the raw
     response (via `_client.chat.completions.with_raw_response.create(...)`) and write
     it onto the **enclosing** OTel span with
     `trace.get_current_span().set_attribute("gen_ai.response.model", served_id)` —
     `chat()` keeps returning `str`, and the span created in `generate_node` is
     annotated in place. This is the OSS-native way to make standby activation
     queryable without an enterprise license.
5. **Param reconciliation.** Keep `litellm_settings.drop_params: true` so a param
   unsupported by one provider is dropped rather than 400-ing. (Scope: `drop_params`
   only *drops unsupported params*; it does **not** reconcile differing
   `max_tokens` ceilings, tokenizers, or response schemas — those are equal here
   only because both deployments are the same model.)

Config/env changes: new `model_list` entries (each with `model_info.id`) plus a
third `chat-standby` entry (same model re-aliased for pinning/fallback);
`router_settings` block (`num_retries`/`allowed_fails`/`cooldown_time` and the
ordered `fallbacks` chain, co-located); a small `app/gateway.py` + `app/agent.py` change to thread the
served deployment into the `generate` span; Prometheus callback only if an
enterprise license is present; new env vars (`ANTHROPIC_API_KEY`, and for Bedrock
`AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY` / `AWS_REGION_NAME`) added to
`.env.example` and the `litellm` service in `docker-compose.yml`. No schema or DB
migration.

## Acceptance criteria

- [ ] **Same-model redundancy:** the `chat` alias resolves to ≥2 deployments of
  the *same* model, and the router confirms both at startup (config lint / router
  introspection). **What the committed `gateway/litellm_config.yaml` ships as
  (resolves the lint-vs-first-run tension below):** the default committed config is
  **two OpenAI `chat` entries** (same `openai/gpt-4o-mini`, distinct
  `model_info.id`s) + an OpenAI `chat-standby`, so the unconditional config-lint
  (`test_chat_alias_has_two_plus_same_model_deployments`, which reads the live
  `gateway/litellm_config.yaml`) passes **and** the "set `OPENAI_API_KEY` only"
  first run still works with no Anthropic/AWS creds. The **Anthropic-direct +
  Bedrock** cross-provider layout in [`examples/litellm_config.prod.yaml`](./examples/litellm_config.prod.yaml)
  is the documented **production swap-in** (true multi-*provider* redundancy), not
  the committed default. (Two same-provider entries exercise the router /
  ejection / fallback mechanism without proving cross-provider redundancy; that is
  an accepted, documented limitation of the OPENAI-only default — see Risks.)
- [ ] **Ordered fallback demoed with a defined kill mechanism:** with the primary
  deployment forced to fail via the documented, config-only kill mechanism
  (point its `api_base` at a dead host *or* set an invalid key for that
  deployment), a `chat` request still returns a 200 served by the standby, proven
  by a scripted smoke test — not a manual click-through.
- [ ] **Provider/deployment changes need no app code** — adding/swapping a
  deployment is a `litellm_config.yaml` + `.env` edit only. (Scope: this criterion
  is about provider choice; it does **not** cover served-model observability,
  which may require the optional `app/gateway.py` change below.)
- [ ] **Served deployment is observable without log-grepping (OSS path is the
  gate):** the gateway exposes the served deployment per request via
  `x-litellm-model-id` / `response.model`, and each deployment has a stable
  `model_info.id` so that signal is a readable name, not a per-process UUID. The
  smoke test reads this signal via `.with_raw_response` (or via the app-side
  change) — **not** via the current `chat()` return value, which discards it. The
  served deployment is recorded on the agent's `generate` OTel span as
  `gen_ai.response.model` (set from inside `chat()` via
  `trace.get_current_span().set_attribute(...)`, leaving `chat()`'s `str` return
  contract and both its call sites — `app/agent.py`, `app/evals.py` — unchanged) so
  standby activation is queryable in any OTel backend. The hardcoded
  `gen_ai.request.model: "chat"` stays (it is the *requested* alias); the
  *served* deployment is the new attribute. *Prometheus `/metrics` is NOT part
  of this gate* — it is LiteLLM Enterprise-only and unavailable on the OSS image
  (see Risks); if an enterprise license is present it MAY additionally be wired,
  but acceptance does not depend on it.
- [ ] **Unhealthy deployment is ejected, not hot-path-retried:** `num_retries: 1`,
  `allowed_fails: 1`, `cooldown_time: 30` are configured. The smoke test makes
  ejection observable deterministically by **driving requests until the failure
  threshold is crossed** (do not hardcode "exactly one failure ejects" — LiteLLM's
  `allowed_fails` boundary semantics, ejection-on-Nth vs ejection-after-N, are not
  assumed; the test loops until ejection is observed, then asserts), after which
  subsequent `chat` requests within the cooldown window are all served by the
  standby (asserted via `x-litellm-model-id`) and do **not** incur the dead-host
  retry/timeout latency — i.e. ejection is proven by *who served the request*, not
  by inspecting opaque router internals.
- [ ] **`drop_params` proven, scope honored:** `drop_params: true` is set
  (config-lint), and the smoke test confirms a chat request carrying an extra param
  succeeds (no 400). **Scope caveat (accepted):** the no-AWS smoke path is
  *single-provider* (OpenAI only), so it cannot exercise a genuinely
  *provider-unsupported* param — there is no second provider to diverge from. The
  smoke test therefore proves only "`drop_params` is on and a benign extra param
  does not 400"; true cross-provider param dropping is exercised only in the
  production Anthropic+Bedrock config (`examples/litellm_config.prod.yaml`) and is
  recorded as an accepted, production-only check, not a CI gate. Documented that
  `drop_params` does not reconcile semantic differences
  (max_tokens/tokenizer/schema).
- [ ] **Eval gate still passes against the standby:** running the existing eval
  gate pinned to the standby passes its threshold, proving the standby is a viable
  served model and not a silent quality regression. **Pinning mechanism is
  concrete:** the standby is exposed under its own alias (`chat-standby`) and the
  gate is pointed at it by setting `CHAT_MODEL=chat-standby` (read by
  `app/config.py` → `settings.chat_model`). Note this co-pins the LLM-as-judge in
  `app/evals.py` (it also calls `chat()`); acceptable because the standby is the
  *same model*, but recorded so it is not a surprise.
- [ ] **Embeddings SPOF explicitly resolved:** the embeddings strategy is recorded
  as either (a) accepted out of scope with rationale, or (b) a *same-model*
  embeddings standby (e.g. Azure OpenAI `text-embedding-3-small`) — never a
  different embedding model.

## Dependencies

- None

## Open questions

- **Bedrock setup burden for the demo.** True same-model multi-provider redundancy
  needs Anthropic + Bedrock (or Vertex), and Bedrock requires an AWS account with
  per-model Bedrock access explicitly enabled plus AWS creds in env — heavyweight
  for a "set `OPENAI_API_KEY` only" first-run sandbox. Decide: do we (a) require
  AWS for the demo, or (b) prove the *fallback mechanism* with a no-AWS path
  (primary = real deployment, standby = a second real deployment or a deliberately
  broken primary) and document Bedrock as the production-shaped example? Leaning
  (b) for the smoke test so CI/devs don't need AWS.
- **Embeddings strategy:** out of scope (a) or same-model standby via Azure OpenAI
  (b)? Note `text-embedding-3-small` is served by both OpenAI and Azure OpenAI, so
  (b) is feasible without changing the vector space.
- **Observability surface:** the LiteLLM Prometheus `/metrics` endpoint is
  **Enterprise-only** and absent from the OSS `main-stable` image we run, so it
  cannot be the acceptance gate. Resolved: the required signal is
  `x-litellm-model-id` (with stable `model_info.id`) threaded into the app's OTel
  `generate` span. This does require the small `app/gateway.py` change — so a
  strict reading of "app code unchanged" is relaxed *for observability only*
  (provider choice itself still needs no app change; see the scope note on that
  criterion). Open sub-question: ship the OTel threading as part of this feature,
  or leave the smoke test reading `x-litellm-model-id` via `.with_raw_response` and
  defer the span change? Leaning: ship the span change — it is the OSS-native way to
  make standby activation queryable and is a few lines.

## Risks & mitigations

- **(Highest) Embedding-space incompatibility.** Failing embeddings over to a
  *different* model silently corrupts retrieval (vectors land in a different
  space). Mitigation: embeddings standby must be the *same* model from a second
  provider, or no embeddings failover at all. Captured as a non-goal + open
  question.
- **Dead-deployment latency incident.** Without cooldowns, every request retries
  the dead primary, converting an outage into a latency spike. Mitigation:
  `allowed_fails` + `cooldown_time` so the bad deployment is ejected.
- **Hidden setup cost (Bedrock).** Per-model access enablement + AWS creds.
  Mitigation: provide a no-AWS smoke-test path; keep Bedrock as the documented
  production example.
- **Cross-provider behavior drift.** Only *same-weights* deployments count as
  "same model"; anything else is cross-model fallback (a deliberate, separate
  decision — non-goal here).
- **Cost.** Active/active doubles provider exposure/spend. Mitigation: make the
  standby fallback-only (ordered `fallbacks`) rather than load-balanced, or weight
  deployments, if cost matters more than spread.
- **Prometheus is Enterprise-gated.** The `prometheus` callback / `/metrics`
  endpoint is a LiteLLM Enterprise feature and is not available on the OSS
  `ghcr.io/berriai/litellm:main-stable` image this stack uses. Mitigation: do not
  make metrics an acceptance gate; use the OSS-guaranteed `x-litellm-model-id`
  header + the app-side OTel span as the observability path. Prometheus stays as
  the documented production-with-enterprise option only.
- **Smoke test needs live infra + a real key.** Like the existing eval gate
  (`tests/test_evals.py` → real model calls), the failover smoke test requires a
  running `litellm` container and a real `OPENAI_API_KEY`; it does **not** mock the
  gateway. **Correction (verified against the code):** `tests/test_evals.py` has
  **no** skip guard today — it calls `run()` unconditionally and would error/fail
  without a gateway + key. So the smoke test's skip behaviour is **net-new, not a
  mirror of an existing pattern**: this feature must add an explicit guard (e.g. a
  `pytest.mark.skipif` / fixture probing `OPENAI_API_KEY` and gateway reachability)
  in `tests/`. Recommend adding the same guard to the standby-pinned eval-gate run
  (acceptance criterion "Eval gate still passes against the standby"), which
  otherwise inherits the existing test's hard-fail-without-infra behaviour. Keep
  both in the no-AWS path so they never need Bedrock creds. Note: there is no
  `.github/workflows` in the repo yet — CI wiring is the separate `07-ci-hardening`
  spec; this feature ships the tests runnable via `pytest`/`make`, and CI picks them
  up later.
- **Accepted (sandbox):** `LITELLM_MASTER_KEY` default (`sk-sandbox-master`) — fine
  for the sandbox, not for production.
- **Accepted — committed default config proves the *mechanism*, not cross-provider
  redundancy.** The unconditional config-lint reads the live
  `gateway/litellm_config.yaml` and requires ≥2 `chat` deployments, while the
  sandbox must still run on `OPENAI_API_KEY` only. These are reconciled by shipping
  the default as **two OpenAI `chat` entries + OpenAI `chat-standby`** (same model,
  distinct `model_info.id`s). That exercises routing/load-balancing, ejection, and
  the fallback chain with no Anthropic/AWS creds — but it does **not** prove true
  multi-*provider* redundancy, which only the Anthropic+Bedrock production swap
  (`examples/litellm_config.prod.yaml`) delivers. Cross-provider redundancy is
  therefore a documented production property, not a CI-gated one.
- **Accepted — `drop_params` cross-provider behavior is not CI-proven.** The no-AWS
  smoke path is single-provider (OpenAI), so it cannot exercise a
  *provider-unsupported* param; the smoke test proves only that `drop_params` is on
  and a benign extra param does not 400. Genuine cross-provider param dropping is a
  production-only check against the Anthropic+Bedrock config (see acceptance
  criterion "`drop_params` proven").
- **Accepted (Low) — config-lint hardcodes the `fallbacks` block.**
  `test_ordered_fallback_chain_present_and_colocated` asserts the chain under
  `router_settings`. If runtime testing shows the OSS proxy only honors `fallbacks`
  under `litellm_settings` (see the §2 caveat), the chain **and** this lint
  assertion must move together — the runtime smoke test, not the lint, is the
  authority on whether the chain actually fires.
- **Accepted (Low) — illustrative Bedrock model id.** `bedrock/anthropic.claude-sonnet-4-6`
  in the snippets is illustrative shorthand; real Bedrock ids are versioned/region-
  qualified (e.g. `anthropic.claude-3-5-sonnet-20241022-v2:0`-style). The
  production config must use the actual Bedrock model id for the chosen region, and
  "identical weights" must be confirmed against the specific Anthropic-direct
  version. Does not affect the no-AWS smoke path. To be pinned in `design.md` /
  `examples/`.
- **Accepted (Low) — cooldown recovery not tested.** The smoke test proves ejection
  within the `cooldown_time: 30` window; it does **not** assert the ejected
  deployment re-enters rotation after the window expires (would add ~30s of test
  wall-clock for little signal). Acceptable; noted so the gap is explicit.
- **Accepted — partial overlap between (1) and (2).** With both Anthropic and
  Bedrock under the `chat` alias, the router already fails over *between them* on
  errors (driven by `num_retries` + ejection), so the ordered `fallbacks` chain to
  `chat-standby` is partly redundant in the two-provider case. It earns its keep
  when the *entire* `chat` class is unhealthy (e.g. a bad shared config or a region
  both providers depend on) and as the **pinning** handle the eval gate and smoke
  test need. Kept deliberately; not removed. The smoke test asserts failover by the
  *served deployment id* (`x-litellm-model-id` == the standby's `model_info.id`),
  which is satisfied by either mechanism, so the test does not depend on
  distinguishing them.

## Test & rollout plan

- **Config/unit:** lint `litellm_config.yaml`; assert the router resolves ≥2
  deployments for `chat`, that each has a stable `model_info.id`, that a
  `chat-standby` alias exists as its own `model_list` entry, that the ordered
  `fallbacks` chain (`chat`→`chat-standby`) is present, and that `router_settings`
  (`num_retries`, `allowed_fails`, `cooldown_time`) are present with the documented
  values. These
  run without any network/provider and so are CI-safe unconditionally.
- **Integration smoke test (new, in `tests/`):** stand up the gateway with a
  `chat` primary and a `chat-standby` alias (no-AWS path: both OpenAI deployments,
  or primary deliberately broken), force the primary to fail via the documented
  kill mechanism (dead `api_base` or invalid key for that deployment), and assert
  failover. **Read the served deployment from the raw response, not from
  `chat()`** (which returns only the message string): use the OpenAI SDK's
  `client.chat.completions.with_raw_response.create(...)` to read the
  `x-litellm-model-id` header / `resp.model` and assert it equals the standby's
  `model_info.id` (primary and standby must have **distinct** ids in this path).
  Then assert ejection: with `allowed_fails: 1` / `cooldown_time: 30`, **loop
  requests until ejection is observed** (do not assume the exact failure count that
  trips it), then assert every follow-up request inside the window is served by the
  standby and returns without the dead-host retry latency. Add an **explicit** skip
  guard (probe `OPENAI_API_KEY` + gateway reachability) — there is no existing skip
  pattern in `tests/` to inherit. Runs without AWS.
- **Eval gate:** run the existing eval gate pinned to the standby deployment; it
  must pass — proving the standby is a viable served model.
- **Rollout:** config-only, no migration. The committed default config is
  **active/active OpenAI** (two `chat` entries + `chat-standby`, all
  `openai/gpt-4o-mini`), so first run still needs only `OPENAI_API_KEY` and the
  unconditional config-lint passes. Multi-*provider* redundancy (Anthropic +
  Bedrock) is opt-in by swapping in the `examples/litellm_config.prod.yaml` entries
  and adding the provider/AWS keys. Ship `.env.example` + `docker-compose.yml` env
  additions alongside the config.

## References

- [Design notes](./design.md) — architecture, alternatives, interface sketch, edge cases
- [Examples](./examples/) — illustrative config, app-side change, and tests (not wired in)
- [Testing plan](./testing.md) — proof + merge gate for each acceptance criterion
- [Feature roadmap](../../ROADMAP.md)
- [Specs index](../README.md)
- [Data residency](../16-data-residency/README.md) — depends on this spec (multi-region builds on the failover seam)
- [CI hardening](../07-ci-hardening/README.md) — wires the smoke test + standby eval into CI
