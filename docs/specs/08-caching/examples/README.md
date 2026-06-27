# Examples — illustrative only

Everything in this directory is a **spec, not wired-in code**. These files show
the concrete shape each change would take in *this* codebase (real paths, real
signatures), but nothing here is imported, mounted, or executed by the running
stack. Do not copy verbatim without confirming the version-sensitive lines flagged
inline (especially `cache_params.type` and LiteLLM's cache-hit field/header names
against the **pinned** image — see [`../design.md` §2, §6](../design.md)).

| File | Illustrates | Maps to real file |
|---|---|---|
| `litellm_config.caching.yaml` | exact-match (default) + semantic (opt-in) cache config | `gateway/litellm_config.yaml` (CHANGED) |
| `litellm_config.eval.yaml` | CI-only caching-OFF config for the eval bypass | `gateway/litellm_config.eval.yaml` (NEW) |
| `docker-compose.caching.yaml` | wiring the `litellm` service to Redis | `docker-compose.yml` (CHANGED) |
| `app_gateway.py` | widening `chat()` to surface cache metadata | `app/gateway.py` (CHANGED) |
| `app_observability.py` | attaching `cache.*` span attributes | `app/observability.py` (CHANGED) |
| `app_pricing.py` | per-model price table for `tokens_saved → $` | `app/pricing.py` (NEW, optional) |
| `app_retrieval_key_alignment.py` | sha256 key alignment (NOT a rewrite) | `app/retrieval.py` (CHANGED, one line) |
| `test_caching.py` | unit + live tests per acceptance criterion | `tests/test_caching_*.py` (NEW) |
| `ci_eval_gate.snippet.yaml` | eval-gate cache bypass + semantic-on regression job | `.github/workflows/ci.yml` (CHANGED, coordinate w/ spec 07) |

See [`../testing.md`](../testing.md) for how each acceptance criterion is proven
and how it gates merge.
