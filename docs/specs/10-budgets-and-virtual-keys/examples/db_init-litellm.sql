-- ILLUSTRATIVE — a spec, not wired-in code. Mirrors a NEW file: db/init-litellm.sql
--
-- Why this file exists:
--   * Postgres only auto-creates POSTGRES_DB (= `sandbox`) on first start.
--   * LiteLLM's Prisma `migrate deploy` runs against an *existing* database; it
--     creates TABLES, not the database itself.
--   * So nothing creates the `litellm` database unless we do it here.
--
-- Like db/init.sql, this runs EXACTLY ONCE, on the first start of an empty
-- pgdata volume (docker-entrypoint-initdb.d). Numbered 02- in the compose mount
-- so ordering relative to 01-init.sql is deterministic. On an existing volume it
-- does NOT run — see the rollout note in README.md (requires `make down` (-v) or
-- a manual CREATE DATABASE on an already-initialized stack).

-- Least-privilege role for the proxy: owns only its own database, has no rights
-- on `sandbox`/`documents`. Keeps app migrations and LiteLLM migrations isolated.
CREATE ROLE litellm WITH LOGIN PASSWORD 'litellm';   -- VERIFY: keep in sync with DATABASE_URL in compose / .env

CREATE DATABASE litellm OWNER litellm;

-- Note: no GRANT on the `sandbox` database is issued, so the `litellm` role
-- cannot read or write the documents corpus even though both DBs share one server.
