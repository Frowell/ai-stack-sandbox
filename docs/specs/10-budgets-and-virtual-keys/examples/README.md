# Examples — illustrative only

> **These files are a specification, not wired-in code.** They are not imported by
> the app, not on any build path, and intentionally live under `docs/`. They show
> the *real* signatures, file paths, and config shapes the implementation should
> match so a reviewer can judge the design before any code lands. Version-sensitive
> lines (LiteLLM admin-API request/response shapes, health endpoints, the Redis
> config keys) are flagged inline with `# VERIFY` against the **pinned** litellm
> image.

| File | Mirrors (when implemented) | Shows |
|---|---|---|
| [`docker-compose.budgets.yaml`](docker-compose.budgets.yaml) | `docker-compose.yml` (`litellm` + `app` + `postgres` services) | DB-backed proxy: `DATABASE_URL`, Redis wiring, healthcheck, and `service_healthy` ordering |
| [`db_init-litellm.sql`](db_init-litellm.sql) | `db/init-litellm.sql` | first-run creation of the separate `litellm` database + least-privilege role |
| [`litellm_config.budgets.yaml`](litellm_config.budgets.yaml) | `gateway/litellm_config.yaml` | `general_settings`/`litellm_settings`/`router_settings` for DB, Redis, and default budget/limits |
| [`seed_keys.py`](seed_keys.py) | `scripts/seed_keys.py` (`make seed`) | idempotent virtual-key minting via the admin API + `.env` write-back |
| [`app_config.py`](app_config.py) | `app/config.py` | the **one** app code change: prefer `LITELLM_VIRTUAL_KEY`, fall back to master |
| [`app_policy_errors.py`](app_policy_errors.py) | `app/policy_errors.py` | classifying policy-denial (`400`/`429`/`401`) vs. provider errors |
| [`env.example.snippet`](env.example.snippet) | `.env.example` | the new commented env vars |
| [`Makefile.snippet`](Makefile.snippet) | `Makefile` | the `seed` target |
| [`test_budgets.py`](test_budgets.py) | `tests/test_budgets.py` | one concrete test per acceptance criterion (project idiom) |

See [`../design.md`](../design.md) for why each shape was chosen and
[`../testing.md`](../testing.md) for how each acceptance criterion is proven and
how it gates merge.
