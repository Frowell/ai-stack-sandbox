"""ILLUSTRATIVE — spec for spec 14, not wired-in code.

Offline, hermetic tests proving each acceptance criterion. This file is named
`example_tests.py` (NOT test_*.py / *_test.py) on purpose, so pytest's default
discovery does NOT collect it while it lives under docs/specs/. Port these into
`tests/test_observability.py` (and extend `tests/test_evals.py`) when implementing
— do not rename it into a collected pattern, and do not import from this dir.

Hermeticity (the two things a naïve port gets wrong — see testing.md):

  1. `app.evals.run()` is NOT hermetic by itself: it calls ask() -> retrieve()
     (Postgres) + chat()/embed() (the gateway). So the unit tests monkeypatch
     `app.evals.ask` to a canned answer and use a questions-only fixture (no
     `reference` => judge_score short-circuits without a gateway call; no
     `keywords` => keyword_score returns 1.0). With the endpoint unset, run() then
     touches no Postgres, no gateway, no network. DB persistence is best-effort/
     non-fatal, so its absence does not fail the run.

  2. The flush test uses a BUFFERING BatchSpanProcessor with a long
     schedule_delay_millis (so the timer never fires); the evals.run span reaches
     the exporter ONLY because of the explicit flush() in run()'s finally. A
     SimpleSpanProcessor would export on span-end and pass even WITHOUT the fix —
     proving nothing.

Criterion 1 (full Langfuse v3 stack boots + bootstrap) is a manual rollout check,
never in CI (see testing.md "Rollout verification").
"""
import sys

import pytest
from opentelemetry.sdk.trace.export import BatchSpanProcessor, SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from app import evals, observability
from app.score_sink import NoopSink, get_score_sink


@pytest.fixture
def suite(tmp_path, monkeypatch):
    """A 2-case, questions-only fixture + a stubbed ask() => fully hermetic run()."""
    p = tmp_path / "mini.jsonl"
    p.write_text('{"question": "q1"}\n{"question": "q2"}\n')
    monkeypatch.setattr(evals, "ask", lambda q: f"answer to {q}")  # no retrieve/gateway
    return str(p)


# --- Criterion 2: short-lived run reliably exports (the flush fix) -----------
def test_run_flushes_spans(suite):
    exporter = InMemorySpanExporter()
    proc = BatchSpanProcessor(exporter, schedule_delay_millis=60_000)  # timer won't fire
    observability._provider.add_span_processor(proc)
    try:
        evals.run(path=suite)  # endpoint unset; flush() in run()'s finally must export
        names = {s.name for s in exporter.get_finished_spans()}
        assert "evals.run" in names  # empty WITHOUT the finally flush (fails today)
    finally:
        proc.shutdown()


# --- Criterion 3: distinct, non-null per-case trace_id, even with no endpoint -
def test_trace_ids_distinct(suite):
    report = evals.run(path=suite)  # provider is ALWAYS_ON, so trace_id is valid
    ids = [c["trace_id"] for c in report["cases"]]
    assert len(ids) == 2
    assert all(ids)  # non-null / non-empty
    assert len(set(ids)) == len(ids)  # DISTINCT — guards the shared-trace collision


# --- Criterion 4: trace_url from template, else NULL with usable trace_id -----
def test_trace_url_from_template(suite, monkeypatch):
    monkeypatch.setattr(evals, "_URL_TEMPLATE", "https://h/project/p/traces/{trace_id}")
    report = evals.run(path=suite)
    for c in report["cases"]:
        assert c["trace_url"] == f"https://h/project/p/traces/{c['trace_id']}"


def test_trace_url_null_without_template(suite, monkeypatch):
    monkeypatch.setattr(evals, "_URL_TEMPLATE", None)
    report = evals.run(path=suite)
    assert all(c["trace_url"] is None for c in report["cases"])
    assert all(c["trace_id"] for c in report["cases"])  # row still usable


# --- Criterion 5: sink default no-op; OTLP path imports no vendor SDK ---------
def test_score_sink_noop_default(monkeypatch):
    monkeypatch.delenv("EVAL_SCORE_SINK", raising=False)
    assert isinstance(get_score_sink(), NoopSink)


def test_otlp_path_imports_no_vendor_sdk(suite, monkeypatch):
    monkeypatch.delenv("EVAL_SCORE_SINK", raising=False)
    sys.modules.pop("langfuse", None)
    evals.run(path=suite)
    assert "langfuse" not in sys.modules  # lazy import never triggered


# --- Criterion 6: endpoint unset => behaves as today, no error ---------------
def test_no_endpoint_no_export_no_error(suite, monkeypatch):
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_ENDPOINT", raising=False)
    report = evals.run(path=suite)  # must not raise
    assert "cases" in report and "passed" in report
# (The "core 4-container stack unchanged" half of criterion 6 is a compose review:
#  the backend service is profiles:-gated, so a plain `docker compose up` omits it.)


# --- Criterion 8: OTEL_CAPTURE_CONTENT off drops content (span attr + comment) -
def test_capture_content_off(monkeypatch):
    # Test span() DIRECTLY: with ask stubbed in the hermetic run() tests, the
    # agent.run/retrieve spans that carry these keys are never created, so a
    # run()-based assertion would pass vacuously.
    monkeypatch.setattr(observability, "_CAPTURE_CONTENT", False)
    exporter = InMemorySpanExporter()
    observability._provider.add_span_processor(SimpleSpanProcessor(exporter))
    with observability.span("t", **{"input.question": "secret", "other": "ok"}):
        pass
    s = exporter.get_finished_spans()[-1]
    assert "input.question" not in s.attributes  # content suppressed
    assert s.attributes.get("other") == "ok"  # non-content kept


def test_score_comment_gated_by_capture_content(suite, monkeypatch):
    # The other content channel: the score_sink comment must also honor the toggle.
    monkeypatch.setattr(observability, "_CAPTURE_CONTENT", False)
    seen = []

    class _CaptureSink:
        def record(self, trace_id, name, value, *, comment=None):
            seen.append(comment)

    monkeypatch.setattr(evals, "get_score_sink", lambda: _CaptureSink())
    evals.run(path=suite)
    assert seen and all(c is None for c in seen)  # question NOT pushed as comment


# --- Criterion 7: eval_results auto-created on an already-populated volume -----
# DB-gated: needs the compose Postgres; skipped when DATABASE_URL is unreachable,
# so the default suite stays hermetic.
def _db_reachable() -> bool:
    import psycopg

    from app.config import settings

    try:
        psycopg.connect(settings.database_url, connect_timeout=2).close()
        return True
    except Exception:
        return False


@pytest.mark.skipif(not _db_reachable(), reason="needs the compose Postgres")
def test_eval_results_created_on_existing_db(suite):
    import psycopg

    from app.config import settings

    # Simulate a pre-existing (documents-only) volume: drop eval_results first, so
    # the run must recreate it via the runtime CREATE TABLE IF NOT EXISTS path
    # (NOT db/init.sql, which only runs on an empty pgdata volume).
    with psycopg.connect(settings.database_url, autocommit=True) as conn:
        conn.execute("DROP TABLE IF EXISTS eval_results")

    evals.run(path=suite)

    with psycopg.connect(settings.database_url, autocommit=True) as conn:
        n = conn.execute("SELECT count(*) FROM eval_results").fetchone()[0]
        assert n >= 2  # both cases persisted; table was auto-created at runtime


def test_persistence_db_unavailable_is_non_fatal(suite, monkeypatch):
    # Hermetic: a DB hiccup must never fail the gate. run() wraps psycopg.connect
    # in try/except (conn=None => skip persistence) and _persist swallows insert
    # errors, so an unreachable DB still yields a verdict.
    def _boom(*a, **k):
        raise RuntimeError("db down")

    monkeypatch.setattr(evals.psycopg, "connect", _boom)
    report = evals.run(path=suite)  # must still return a pass/fail verdict
    assert "passed" in report
