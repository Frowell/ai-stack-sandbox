# Reranker — design notes

Deeper notes behind [`README.md`](README.md): alternatives considered, interface
sketches, the failure taxonomy, and edge cases. Illustrative code lives in
[`examples/`](examples/); the verification plan in [`testing.md`](testing.md).

## Why this shape

The repo's whole thesis is **modularity behind seams**. There are two relevant
seams already:

- **`rerank()` in `app/retrieval.py`** — a same-signature hook the hot path calls.
- **`app/gateway.py`** — the model-call seam; the app names *aliases*, never a
  provider.

This feature keeps both. The reranker is selected by config (`RERANK_BACKEND`),
the hosted path rides the gateway (a `litellm_config.yaml` edit swaps providers),
and the local path stays in-process (a cross-encoder is not a network call, so
the gateway seam does not apply to it). Nothing in `retrieve()`'s control flow
changes except that the existing `rerank(...)` call now resolves a real backend.

## Interface sketch

```python
# app/retrieval.py — signature UNCHANGED from today
def rerank(query: str,
           candidates: list[tuple[int, str]],
           top_n: int) -> list[tuple[int, str]]:
    """Dispatch to settings.rerank_backend; fail-open to candidates[:top_n]."""
```

Two private backends, plus the existing identity:

```python
def _rerank_local(query, candidates, top_n) -> list[tuple[int, str]]:
    # lazy: from sentence_transformers import CrossEncoder  (inside the fn)
    # model cached in a module global; scores (query, content) pairs.

# app/gateway.py — NEW, mirrors embed()/chat() but uses httpx (no SDK route)
def rerank(query: str, documents: list[str], *, model: str, top_n: int,
           timeout: float) -> list[tuple[int, float]]:
    # POST {gateway_base_url}/rerank with the gateway master key.
    # returns [(index_into_documents, relevance_score), ...]
```

The dispatcher owns ordering and slicing; the hosted backend returns
`(index, score)` pairs and the dispatcher maps `index` back into the
`candidates` list it passed in. This keeps the id↔text mapping in one place and
means the gateway function never needs to know about DB ids.

## The failure taxonomy (what "fail-open" must catch)

The wrapper degrades to `candidates[:top_n]` (RRF order) and sets
`rerank.fell_back=true` on **every** one of these — they are all "the ranker
couldn't run", and retrieval is the hot path that feeds every answer and the eval
gate:

| Class | Trigger | Where it raises |
| --- | --- | --- |
| Backend exception | provider 5xx / malformed body | hosted `gateway.rerank()` |
| Timeout | hosted call exceeds `RERANK_TIMEOUT_S` | `httpx` raises `TimeoutException` |
| Missing dependency | `RERANK_BACKEND=local` without the `rerank-local` group | `ImportError` on the lazy import |
| Unknown backend | `RERANK_BACKEND=banana` | dispatcher's `else` branch |
| Degenerate input | empty / single candidate | guarded *before* dispatch (no backend call, no fall-back flag needed) |

Note the last row is **not** a fall-back — it is a correctness guard. Reranking 0
or 1 candidates is a no-op, so we return early without touching a backend and
without flagging `fell_back` (nothing failed).

A **single** startup/first-fall-back log line makes a misconfigured deployment
(e.g. `local` selected but group not installed) visible instead of silently
serving RRF order forever. Use a module-level "already warned" guard so the hot
path doesn't log on every query.

## Alternatives considered

### Backend selection bound at import vs per call
**Rejected: bind at import.** Reading `settings.rerank_backend` once at import
freezes the choice and makes the frozen-dataclass test seam (see README) useless —
a `dataclasses.replace` patch of `app.retrieval.settings` would never be observed.
**Chosen:** the dispatcher reads `app.retrieval.settings` (module attribute) on
every call. Heavyweight *resources* (the cross-encoder model) are still loaded
once and cached in a module global; only the cheap *selection* is per call.

### Hosted rerank: OpenAI SDK vs raw `httpx`
**Rejected: extend `app/gateway.py`'s `OpenAI` client.** `/rerank` is not an
OpenAI SDK route; there is no `_client.rerank(...)`. Faking it through
`client.post()` couples us to SDK internals. **Chosen:** a thin `httpx.post` to
`{gateway_base_url}/rerank` with the gateway master key. `httpx` is already a
transitive dep of `openai`, so no new top-level dependency (the README is explicit:
do **not** add `requests`).

### Local heavy deps: PEP-621 extra vs uv dependency-group
**Rejected: `[project.optional-dependencies]` + `pip install '.[rerank-local]'`.**
The project sets `[tool.uv] package = false` and has no `[build-system]`, so it is
non-packaged — `pip install '.[...]'` cannot resolve an extra of a non-built
project. **Chosen:** a `[dependency-groups] rerank-local` uv group installed with
`uv sync --group rerank-local`, alongside the existing `dev` group. Default
`uv sync` stays lean, so image size and cold-start are unchanged.

### Eval labels: integer DB id vs `source`
**Rejected: hard-code gold ids `1..N`.** `id` is `BIGSERIAL` and `ingest()` does
`TRUNCATE documents RESTART IDENTITY`, so ids are deterministic *today* (corpus
line order) but couple the labels to corpus ordering — reorder/extend the corpus
and the labels silently rot. **Chosen:** label by `source` (the stable semantic
key, e.g. `"retrieval"`), and resolve `source → id` at eval time via
`SELECT id, source FROM documents`.

### Rerank metric vs end-to-end answer score
**Rejected: gate on `app/evals.py`'s mean answer score.** On 7 docs / 4 questions
it will not move measurably with reranking, so it can neither prove the mechanism
nor catch a regression in it. **Chosen:** a dedicated retrieval-quality harness
(hit@k / MRR over gold doc ids), **reported** (not hard-asserted — corpus too
small for a stable threshold). The answer-score gate stays the merge gate,
unchanged.

### Bounding local latency with `RERANK_TIMEOUT_S`
**Rejected.** The local cross-encoder is a synchronous, CPU-bound `predict()`; a
wall-clock budget can't interrupt it without threads/signals (out of scope).
`RERANK_TIMEOUT_S` therefore bounds only the hosted (network) call; local latency
is bounded by `RERANK_POOL` (number of pairs scored).

## Pool semantics (two different "pools")

There are two independent sizes; conflating them is the easy bug:

- `retrieve(pool=10)` — the per-source DB `LIMIT`. `dense` and `sparse` each
  return ≤10, so RRF emits up to ~20 unique fused candidates.
- `RERANK_POOL` (default 10) — how many of the **fused** list the reranker
  actually scores: `fused[:RERANK_POOL]`. Independent of `retrieve`'s `pool`. If
  `RERANK_POOL > len(fused)`, score all of them.

The reranker returns the best `top_n` of the scored slice. `none` returns
`fused[:top_n]`. For the same query, `none` and a configured backend return the
**same number** of results (`top_n`) — only the order differs.

## Edge cases

- **`top_n >= len(candidates)`** — reranking still defined (reorders all); slice
  is a no-op. `none` returns the whole list.
- **Duplicate ids across dense/sparse** — already deduped by `rrf()` (keyed on
  `doc_id`); the reranker sees unique ids.
- **Hosted returns fewer results than requested** (some providers cap or drop) —
  map whatever indices come back; if the mapped list is shorter than `top_n`,
  that is what we return (do not pad). A returned `index` out of range → treat as
  a malformed body → fail-open.
- **Gateway route drift** — LiteLLM serves both `/rerank` and `/v1/rerank`. Pin
  one path in `gateway.rerank()` and assert it in the contract test so a gateway
  upgrade can't silently 404 into the fail-open path (an outage that looks like
  "rerank just stopped helping").
- **Span attribute typing** — OTel attributes are typed and the `span()` helper
  drops `None`. Emit cost/usage as `float`/`int` or omit the key; never pass a
  string-encoded number or `None`.
- **Redis embed cache** — unrelated to rerank, but the existing cache keys on
  `hash(text)` (salted per process via `PYTHONHASHSEED`), so it never hits across
  restarts. A future rerank cache must **not** copy that pattern (use a stable
  hash). Flagged, not fixed here.

## Observability detail

One child span per rerank, `span("rerank", ...)`, nested under `retrieve`:

| Attribute | Type | Notes |
| --- | --- | --- |
| `rerank.backend` | str | `none`/`local`/`cohere`/`voyage` |
| `rerank.model` | str | resolved model id (omit for `none`) |
| `rerank.candidates` | int | `len(candidates)` seen by the dispatcher |
| `rerank.top_n` | int | requested top-N |
| `rerank.fell_back` | bool | `true` on any failure-class fall-back |
| `gen_ai.usage.*` / cost | int/float | hosted only, when LiteLLM returns it; omit otherwise |

There is no OTel GenAI convention for rerank yet, so `rerank.*` are custom keys;
`gen_ai.usage.*` reuses the existing GenAI namespace already used elsewhere in
the app.
