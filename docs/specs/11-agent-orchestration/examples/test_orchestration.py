"""ILLUSTRATIVE — proposed tests/test_orchestration.py. Not wired in.

Project idiom: pytest with `from app... import ...` (conftest.py puts the repo root
on sys.path). Tests are hermetic where possible by monkeypatching app.gateway so no
real model/DB is needed; the fresh-process resume test is the one integration case
that needs the compose Postgres (skipped when DATABASE_URL is unreachable).

CONFIG GOTCHA (load-bearing — see ../testing.md and ../design.md §8). `app.config`
exposes a module-level *frozen* `settings = Settings()` built **at import time**, and
the dataclass field defaults capture `os.environ.get(...)` **once at class-definition
time**. So `monkeypatch.setenv("MAX_ITERATIONS", ...)` after import is a NO-OP — it
changes neither the live `settings` nor a freshly constructed `Settings()`. In-process
tests must instead swap the module-level `settings` with a modified copy via
`dataclasses.replace(...)` (which works on frozen dataclasses) and reset the cached
graphs. The `recursion_limit` property is the one field read from env at call time.
Only a *fresh interpreter* (subprocess) re-reads env at import — that is why the
single-mode and resume tests below shell out instead of using setenv in-process.

Maps to acceptance criteria in ../README.md; see ../testing.md for the full matrix.
"""
import subprocess
import sys
from dataclasses import replace

import pytest


def _use_settings(monkeypatch, **overrides):
    """Swap app.agent's module-level `settings` with a frozen copy carrying the
    overrides, and clear cached graphs so the new mode/caps take effect. This is the
    correct way to drive config in-process; setenv does NOT work (see module docstring)."""
    from app import agent
    monkeypatch.setattr(agent, "settings", replace(agent.settings, **overrides))
    monkeypatch.setattr(agent, "_DURABLE_GRAPH", None, raising=False)
    return agent


# --- AC: caps route to truncate, never raise GraphRecursionError (hermetic) ----
def test_looping_supervisor_truncates_before_recursion_limit(monkeypatch):
    agent = _use_settings(monkeypatch, orchestration_mode="multi", max_iterations=3, token_budget=0)
    # force the supervisor to never say "done"
    monkeypatch.setattr(agent, "supervisor_node",
                        lambda s: {"iterations": s.get("iterations", 0) + 1, "next": "research"})
    monkeypatch.setattr(agent, "research_node", lambda s: {"notes": [*s.get("notes", []), "x"]})
    graph = agent._build_multi()  # in-memory checkpointer; no DB needed
    out = graph.invoke(
        {"question": "q", "iterations": 0, "notes": [], "context": [(1, "c")], "token_budget": 0},
        {"recursion_limit": agent.settings.recursion_limit},
    )
    assert out["truncated"] is True            # ended via truncate, not an exception
    assert out["iterations"] <= 3 + 1          # stopped at the iteration cap


# --- AC: token_budget consumes real resp.usage -> truncated=True (hermetic) -----
def test_token_budget_truncates(monkeypatch):
    agent = _use_settings(monkeypatch, orchestration_mode="multi", token_budget=10, max_iterations=6)
    from app.gateway import ChatResult
    monkeypatch.setattr(agent, "chat_with_usage", lambda *a, **k: ChatResult("partial", 999))
    monkeypatch.setattr(agent, "retrieve", lambda q, **k: [(1, "ctx")])
    # Keep it hermetic: ask_resumable() in multi mode would otherwise call get_graph()
    # -> PostgresSaver (a live DB connection). Pin it to the in-memory multi graph.
    monkeypatch.setattr(agent, "get_graph", lambda: agent._build_multi())
    res = agent.ask_resumable("expensive question")
    assert res.truncated is True               # truncated on BUDGET, not the iteration cap


# --- AC: invalid/out-of-range route falls back, does not crash --------------
def test_invalid_route_falls_back_to_truncate():
    from app.agent import _route
    assert _route({"next": "no-such-node", "token_budget": 0}) == "truncate"


# --- AC: ask() keeps (question: str) -> str ----------------------------------
def test_ask_signature_unchanged(monkeypatch):
    import inspect

    from app.agent import ask
    sig = inspect.signature(ask)
    assert list(sig.parameters) == ["question"]
    assert sig.return_annotation in (str, "str")


# --- AC: single mode opens NO DB connection at import ------------------------
def test_single_mode_import_opens_no_db():
    # Import in a subprocess with a deliberately-unreachable DB; single mode must
    # still import cleanly because it never connects.
    code = "import os; os.environ['ORCHESTRATION_MODE']='single'; import app.agent; print('ok')"
    env = {"DATABASE_URL": "postgresql://nope:nope@127.0.0.1:1/none", "PATH": "/usr/bin:/bin"}
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, env=env)
    assert r.returncode == 0 and "ok" in r.stdout


# --- AC: the postgres saver dependency is importable -------------------------
def test_postgres_saver_importable():
    # FAILS today until langgraph-checkpoint-postgres is added to pyproject/uv.lock.
    import importlib
    assert importlib.import_module("langgraph.checkpoint.postgres")


# --- AC: fresh-process resume completes without re-running committed nodes ----
@pytest.mark.integration
def test_resume_in_fresh_process(postgres_available):
    """Run #1 (this process) interrupts after a node commit; run #2 (a fresh
    subprocess) resumes by thread_id and finishes. A side-effect counter persisted
    out-of-band proves the committed node is NOT re-executed on resume."""
    if not postgres_available:
        pytest.skip("needs compose Postgres")
    # 1. start a thread, force an interrupt after `retrieve` commits (graph compiled
    #    with interrupt_after=["retrieve"]); record thread_id.
    # 2. spawn `python -c "from app.agent import ask_resumable; \
    #       print(ask_resumable(None, thread_id=TID).answer)"` in a fresh process.
    # 3. assert it returns a complete answer and the retrieve side-effect ran once.
    ...
