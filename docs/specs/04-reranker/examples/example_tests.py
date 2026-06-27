"""ILLUSTRATIVE — spec for tests/test_rerank.py, not wired in.

Hermetic: no live provider key, no gateway, no Postgres. Every path is exercised
with stubs/monkeypatch. Renamed `example_tests.py` (not test_*.py) so pytest's
default recursive discovery does NOT collect it while it lives under docs/specs/.
Port to tests/test_rerank.py when implementing.

The frozen-Settings seam: Settings is @dataclass(frozen=True), so
`monkeypatch.setattr(retrieval.settings, "rerank_backend", ...)` raises
FrozenInstanceError. Instead replace the module-level reference the dispatcher
reads, with dataclasses.replace().
"""
import dataclasses

import pytest

from app import gateway, retrieval


def use_backend(monkeypatch, backend: str, **extra):
    new = dataclasses.replace(retrieval.settings, rerank_backend=backend, **extra)
    monkeypatch.setattr("app.retrieval.settings", new)


CANDS = [(10, "alpha"), (20, "beta"), (30, "gamma"), (40, "delta")]


# --- AC: none == identity (opt-in) ------------------------------------------
def test_none_is_identity(monkeypatch):
    use_backend(monkeypatch, "none")
    assert retrieval.rerank("q", CANDS, top_n=2) == CANDS[:2]


# --- AC: empty / single-candidate guard, no backend call --------------------
@pytest.mark.parametrize("cands", [[], [(1, "only")]])
def test_degenerate_input_no_backend(monkeypatch, cands):
    # Even with a hosted backend selected, the guard returns before dispatch.
    use_backend(monkeypatch, "cohere", rerank_model="rerank-english-v3.0")
    called = False

    def boom(*a, **k):
        nonlocal called
        called = True
        raise AssertionError("backend must not be called for <=1 candidate")

    monkeypatch.setattr(gateway, "rerank", boom)
    assert retrieval.rerank("q", cands, top_n=4) == cands
    assert called is False


# --- AC: hosted mapping — reorder by returned index, slice top_n ------------
def test_hosted_reorders_by_index(monkeypatch):
    use_backend(monkeypatch, "cohere", rerank_model="rerank-english-v3.0")
    # Provider says doc at index 2 (gamma) is best, then index 0 (alpha).
    monkeypatch.setattr(
        gateway, "rerank", lambda *a, **k: [(2, 0.9), (0, 0.5), (1, 0.2), (3, 0.1)]
    )
    out = retrieval.rerank("q", CANDS, top_n=2)
    assert out == [(30, "gamma"), (10, "alpha")]


# --- AC: fail-open when hosted backend raises (outage/timeout) --------------
def test_fail_open_on_backend_error(monkeypatch):
    use_backend(monkeypatch, "cohere", rerank_model="rerank-english-v3.0")

    def boom(*a, **k):
        raise RuntimeError("provider 503")

    monkeypatch.setattr(gateway, "rerank", boom)
    assert retrieval.rerank("q", CANDS, top_n=3) == CANDS[:3]  # RRF order preserved


# --- AC: fail-open on misconfig — local group not installed (ImportError) ----
def test_fail_open_local_dependency_missing(monkeypatch):
    # In the default (hermetic) env sentence-transformers is NOT installed, so the
    # real lazy import inside _rerank_local raises ImportError -> fail-open.
    monkeypatch.setattr(retrieval, "_local_model", None)
    use_backend(monkeypatch, "local", rerank_model="cross-encoder/ms-marco-MiniLM-L-6-v2")
    assert retrieval.rerank("q", CANDS, top_n=3) == CANDS[:3]


# --- AC: fail-open on unknown backend value ---------------------------------
def test_fail_open_unknown_backend(monkeypatch):
    use_backend(monkeypatch, "banana")
    assert retrieval.rerank("q", CANDS, top_n=3) == CANDS[:3]


# --- AC: same result COUNT for none vs configured backend -------------------
def test_same_count_none_vs_backend(monkeypatch):
    use_backend(monkeypatch, "none")
    base = retrieval.rerank("q", CANDS, top_n=3)
    use_backend(monkeypatch, "cohere", rerank_model="rerank-english-v3.0")
    monkeypatch.setattr(gateway, "rerank", lambda *a, **k: [(1, 0.9), (0, 0.8), (2, 0.1)])
    treat = retrieval.rerank("q", CANDS, top_n=3)
    assert len(base) == len(treat) == 3


# --- AC: contract — pinned /rerank path, gateway key only, no provider key ---
def test_gateway_rerank_contract(monkeypatch):
    captured = {}

    class FakeResp:
        def raise_for_status(self):
            pass

        def json(self):
            return {"results": [{"index": 0, "relevance_score": 0.7}]}

    def fake_post(url, *, headers, json, timeout):
        captured.update(url=url, headers=headers, json=json, timeout=timeout)
        return FakeResp()

    monkeypatch.setattr(gateway.httpx, "post", fake_post)
    out = gateway.rerank("q", ["a", "b"], model="rerank", top_n=1, timeout=5.0)

    assert out == [(0, 0.7)]
    assert captured["url"].endswith("/rerank")          # pinned path, not /v1/rerank drift
    # The app posts the gateway ALIAS, never a provider model id — so a provider
    # swap is a litellm_config.yaml edit, not an app change.
    assert captured["json"]["model"] == "rerank"
    # Only the gateway master key leaves the app; no provider (Cohere/Voyage) key.
    assert captured["headers"]["Authorization"].startswith("Bearer ")
    assert "COHERE_API_KEY" not in str(captured)       # smoke check, not the guarantee


# --- AC: span carries rerank.fell_back (and friends) ------------------------
def test_span_records_fell_back(monkeypatch):
    from opentelemetry import trace
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
        InMemorySpanExporter,
    )

    exporter = InMemorySpanExporter()
    trace.get_tracer_provider().add_span_processor(SimpleSpanProcessor(exporter))

    use_backend(monkeypatch, "cohere", rerank_model="rerank-english-v3.0")
    monkeypatch.setattr(gateway, "rerank", lambda *a, **k: (_ for _ in ()).throw(RuntimeError()))
    retrieval.rerank("q", CANDS, top_n=2)

    spans = {s.name: s for s in exporter.get_finished_spans()}
    attrs = spans["rerank"].attributes
    assert attrs["rerank.fell_back"] is True
    assert attrs["rerank.backend"] == "cohere"
    assert attrs["rerank.candidates"] == len(CANDS)
    assert attrs["rerank.top_n"] == 2
