# Budgets & virtual keys — test & verification plan

How every acceptance criterion in [`README.md`](README.md) is proven, what
fixtures it needs, and how it gates merge through the project's eval/CI gate.
Concrete tests live in [`examples/test_budgets.py`](examples/test_budgets.py)
(integration) and the unit snippets below.

## Two test tiers (mirrors the rest of the repo)

The target two-tier CI model (owned by [#07 CI hardening](../07-ci-hardening/README.md),
not yet present in the repo — there is no `.github/` today) is: `lint` always, and
a secret-gated `eval-gate` that stands up Postgres + the gateway and runs
`uv run pytest` (the eval merge gate). This feature slots into both tiers and
assumes #07 lands the gate; until then the unit tests below still run under a
plain `uv run pytest`:

| Tier | Runs | Needs | Where |
|---|---|---|---|
| **Unit** (default `uv run pytest`) | every PR, incl. forks | nothing (no live stack, no secret) | `tests/test_config_keys.py`, `tests/test_seed_idempotency.py`, `tests/test_policy_errors.py` |
| **Integration** (`-m integration`, `RUN_BUDGET_IT=1`) | secret-gated `eval-gate` job only | live DB-backed proxy + real `OPENAI_API_KEY` | `tests/test_budgets.py` |

Budget assertions track **real spend**, so they need a live provider call and a
real key — they cannot run in the default unit run or on fork PRs (no secret).
They carry **two** markers (`pytestmark = [pytest.mark.integration,
pytest.mark.skipif(RUN_BUDGET_IT != "1", ...)]` in the example): `integration` so
`-m integration` actually selects them, and the skipif so they stay skipped
without the secret. The `integration` marker must be registered in pyproject
(`[tool.pytest.ini_options] markers = ["integration: live-stack tests"]`) or
strict-marker runs error. Because spend tracking is async, the over-budget and
spend-query tests **poll/retry** rather than assert immediately.

## Acceptance criterion → evidence

| # | Acceptance criterion (README) | Tier | Test | How it's proven |
|---|---|---|---|---|
| 1 | Proxy runs on its own `litellm` DB; created on first start; Prisma migrations applied; **restart preserves keys + spend** | Integration | `test_restart_preserves_keys_and_spend` | Mint a key, spend on it, `docker compose restart litellm`, then `/key/info` still returns the key with spend ≥ prior. (DB created is implicitly proven by the stack coming up healthy at all.) |
| 2 | Generate + revoke a key; revoked → `401` | Integration | `test_revoked_key_is_401` | `/key/generate` then `/key/delete`; a call with the dead key raises `openai.AuthenticationError`. |
| 3 | Scoped key calls `chat`+`embeddings`; cannot hit admin endpoints | Integration | `test_scoped_key_calls_both_aliases`, `test_scoped_key_cannot_admin` | Both alias calls succeed with the virtual key; `/key/generate` with the *virtual* key returns `401/403`. |
| 4 | Over-budget → `400` budget reason; under-budget still succeeds (soft ceiling) | Integration | `test_over_budget_rejected_400` | Mint a near-zero-budget key; loop with retry until `openai.BadRequestError` whose body contains "budget". A normal-budget key in the other tests proves under-budget succeeds. |
| 5 | Over RPM/TPM → `429`; counters in Redis (survive restart) | Integration | `test_over_rpm_rejected_429`, `test_rpm_counter_survives_restart` | `rpm_limit=1` then a burst raises `openai.RateLimitError`. Restart variant: trip the limit, restart proxy, confirm the counter is still tripped within the window (Redis-backed, not in-proxy memory). |
| 6 | Per-key spend queryable; reflects chat **and** embeddings | Integration | `test_spend_includes_chat_and_embeddings` | One chat + one embedding call on a key; poll `/key/info` until `spend > 0`. |
| 7 | App authenticates with the virtual key, not master | Unit + Integration | `test_config_prefers_virtual_key`; `test_scoped_key_cannot_admin` | Unit: `config.gateway_api_key` resolves to `LITELLM_VIRTUAL_KEY` when set. Integration: the app's runtime key is provably *not* admin-capable (#3). |
| 8 | First-run fallback (unset **and** empty) + `make seed` idempotency | Unit | `test_config_falls_back_when_unset`, `test_config_falls_back_when_blank`, `test_seed_idempotent`, `test_seed_no_clobber` | Unit env-var matrix for fallback; seed idempotency with a mocked admin API asserts no duplicate `/key/generate` and no `.env` clobber on a second run. |

Plus a small contract test for the error mapping (README point 5 / unit row of the
"Test & rollout plan"): `test_classify_gateway_error` asserts
`classify_gateway_error` maps `BadRequestError`/`RateLimitError`/`AuthenticationError`
to the right `PolicyDenial` and leaves a provider `APIError` as `NONE`.

## Fixtures & helpers

- **Unit, no network.** `monkeypatch.setenv`/`delenv` to exercise the
  `config.gateway_api_key` precedence; **re-import** `app.config` (it's evaluated
  at import time as a frozen dataclass default) inside the test, e.g.
  `importlib.reload(app.config)`.
- **Seed idempotency (unit).** Monkeypatch the seed's `_api`/`urllib` calls with a
  tiny fake that records requests and serves a stable alias on the second run;
  point `ENV_FILE` at a `tmp_path/.env`. Assert exactly one `/key/generate` across
  two `main([])` invocations and that the `.env` value is unchanged the second time.
- **Integration.** No pytest fixture needed beyond the running stack; helpers
  `_admin()` (master-key admin calls) and `_mint()`/`_client()` are in the example
  file. Each test mints its own key so tests don't share budget/limit state.
  Provider cost is bounded by tiny `max_budget` and short prompts (fractions of a
  cent per run).

## Concrete unit test (project idiom)

Lands in `tests/test_config_keys.py`; runs in the default `uv run pytest`, so it
is part of the **merge gate** on every PR (including forks) with no secret.

```python
import importlib
import app.config


def _reload(monkeypatch, **env):
    for k in ("LITELLM_VIRTUAL_KEY", "LITELLM_MASTER_KEY"):
        monkeypatch.delenv(k, raising=False)
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    return importlib.reload(app.config).settings


def test_config_prefers_virtual_key(monkeypatch):
    s = _reload(monkeypatch, LITELLM_VIRTUAL_KEY="sk-virtual", LITELLM_MASTER_KEY="sk-master")
    assert s.gateway_api_key == "sk-virtual"


def test_config_falls_back_when_unset(monkeypatch):
    s = _reload(monkeypatch, LITELLM_MASTER_KEY="sk-master")
    assert s.gateway_api_key == "sk-master"


def test_config_falls_back_when_blank(monkeypatch):
    # set-but-empty must fall through (the `or`, not two-arg get) — AC #8
    s = _reload(monkeypatch, LITELLM_VIRTUAL_KEY="", LITELLM_MASTER_KEY="sk-master")
    assert s.gateway_api_key == "sk-master"


def test_config_default_when_nothing_set(monkeypatch):
    s = _reload(monkeypatch)
    assert s.gateway_api_key == "sk-sandbox-master"
```

(Restore the module after the suite with a final `importlib.reload(app.config)` in
a fixture/teardown so reloads don't leak into other tests.)

## How it gates merge

- **Unit tests are required on every PR.** They run inside the existing
  `uv run pytest` invocation that the eval gate already executes, so a regression
  in the config-fallback contract or seed idempotency **fails the same check that
  blocks merge today** (`eval-gate` / `eval-gate-result`). These need no secret, so
  fork PRs enforce them too.
- **Integration tests gate via the secret-gated job.** They run only where the
  live DB-backed proxy and `OPENAI_API_KEY` exist — the `eval-gate` job in
  [#07 CI hardening](../07-ci-hardening/README.md). Wiring required:
  1. The gate's `docker compose up` must include this feature's compose deltas
     (`db/init-litellm.sql`, `DATABASE_URL`, Redis, healthcheck) so the proxy comes
     up DB-backed.
  2. A `make seed` step (or inline `RUN_BUDGET_IT=1`) before the integration run.
  3. Pin the litellm image (per #07 supply-chain pinning) — these tests assert
     version-sensitive `400`/`429`/`401` behavior and admin shapes (`# VERIFY`
     lines), so an unpinned `main-stable` could flip them.
- Per #07, the required check is the always-running **`eval-gate-result`** summary,
  not the conditionally-skipped `eval-gate` — so a fork PR (integration skipped,
  units green) still merges, while a real failure with the secret present blocks.
- **CI cost & flake control.** Mark the integration class so it can be gated to
  nightly/`main` rather than every PR if provider spend or async-spend flake is a
  problem (the over-budget/spend tests already poll with bounded retries). Decide
  jointly with #07's open question "gate on PRs vs. only on `main`".

## Out of scope / not gated

- Cross-**replica** rate-limit enforcement: configured (Redis) and argued correct,
  but the single-replica demo only verifies *enforcement* and *counter survival
  across restart*, not multi-replica. Not a gate (would need scaling the proxy).
- Exact bounded-overshoot magnitude under concurrency: budgets are a soft ceiling;
  tests assert "rejected once recorded spend ≥ budget," not a token-exact cap.
- Ingest-robustness on mid-ingest budget exhaustion (corpus-wipe edge case): a
  documented accepted risk, owned by ingest robustness, not gated here.
