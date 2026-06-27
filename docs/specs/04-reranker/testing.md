# Reranker — test & verification plan

How each [acceptance criterion](README.md#acceptance-criteria) is proven, the
fixtures needed, and how it gates merge. Illustrative tests are in
[`examples/example_tests.py`](examples/example_tests.py) and the harness in
[`examples/retrieval_eval.py`](examples/retrieval_eval.py); port them to
`tests/test_rerank.py` and `evals/` when implementing.

## The gate today (and what this adds)

There is **no `.github/` Actions workflow** in this repo yet — "CI" is
`uv run pytest` (Makefile `test:` / README), and the literal merge gate is
`tests/test_evals.py::test_quality_gate`, which runs the agent over
`evals/golden.jsonl` and asserts `mean >= 0.7`. That gate is **unchanged** by this
feature and stays the merge gate.

This feature adds a **new hermetic suite** `tests/test_rerank.py`, collected by
the same default `uv run pytest`. It needs **no** DB, gateway, or provider key, so
it gates on every run regardless of secrets. Two layers:

| Layer | Stack needed | Runs in the gate? |
| --- | --- | --- |
| `tests/test_rerank.py` (unit + hosted contract, mocked) | none (hermetic) | **yes**, always |
| `tests/test_evals.py::test_quality_gate` (pre-existing) | full stack (DB + gateway + key) | yes, unchanged |
| `evals/retrieval_eval.py` (hit@k / MRR) | full stack + a backend | **no** — *reported* in the PR, not asserted (corpus too small for a stable threshold) |

> A *bare* `uv run pytest` without the stack will still fail on the pre-existing
> non-hermetic `test_quality_gate` — that is out of scope and unchanged. The
> rerank suite is designed so `uv run pytest tests/test_rerank.py` passes
> standalone with nothing running.

## Fixtures / inputs needed

- **Synthetic candidate list** — `CANDS = [(10,"alpha"),(20,"beta"),(30,"gamma"),(40,"delta")]`; no DB.
- **Frozen-Settings patch helper** — `dataclasses.replace(retrieval.settings, rerank_backend=...)` assigned to `app.retrieval.settings` (the dispatcher reads the module attribute per call). Direct `setattr` on the frozen instance raises `FrozenInstanceError`.
- **Mocked gateway** — `monkeypatch.setattr(gateway, "rerank", ...)` for dispatcher tests; `monkeypatch.setattr(gateway.httpx, "post", ...)` for the contract test.
- **In-memory span exporter** — `InMemorySpanExporter` + `SimpleSpanProcessor` attached to the existing provider, to assert `rerank.*` attributes.
- **Gold labels** — `evals/retrieval_gold.jsonl` (keyed on `source`); resolved to ids via `SELECT id, source` at eval time (harness only, not the unit gate).
- **No real key.** The local backend's `ImportError` fall-back is exercised by the *real* lazy import failing in the default env (sentence-transformers not installed).

## Acceptance criteria → proof

> **Mapping note.** The README lists 16 acceptance-criteria checkboxes; the table
> below groups them into rows (some README criteria are restated, e.g. the uv-group
> criterion appears twice). The two hosted-contract invariants the README calls out
> separately — *POST the gateway alias, not a provider id*, and *slice to `top_n`
> when the provider returns more* — are given their own rows (1a, 1b) so each has an
> explicit proof rather than being folded into row 1.

| # | Acceptance criterion | How it is proven |
| --- | --- | --- |
| 1 | `rerank()` reorders via the chosen backend; provider swap is config-only | `test_hosted_reorders_by_index` (reorders by returned `index`); provider-swap-is-config asserted by review of `litellm_config.snippet.yaml` (no app code in the swap) + `test_gateway_rerank_contract` (app never names a provider) |
| 1a | Hosted POSTs the gateway **alias** (`settings.rerank_model or "rerank"`), never a provider model id (posting a raw id would 400 into permanent silent fail-open) | `test_gateway_rerank_contract` asserts `captured["json"]["model"] == "rerank"` |
| 1b | Hosted mapped result is sliced to `top_n` even when the provider returns **more** than `top_n` (and the documented "provider returns fewer" exception is *not* padded) | `test_hosted_reorders_by_index` (provider returns 4, `top_n=2` → 2 results, sliced); the "returns fewer" exception is design-documented, not asserted |
| 2 | `RERANK_BACKEND=none` reproduces today's identity | `test_none_is_identity` (`== candidates[:top_n]`) |
| 3 | New config follows convention; dispatcher reads module-level `settings` so `dataclasses.replace` patch takes effect | The `use_backend` helper patches `app.retrieval.settings`; **every** routing test depends on that patch being observed — if the dispatcher captured an import-time alias, they'd all fail |
| 4 | Fail-open on hosted outage/timeout/error; span `rerank.fell_back=true` | `test_fail_open_on_backend_error` (RRF order returned) + `test_span_records_fell_back` (`fell_back is True`) |
| 5 | Fail-open on misconfig: local group absent + unknown backend | `test_fail_open_local_dependency_missing` (real `ImportError`) + `test_fail_open_unknown_backend` |
| 6 | Empty / single-candidate handled without backend call or error | `test_degenerate_input_no_backend` (parametrized `[]` and 1-item; backend stub asserts it is never called) |
| 7 | `rerank-local` installs via uv group; default `uv sync` pulls no torch | CI/manual check (see below): `uv sync` then assert `sentence_transformers` not importable; `uv sync --group rerank-local` then assert it is. Spec'd in `pyproject.snippet.toml` |
| 8 | `RERANK_POOL` scores at most `fused[:RERANK_POOL]`, returns `top_n`; same count as `none` | `test_same_count_none_vs_backend` (count) + a pool test: set `rerank_pool=2`, stub `gateway.rerank` asserting `len(documents) == 2` |
| 9 | Gold labels key on `source`, resolved to id at eval time | `retrieval_eval._resolve_sources` (`SELECT id, source`) + a unit test of `_hit_and_rr` / resolution against a fake row set (no DB) |
| 10 | Rerank unit/contract tests hermetic, individually runnable, no key/gateway/DB | Run `uv run pytest tests/test_rerank.py` in a clean env (the CI design below); all pass with nothing running |
| 11 | Retrieval-quality delta vs identity is **reported** (hit@k/MRR) for ≥1 real backend | `evals/retrieval_eval.py` prints `none` vs backend + delta; pasted into the PR (not asserted) |
| 12 | Dedicated `rerank` span carries backend/model/candidates/top_n/fell_back + hosted cost when available | `test_span_records_fell_back` asserts the keys; a success-path span test asserts `fell_back is False` and (with a usage-bearing stub) the cost key is a `float` |
| 13 | Local heavy deps are an optional uv group; default install/cold-start unchanged | Same as #7 (one criterion, restated in the README) |
| 14 | Existing eval merge gate (`uv run pytest`) still passes with default config | `test_quality_gate` unchanged; default `rerank_backend=none` means `retrieve()` behaviour is byte-for-byte identical, so the gate is unaffected |

## A note on the "no torch in default install" check (#7/#13)

This is the one criterion a pure unit test can't fully prove (it's about the
*install*, not runtime). Verify it as an explicit step — and the natural home is
the **CI hardening** spec ([07-ci-hardening](../07-ci-hardening/README.md)), which
introduces the first real workflow. Until then it is a documented manual check:

```bash
uv sync && uv run python -c "import importlib.util as u; \
  assert u.find_spec('sentence_transformers') is None, 'default install pulled torch'"
uv sync --group rerank-local && uv run python -c "import sentence_transformers"
```

## Example test (project idiom)

See [`examples/example_tests.py`](examples/example_tests.py) for the full file.
The smallest representative case — fail-open never lets an exception escape the
hot path:

```python
def test_fail_open_on_backend_error(monkeypatch):
    use_backend(monkeypatch, "cohere", rerank_model="rerank-english-v3.0")
    monkeypatch.setattr(gateway, "rerank",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("503")))
    assert retrieval.rerank("q", CANDS, top_n=3) == CANDS[:3]  # RRF order, no raise
```

## Rollout verification

- Merge with `RERANK_BACKEND=none` (default): the gate proves it's a no-op (#14).
- Enable `local`: `uv sync --group rerank-local`, set `RERANK_BACKEND=local` +
  `RERANK_MODEL`, run `make ask` / `evals/retrieval_eval.py`, confirm a `rerank`
  span appears and `fell_back=false`.
- Enable hosted: add the `rerank` entry to `litellm_config.yaml` + provider key in
  the **gateway** env, set `RERANK_BACKEND=cohere`. Confirm the contract path
  (`/rerank`) resolves (not a 404 fall-back) and cost/usage land on the span.
