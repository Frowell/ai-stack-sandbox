"""ILLUSTRATIVE — how the checkpoint schema is created. Not wired in.

The checkpoint tables (checkpoints, checkpoint_writes, checkpoint_blobs) are owned
by LangGraph, NOT by db/init.sql. db/init.sql is mounted into
/docker-entrypoint-initdb.d and only runs on the FIRST boot of an empty pgdata
volume, so it would silently skip an existing volume; and the DDL belongs to
LangGraph, not hand-transcribed SQL. Instead, call the saver's idempotent setup()
at process start when ORCHESTRATION_MODE=multi.

setup() is safe to call repeatedly (CREATE ... IF NOT EXISTS semantics), so this
doubles as the migration step. `make down -v && make up` on a clean volume still
boots because db/init.sql is untouched and these tables are created lazily by the
app, not at DB init.
"""
from .config import settings


def ensure_checkpoint_schema() -> None:
    """Idempotent. Called once at process start (or lazily on first get_graph())."""
    if settings.orchestration_mode != "multi":
        return  # single mode opens no DB connection and needs no checkpoint tables
    from langgraph.checkpoint.postgres import PostgresSaver

    with PostgresSaver.from_conn_string(settings.database_url) as saver:
        saver.setup()


# Smoke check for the dependency acceptance criterion:
#   uv run python -c "import langgraph.checkpoint.postgres"   # must succeed
# Today this FAILS (langgraph 1.2.6 + langgraph-checkpoint 4.1.1, no postgres saver
# in the resolved tree) until langgraph-checkpoint-postgres is added to
# pyproject.toml and uv.lock — see pyproject_dependency.toml.
