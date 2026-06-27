# Testing & verification plan — model failover

How each [acceptance criterion](./README.md#acceptance-criteria) is **proven** and
how it **gates merge**. Three layers, matching how this repo already tests
(`tests/test_evals.py` wires the eval gate into `pytest`, run via
`make test` / `uv run pytest -q`):

| Layer | Network? | Gates merge? | Where |
|---|---|---|---|
| **Config-lint (unit)** | No | Yes, unconditionally | `tests/test_failover_config.py` (see [`examples/test_config_lint.py`](./examples/test_config_lint.py)) |
| **Integration smoke** | Yes (gateway + real key) | Yes when key/gateway present; **skips** otherwise | `tests/test_failover_smoke.py` (see [`examples/test_failover_smoke.py`](./examples/test_failover_smoke.py)) |
| **Eval gate on standby** | Yes (gateway + real key) | Yes — reuses the existing gate | `app/evals.py` via `CHAT_MODEL=chat-standby` |

The smoke test deliberately mirrors the existing gate's posture: it does **not**
mock the gateway, and it **skips cleanly** when `OPENAI_API_KEY` or the gateway is
absent (see the `client` fixture). There is no `.github/workflows/` in the repo
yet — CI wiring is the separate `07-ci-hardening` spec; this feature ships tests
runnable via `pytest` / `make test`, and CI picks them up once it exists.

## Fixtures / harness needed

- **No-AWS gateway config** for the smoke test:
  [`examples/litellm_config.no-aws.yaml`](./examples/litellm_config.no-aws.yaml) —
  primary `chat` with a dead `api_base` (the config-only kill mechanism), healthy
  `chat-standby`, same three-entry shape as production. Mounted in place of the
  default config when the smoke test stack is brought up (e.g. a compose override
  or a `LITELLM_CONFIG` path), so the production config is never mutated.
- **Real `OPENAI_API_KEY`** in env (smoke + standby-eval only). No AWS creds
  needed.
- **PyYAML** (or a stdlib loader) for the config-lint test; not currently a dep,
  so it joins the `dev` group.
- For the **standby eval**: the existing stack (Postgres + gateway), ingested
  corpus (`make ingest`), and `CHAT_MODEL=chat-standby`.

## Criterion-by-criterion proof

### 1. Same-model redundancy (`chat` resolves to ≥2 deployments)
- **Proven by** `test_chat_alias_has_two_plus_same_model_deployments` +
  `test_every_deployment_has_stable_model_info_id` (config-lint). Asserts two
  `model_name: chat` entries and that each deployment carries a `model_info.id`.
- **Live confirmation (optional):** the smoke fixture's `client.models.list()`
  shows the router resolved the alias at startup.
- **Gate:** config-lint runs on every PR, no network.

### 2. Ordered fallback demoed with a defined kill mechanism
- **Proven by** `test_ordered_fallback_serves_standby` (smoke). With the primary
  killed via dead `api_base` (config-only), a `chat` request returns **200 served
  by the standby**, asserted on `x-litellm-model-id == "chat-standby-openai"` —
  scripted, not a manual click-through.
- **Static half:** `test_ordered_fallback_chain_present_and_colocated` asserts the
  `fallbacks: [{chat: [chat-standby]}]` chain exists in `router_settings`.
- **Gate:** smoke when key present; config-lint always.

### 3. Provider/deployment changes need no app code
- **Proven by** inspection + the no-AWS vs prod configs differing only in
  `litellm_params` while the app and tests are unchanged. The config-lint test
  reads `gateway/litellm_config.yaml` exclusively; no `app/` change is required to
  add/swap a deployment.
- **Scope note (honored):** this covers *provider choice*. Served-model
  *observability* does need the small `app/gateway.py` change (criterion 4) — that
  is the one explicitly-scoped exception, called out in the README.
- **Gate:** config-lint (it would fail if a provider swap required touching app
  code, because the lint targets only the YAML).

### 4. Served deployment observable without log-grepping (OSS path is the gate)
- **Proven by** the smoke test reading `x-litellm-model-id` via
  `with_raw_response` (`_call_served_by`) — explicitly **not** via `chat()`, which
  discards it. `test_every_deployment_has_stable_model_info_id` guarantees that
  signal is a readable name, not a UUID.
- **App-side thread:** [`examples/gateway_served_model.py`](./examples/gateway_served_model.py)
  sets `gen_ai.response.model` on the active `generate` span. Verified by a unit
  test that runs `chat()` inside a span and asserts the attribute is set (uses an
  OTel in-memory span exporter; no backend needed). See "OTel span unit test"
  below.
- **Explicitly out of gate:** Prometheus `/metrics` is LiteLLM Enterprise-only and
  absent from the OSS `main-stable` image — not asserted anywhere.
- **Gate:** smoke (header) + the OTel span unit test.

### 5. Unhealthy deployment ejected, not hot-path-retried
- **Proven by** `test_unhealthy_deployment_is_ejected_not_retried` (smoke): after
  the first failure crosses `allowed_fails: 1`, every follow-up within
  `cooldown_time: 30` is served by the standby **and** returns in `< 5s` (no
  dead-host retry latency). Ejection is proven by *who served the request*, not by
  inspecting opaque router internals.
- **Static half:** `test_router_ejection_values` asserts
  `num_retries: 1` / `allowed_fails: 1` / `cooldown_time: 30`.
- **Edge case (from design.md):** keep the follow-up burst tight so it stays
  inside the cooldown window on slow CI; otherwise the primary re-enters rotation
  and a late request flakes. Raise `cooldown_time` in the CI config if needed.
- **Gate:** smoke + config-lint.

### 6. `drop_params` proven, scope honored
- **Proven by** `test_drop_params_enabled` (config-lint) for the flag, plus
  `test_drop_params_no_400` (smoke): a chat request carrying an extra param
  succeeds (no 400).
- **Accepted scope limit (single-provider no-AWS path):** the no-AWS smoke config
  is OpenAI-only, so there is no *second* provider to make a param "unsupported" —
  the smoke test can only prove "`drop_params` on + benign extra param does not
  400", **not** genuine cross-provider param dropping. Real provider divergence is
  exercised only in the production Anthropic+Bedrock config and is documented as a
  production-only check, not a CI gate. (Named `test_drop_params_no_400` rather
  than `..._across_deployments` to stay honest about what it proves.)
- **Scope:** documented in README/design that `drop_params` does **not** reconcile
  `max_tokens`/tokenizer/schema — those match only because both deployments are
  the same model.
- **Gate:** config-lint (flag) + smoke (no-400 best-effort).

### 7. Eval gate still passes against the standby
- **Proven by** running the existing gate pinned to the standby:
  ```bash
  make ingest                       # ensure corpus is loaded
  CHAT_MODEL=chat-standby uv run pytest -q tests/test_evals.py
  # or: CHAT_MODEL=chat-standby uv run python -m app.evals
  ```
  `CHAT_MODEL` is read by `app/config.py` → `settings.chat_model`, which
  `app/gateway.chat()` passes as the model. The gate must clear `THRESHOLD = 0.7`
  (`app/evals.py`), proving the standby is a viable served model, not a silent
  quality regression.
- **Recorded caveat (design.md):** this co-pins the LLM-judge (`app/evals.py` also
  calls `chat()`). Acceptable because the standby is the *same model*; PR #1's
  decoupled judge would let the judge be pinned independently later.
- **Gate:** the eval gate is already the merge gate; this just runs it with the
  standby pin.

### 8. Embeddings SPOF explicitly resolved
- **Proven by** documentation, not a test: README non-goals + Open questions +
  design.md record the decision (out of scope, with rationale) and the *only* safe
  alternative (a same-model embeddings standby, e.g. Azure OpenAI
  `text-embedding-3-small`). A negative test guards the trap: a unit test asserting
  the `embeddings` alias is **not** wired to a different embedding model (it stays
  `text-embedding-3-small`), so a future "helpful" cross-model embeddings fallback
  fails the lint.
- **Gate:** config-lint (the negative assertion above).

## OTel span unit test (idiom example)

CI-safe (no network): runs `chat()` under a span with an in-memory exporter and
asserts the served deployment was recorded. Mirrors how the repo keeps the
observability seam optional (`app/observability.py`).

```python
# tests/test_failover_observability.py  (ILLUSTRATIVE)
from opentelemetry import trace
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter


def test_served_deployment_lands_on_generate_span(monkeypatch):
    # IMPORTANT: app/observability.py sets the GLOBAL TracerProvider at import time,
    # and OTel forbids overriding it. So `app.gateway.chat()` / `app.observability.span`
    # always emit through that existing provider — a fresh `TracerProvider()` we build
    # here would never be used, and the exporter would capture nothing. Attach the
    # in-memory exporter to the ALREADY-REGISTERED global provider instead.
    exporter = InMemorySpanExporter()
    provider = trace.get_tracer_provider()           # the one app.observability registered
    provider.add_span_processor(SimpleSpanProcessor(exporter))

    # Stub the gateway client so no network is needed; return a fake raw response
    # whose `x-litellm-model-id` header carries the served deployment id.
    # ... monkeypatch app.gateway._client.chat.completions.with_raw_response.create ...
    from app.observability import span
    from app.gateway import chat

    with span("generate", **{"gen_ai.request.model": "chat"}):
        chat([{"role": "user", "content": "hi"}])

    spans = exporter.get_finished_spans()
    gen = next(s for s in spans if s.name == "generate")
    assert gen.attributes["gen_ai.response.model"] == "chat-bedrock"
    assert gen.attributes["gen_ai.request.model"] == "chat"   # requested alias unchanged
```

> Caveat: `trace.get_tracer_provider()` returns the SDK `TracerProvider` only when
> `app.observability` has been imported (it sets one at import). If a test imports
> this before `app.observability`, OTel's default no-op provider has no
> `add_span_processor`; import `app.observability` (or `app.gateway`, which imports
> it) first. The illustrative imports above do exactly that.

## How it gates merge (summary)

- **Every PR:** `uv run pytest -q` runs config-lint + the OTel span unit test
  (both network-free) and the **existing eval gate** (`tests/test_evals.py`),
  which already blocks merge on quality regression.
- **When a key + gateway are available** (locally, or in the secret-gated
  `eval-gate` job that `07-ci-hardening` will add): the failover smoke test runs
  and asserts fallback + ejection by served-deployment id; the standby eval runs
  with `CHAT_MODEL=chat-standby`.
- **When no key/gateway:** the smoke + standby-eval skip cleanly (same posture as
  the existing eval gate), so a contributor without secrets is never blocked by
  infra they cannot reach, while config-lint still guards the static contract.
