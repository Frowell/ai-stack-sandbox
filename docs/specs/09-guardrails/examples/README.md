# Examples — illustrative only

> **These files are a specification, not wired-in code.** They are not imported by
> the app, not on any build path, and intentionally live under `docs/`. They show
> the *real* signatures, file paths, and config shapes the implementation should
> match so a reviewer can judge the design before any code lands. Version-sensitive
> lines (LiteLLM hook/header APIs) are flagged inline with `# VERIFY`.

| File | Mirrors (when implemented) | Shows |
|---|---|---|
| [`litellm_config.guardrails.yaml`](litellm_config.guardrails.yaml) | `gateway/litellm_config.yaml` | the `guardrails:` block + callback registration |
| [`Dockerfile.gateway`](Dockerfile.gateway) | `gateway/Dockerfile` | extending the litellm image with Presidio + spaCy |
| [`entrypoint.sh`](entrypoint.sh) | `gateway/entrypoint.sh` | cold-start warmup before the server reports ready (AC4) |
| [`docker-compose.guardrails.yaml`](docker-compose.guardrails.yaml) | `docker-compose.yml` (`litellm` service) | build-from-Dockerfile + mount `guardrails/` |
| [`gateway_guardrails_patterns.py`](gateway_guardrails_patterns.py) | `gateway/guardrails/patterns.py` | injection regex table (unit-testable) |
| [`gateway_guardrails_policy.py`](gateway_guardrails_policy.py) | `gateway/guardrails/policy.py` | fail-mode wrapper + header-name contract |
| [`gateway_guardrails_injection.py`](gateway_guardrails_injection.py) | `gateway/guardrails/injection.py` | `PromptInjectionGuardrail(CustomGuardrail)` |
| [`gateway_guardrails_pii.py`](gateway_guardrails_pii.py) | `gateway/guardrails/pii.py` | `PIIGuardrail(CustomGuardrail)` pre+post |
| [`app_agent_generate_node.py`](app_agent_generate_node.py) | `app/agent.py` | channel separation (delimited untrusted context) |
| [`app_guardrails.py`](app_guardrails.py) | `app/guardrails.py` | `GuardrailBlocked`, `GuardrailDecision`, header parse |
| [`app_gateway.py`](app_gateway.py) | `app/gateway.py` | header read + block→sentinel mapping |
| [`env.example.snippet`](env.example.snippet) | `.env.example` | new env vars |
| [`test_guardrails.py`](test_guardrails.py) | `tests/test_guardrails_*.py` | one concrete test per acceptance criterion (idiom) |

See [`../design.md`](../design.md) for why each shape was chosen and
[`../testing.md`](../testing.md) for how each acceptance criterion is proven.
