# Illustrative examples — model failover

> **These files are a specification, not wired-in code.** They show the *shape* of
> the change against this codebase's real signatures and paths. Nothing here is
> imported, executed by CI, or referenced from `gateway/`, `app/`, or `tests/`.
> Implementation happens in the PR that lands the feature; these are the target.

| File | Illustrates | Maps to real path |
|---|---|---|
| [`litellm_config.prod.yaml`](./litellm_config.prod.yaml) | Production-shaped config: Anthropic + Bedrock active/active under `chat`, `chat-standby` re-alias, `router_settings`, `fallbacks` | `gateway/litellm_config.yaml` |
| [`litellm_config.no-aws.yaml`](./litellm_config.no-aws.yaml) | No-AWS CI/smoke config: single dead-primary OpenAI `chat` + healthy OpenAI `chat-standby` (collapsed to one `chat` deployment on purpose, to force the fallback path) | a CI-only config the smoke test mounts |
| [`gateway_served_model.py`](./gateway_served_model.py) | `app/gateway.chat()` reading the served deployment and setting it on the active OTel span (no signature change) | `app/gateway.py` |
| [`agent_span.py`](./agent_span.py) | The `generate` node's span attributes after the change | `app/agent.py` |
| [`env.example.snippet`](./env.example.snippet) | New provider/region env vars | appended to `.env.example` |
| [`docker-compose.snippet.yml`](./docker-compose.snippet.yml) | The `litellm` service env additions | merged into `docker-compose.yml` |
| [`test_config_lint.py`](./test_config_lint.py) | CI-safe config-lint unit test (no network) | new `tests/test_failover_config.py` |
| [`test_failover_smoke.py`](./test_failover_smoke.py) | Gated integration smoke test proving fallback + ejection via `x-litellm-model-id` | new `tests/test_failover_smoke.py` |

See [`../testing.md`](../testing.md) for how each acceptance criterion maps onto
these tests, and [`../design.md`](../design.md) for why the design is shaped this
way.
