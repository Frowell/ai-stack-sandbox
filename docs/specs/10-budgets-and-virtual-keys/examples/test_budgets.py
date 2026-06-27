"""ILLUSTRATIVE — a spec, not wired-in code. Mirrors tests/test_budgets.py.

One test per acceptance criterion, in the project's pytest idiom (plain asserts,
no heavy fixtures — cf. tests/test_evals.py). These are LIVE-STACK + REAL-PROVIDER
integration tests: they need the DB-backed proxy up and a real OPENAI_API_KEY,
because budgets track *real* spend. They are therefore marked `integration` and
skipped unless explicitly enabled, so the default `uv run pytest` (unit) run and
fork CI (no secret) stay green. See ../testing.md for the CI wiring.

Enable with:  RUN_BUDGET_IT=1 OPENAI_API_KEY=sk-... uv run pytest -m integration tests/test_budgets.py
"""
from __future__ import annotations

import json
import os
import subprocess
import time
import urllib.error
import urllib.request

import openai
import pytest

# Two markers: `integration` so `-m integration` selects this module (register the
# marker in pyproject `[tool.pytest.ini_options] markers = ["integration: ..."]`),
# and skipif so the default `uv run pytest` and fork CI (no secret) stay green.
pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.environ.get("RUN_BUDGET_IT") != "1",
        reason="live-stack budget integration test; set RUN_BUDGET_IT=1 (needs proxy + OPENAI_API_KEY)",
    ),
]

BASE = os.environ.get("GATEWAY_BASE_URL", "http://localhost:4000")
MASTER = os.environ.get("LITELLM_MASTER_KEY", "sk-sandbox-master")


def _admin(method: str, path: str, body: dict | None = None) -> dict:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        f"{BASE}{path}", data=data, method=method,
        headers={"Authorization": f"Bearer {MASTER}", "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.loads(r.read() or b"{}")


def _mint(**overrides) -> str:
    spec = {"models": ["chat", "embeddings"], "max_budget": 0.01,
            "budget_duration": "30d", "rpm_limit": 100, "tpm_limit": 1_000_000}
    spec.update(overrides)
    return _admin("POST", "/key/generate", spec)["key"]


def _client(key: str) -> openai.OpenAI:
    return openai.OpenAI(base_url=BASE, api_key=key)


# AC: virtual key scoped to [chat, embeddings] can call both aliases.
def test_scoped_key_calls_both_aliases():
    key = _mint()
    c = _client(key)
    assert c.chat.completions.create(model="chat", messages=[{"role": "user", "content": "hi"}])
    assert c.embeddings.create(model="embeddings", input=["hello"]).data


# AC: a revoked key is rejected (401).
def test_revoked_key_is_401():
    key = _mint()
    _admin("POST", "/key/delete", {"keys": [key]})
    with pytest.raises(openai.AuthenticationError):
        _client(key).chat.completions.create(model="chat", messages=[{"role": "user", "content": "hi"}])


# AC: scoped key cannot use master-only admin endpoints.
def test_scoped_key_cannot_admin():
    key = _mint()
    req = urllib.request.Request(
        f"{BASE}/key/generate", data=b"{}", method="POST",
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
    )
    with pytest.raises(urllib.error.HTTPError) as e:
        urllib.request.urlopen(req, timeout=10)
    assert e.value.code in (401, 403)


# AC: over-budget rejected with 400/budget reason; spend is async so poll briefly.
def test_over_budget_rejected_400():
    key = _mint(max_budget=0.0000001)   # one real call crosses it
    c = _client(key)
    with pytest.raises((openai.BadRequestError, openai.AuthenticationError)) as e:
        for _ in range(8):
            c.chat.completions.create(model="chat", messages=[{"role": "user", "content": "hi"}])
            time.sleep(1.5)             # let async spend flush, then re-attempt
    assert "budget" in str(e.value).lower()   # VERIFY exact reason text on the pin


# AC: over-RPM rejected with 429.
def test_over_rpm_rejected_429():
    key = _mint(rpm_limit=1)
    c = _client(key)
    with pytest.raises(openai.RateLimitError):
        for _ in range(5):
            c.chat.completions.create(model="chat", messages=[{"role": "user", "content": "hi"}])


# AC: /key/info spend reflects BOTH chat and embedding calls.
def test_spend_includes_chat_and_embeddings():
    key = _mint(max_budget=1.0)
    c = _client(key)
    c.chat.completions.create(model="chat", messages=[{"role": "user", "content": "hi"}])
    c.embeddings.create(model="embeddings", input=["hello world"])
    spend = 0.0
    for _ in range(10):                 # spend tracking is async
        spend = _admin("GET", f"/key/info?key={key}")["info"]["spend"]   # VERIFY response path
        if spend > 0:
            break
        time.sleep(1.5)
    assert spend > 0


def _restart_litellm_and_wait() -> None:
    """Restart the proxy and block until /health/readiness is green again.

    VERIFY: service name (`litellm`) and that the test runner can reach the docker
    socket; in the eval-gate job the stack is already up via `docker compose`.
    """
    subprocess.run(["docker", "compose", "restart", "litellm"], check=True, timeout=120)
    for _ in range(60):                 # cover the (smaller) restart-time migrate/readiness window
        try:
            with urllib.request.urlopen(f"{BASE}/health/readiness", timeout=3) as r:
                if r.status == 200:
                    return
        except (urllib.error.URLError, ConnectionError):
            pass
        time.sleep(2)
    raise AssertionError("litellm did not become ready after restart")


# AC #1: keys + accumulated spend live in Postgres, so a proxy restart preserves
# both (no in-memory-only state).
def test_restart_preserves_keys_and_spend():
    key = _mint(max_budget=1.0)
    c = _client(key)
    c.chat.completions.create(model="chat", messages=[{"role": "user", "content": "hi"}])
    spend_before = 0.0
    for _ in range(10):                 # spend is async; let it flush before restart
        spend_before = _admin("GET", f"/key/info?key={key}")["info"]["spend"]
        if spend_before > 0:
            break
        time.sleep(1.5)
    assert spend_before > 0

    _restart_litellm_and_wait()

    info = _admin("GET", f"/key/info?key={key}")["info"]   # key still known post-restart
    assert info["spend"] >= spend_before                   # spend persisted, not reset


# AC #5: RPM counters live in Redis, not proxy memory, so a tripped limit stays
# tripped across a proxy restart (within the same window). NOTE: this only holds
# if the restart completes inside the RPM window (~60s); if a slow restart lets the
# window roll over, prefer the lower-flake assertion that the *spend/key* survived
# (test_restart_preserves_keys_and_spend) and treat this as best-effort.
def test_rpm_counter_survives_restart():
    key = _mint(rpm_limit=1)
    c = _client(key)
    with pytest.raises(openai.RateLimitError):
        for _ in range(5):
            c.chat.completions.create(model="chat", messages=[{"role": "user", "content": "hi"}])

    _restart_litellm_and_wait()         # Redis keeps the counter; proxy memory is gone

    with pytest.raises(openai.RateLimitError):   # still tripped within the window
        c.chat.completions.create(model="chat", messages=[{"role": "user", "content": "hi"}])
