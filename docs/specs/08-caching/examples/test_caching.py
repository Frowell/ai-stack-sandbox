"""ILLUSTRATIVE — a spec, not wired-in tests.

Example tests in the project's idiom (pytest, import from `app`, see tests/test_evals.py
and conftest.py which puts the repo root on sys.path). These mirror the
acceptance criteria; the full per-criterion mapping is in ../testing.md.

Markers used here (declare in pyproject.toml [tool.pytest.ini_options]):
  - `live`   : needs the running stack (gateway + redis) and a provider key; skipped
               in the default unit run and on fork CI with no secret.
  - default  : pure-unit, offline, deterministic (key construction, layer derivation).
"""
from __future__ import annotations

import time

import pytest


# ---------------------------------------------------------------------------
# AC: cache-key construction includes the virtual key (UNIT, offline, today)
# ---------------------------------------------------------------------------
def test_cache_key_includes_virtual_key():
    """Pure-unit proof of the namespacing contract. No DB / second key needed yet —
    just that the key-construction function folds in the caller key. The live
    two-key isolation test is deferred to a DB-backed gateway (spec 10)."""
    from app.caching import build_cache_key  # illustrative helper (design.md §4)

    base = dict(model="chat", messages=[{"role": "user", "content": "hi"}])
    key_a = build_cache_key(base, virtual_key="sk-tenant-A")
    key_b = build_cache_key(base, virtual_key="sk-tenant-B")

    assert key_a != key_b, "same request under two keys must not share a cache entry"
    # and identical (key, request) is stable -> a real repeat can hit:
    assert key_a == build_cache_key(base, virtual_key="sk-tenant-A")


# ---------------------------------------------------------------------------
# AC: cache.layer derivation (UNIT, offline)
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "hidden, cached_tokens, configured_type, expected_layer, expected_hit",
    [
        ({"cache_hit": True}, 0, "redis", "exact", True),
        ({"cache_hit": True}, 0, "redis-semantic", "semantic", True),
        ({}, 1500, "redis", "prompt", False),       # live call, prefix discounted
        ({}, 0, "redis", "miss", False),
    ],
)
def test_cache_layer_derivation(hidden, cached_tokens, configured_type,
                                expected_layer, expected_hit, monkeypatch):
    from app import gateway

    monkeypatch.setattr(gateway.settings, "cache_layer_name",
                        "semantic" if configured_type == "redis-semantic" else "exact",
                        raising=False)
    resp = _FakeResp(hidden=hidden, cached_tokens=cached_tokens, prompt_tokens=2000)
    meta = gateway.derive_cache_meta(resp, headers={})
    assert meta.layer == expected_layer
    assert meta.hit == expected_hit


# ---------------------------------------------------------------------------
# AC: identical query served from cache, no provider call, byte-identical (LIVE)
# ---------------------------------------------------------------------------
@pytest.mark.live
def test_identical_query_is_cache_hit_and_byte_identical():
    """Default `type: redis` mode. Second identical call returns the SAME bytes,
    materially faster, with cache.hit True and no provider round-trip."""
    from app.gateway import chat_with_meta

    msgs = [{"role": "user", "content": "What makes an AI stack mature?"}]

    t0 = time.perf_counter()
    first, m1 = chat_with_meta(msgs)
    miss_ms = (time.perf_counter() - t0) * 1000
    assert m1.layer in ("miss", "prompt")     # first call is not a response-cache hit

    t1 = time.perf_counter()
    second, m2 = chat_with_meta(msgs)
    hit_ms = (time.perf_counter() - t1) * 1000

    assert m2.hit and m2.layer == "exact"
    assert second == first, "cached response must be byte-identical to the origin"
    assert hit_ms < miss_ms * 0.5, "cache hit must be materially faster"


# ---------------------------------------------------------------------------
# AC: fail-open verified empirically — Redis down -> still succeeds (LIVE)
# ---------------------------------------------------------------------------
@pytest.mark.live
def test_fail_open_when_redis_down(redis_stopped):
    """Fail-open here is LiteLLM-controlled (design.md §7), so it is PROVEN, not
    assumed by analogy to retrieval.py. `redis_stopped` is a fixture that stops the
    redis container for the duration of the test."""
    from app.gateway import chat_with_meta

    answer, meta = chat_with_meta([{"role": "user", "content": "ping"}])
    assert answer, "request must succeed with Redis unavailable"
    assert not meta.hit, "every call is a miss when the cache is unreachable"


# ---------------------------------------------------------------------------
# AC: eval runs bypass every cache layer (LIVE, against the eval config)
# ---------------------------------------------------------------------------
@pytest.mark.live
def test_eval_config_serves_fresh_after_corpus_change(gateway_on_eval_config):
    """Under the caching-OFF eval config, a corpus change must change the answer.

    Temperature is pinned to 0 in the eval gateway's model_list (NOT via an ask()
    kwarg — that cannot thread through ask()->generate_node->chat(), design.md §3),
    so a1 != a2 here is attributable to the corpus change, not model noise. Pair
    with the positive control below (semantic ON => stale) to isolate cache bypass."""
    from app.agent import ask

    a1 = ask("why put a gateway in the hot path?")
    _mutate_corpus_and_reingest()          # changes retrieved context -> answer changes
    a2 = ask("why put a gateway in the hot path?")
    assert a1 != a2, "eval config served a stale cached answer — gate would be corrupted"


@pytest.mark.live
def test_positive_control_semantic_on_serves_stale(gateway_semantic, fresh_redis):
    """POSITIVE CONTROL for AC-6 — MUST use the SEMANTIC config, not exact.

    The exact-match key hashes model + messages + params, and generate_node embeds
    the retrieved CONTEXT inside the user message. A corpus re-ingest changes that
    context -> changes the message -> changes the exact-match key, so a caching-ON
    *exact* gateway would also return a FRESH answer (a1 != a2) and could never
    prove masking. Only the SEMANTIC cache still hits across a small context change,
    so it is the only valid positive control for the mutate-corpus procedure
    (testing.md AC-6). Temperature is pinned to 0 in the semantic gateway's
    model_list, so the only thing that can make a1 == a2 is the stale semantic hit."""
    from app.agent import ask

    a1 = ask("why put a gateway in the hot path?")
    _mutate_corpus_and_reingest()
    a2 = ask("why put a gateway in the hot path?")
    assert a1 == a2, "semantic-ON control did NOT serve stale — test cannot isolate bypass"


# --- tiny fakes / placeholders (illustrative) --------------------------------
class _FakeUsage:
    def __init__(self, cached_tokens, prompt_tokens):
        self.prompt_tokens = prompt_tokens
        self.prompt_tokens_details = type("D", (), {"cached_tokens": cached_tokens})()


class _FakeResp:
    def __init__(self, hidden, cached_tokens, prompt_tokens):
        self._hidden_params = hidden
        self.usage = _FakeUsage(cached_tokens, prompt_tokens)
        self.choices = [type("C", (), {"message": type("M", (), {"content": "x"})()})()]


def _mutate_corpus_and_reingest():  # placeholder for the `mutate_corpus` fixture's helper
    # NOTE: data/corpus.jsonl is a tracked repo file. The real fixture must edit it,
    # re-run app.ingest, AND restore the original content in teardown so the mutation
    # never leaks into other tests or the working tree (testing.md fixtures table).
    ...
