# Feature Roadmap

A living view of where `ai-stack-sandbox` is and where it's going. The repo is a
runnable reference for a *mature* AI stack — the thesis (see the corpus) is that
maturity is **modularity**: clean seams between layers connected by open
standards (OpenTelemetry, an OpenAI-compatible gateway contract, MCP), so any
layer is swappable. The roadmap is organized by that layering.

**Status legend:** ✅ shipped (on `main`) · 🔄 in review (open PR) · 🔜 next ·
🧭 later · 🌅 horizon

**How items graduate:** every change ships behind the gateway/config seam (no
provider names in app code) and must pass the eval gate in CI before merge.
Nothing reaches `main` on vibes.

---

## ✅ Shipped — the working slice

| Layer | What's there |
| --- | --- |
| **Gateway seam** | LiteLLM in the hot path; app speaks the OpenAI-compatible protocol to an alias (`chat`/`embeddings`), never a provider. Provider choice is a `litellm_config.yaml` edit. |
| **Retrieval** | Hybrid: pgvector dense (cosine/HNSW) + Postgres full-text sparse, fused with Reciprocal Rank Fusion, with a rerank hook. |
| **Orchestration** | LangGraph agent (`retrieve → generate`) with typed state. |
| **Observability** | OpenTelemetry GenAI spans beside the hot path; OTLP export is opt-in (no endpoint → no-op). |
| **Evaluation** | Golden-set gate (keyword + LLM-judge) wired into `pytest`. |

## 🔄 In review — open PRs

- **#1 — Eval as a production release gate.** Two independent gates (unweighted
  `overall` + business-value-`weighted`), per-slice hard floors for high-value
  slices, baseline-diff regression detection, N-samples per case for
  non-determinism, a pinned judge decoupled from the system under test,
  served-model pins, and thresholds/weights as versioned config.
- **#2 — Hybrid ingestion.** Routes each source by whether it has exploitable
  layout: layout extraction (Markdown/HTML/CSV defaults; PDF/DOCX/XLSX as a
  pluggable `LayoutExtractor` interface) → structure-aware chunking, with
  semantic chunking as the fallback. Footnotes are merged into the chunk that
  cites them.
- **#3 — CI.** `lint` on every PR; a secret-gated `eval-gate` that stands up
  Postgres + the gateway and runs the eval suite, so the gate enforces itself.

---

## 🔜 Next — highest leverage, mostly designed already

- **Model failover / resilience.** Same-model, multi-provider redundancy
  (e.g. Anthropic + Bedrock on the same model) as active/active load balancing,
  plus ordered `fallbacks` for primary→standby. Keep cross-*model* fallback
  deliberate and observable (a different model is a different behavior contract).
  Surface the served model in responses and metrics. *(Design settled in
  discussion; needs the config + a smoke test.)*
- **Canonical document model.** Promote the implicit `LayoutDoc`/`Element` into a
  versioned, documented contract every extractor normalizes to: a closed block
  taxonomy, optional provenance locators (page / bbox / char offsets), stable
  IDs, and footnotes as a typed `footnote_ref` relation (robust across backends
  that don't keep inline markers). This is the extraction-layer analog of the
  gateway seam. Spec: [`docs/CANONICAL_MODEL.md`](CANONICAL_MODEL.md). Lock the
  schema now; the `locator`/relation refactor of `layout.py` rides with the first
  geometry-bearing backend (PDF), which is the first real producer of locators.
- **Real layout backends.** Implement the registered stubs: `pymupdf` (PDF),
  `python-docx` (DOCX), `openpyxl` (XLSX); `docling` as a heavier opt-in — each
  normalizing to the canonical model. One golden fixture + extractor test per
  format.
- **Reranker.** Replace the identity `rerank()` with a cross-encoder or a hosted
  reranker (Cohere/Voyage); prove the lift through the eval gate, not assertion.
- **Retrieval uses chunk metadata.** Surface section path + merged footnotes into
  the generate step and citations now that chunks carry them.
- **Eval-set maturity.** Grow the golden set from production traces, expand slice
  coverage, record the first real baseline, and add per-slice statistical-
  significance and cost/latency budgets as gates.
- **CI hardening.** Tighten `uv sync` to `--frozen` (lock is now consistent),
  cache deps, make `lint`/`eval-gate` required via branch protection.

## 🧭 Later — depth on each seam

- **Caching.** Gateway semantic cache + a deliberate prompt-caching strategy
  (stable-prefix discipline); track cache-hit rate as a first-class metric.
- **Guardrails.** Input/output validation, PII handling, and prompt-injection
  defense at the gateway seam (the non-spoofable operator channel).
- **Budgets & virtual keys.** Per-tenant virtual keys, budgets, and rate limits
  through the gateway.
- **Agent orchestration.** Decompose the single agent into 3–8 specialist nodes
  with focused prompts/tools and durable, checkpointed state; principled
  subagent delegation rather than one mega-agent.
- **Context management.** Compaction / context-editing for long runs; cross-
  session memory.
- **Structured outputs.** Schema-constrained responses wherever the app parses
  model output, to kill brittle string parsing.

## 🌅 Horizon — observability, governance, safety

- **Observability backend.** Wire a real backend (Langfuse/Phoenix/Braintrust);
  link traces to eval results; sample online evals from live traffic.
- **Governance & audit.** Ahead of EU AI Act high-risk obligations (Aug 2026):
  retained, queryable traces tied to a risk classification; human-in-the-loop
  escalation gates with full audit trails on high-stakes decisions.
- **Data residency.** Inference-geo / region pinning where required.
- **Safety & red-teaming.** Refusal handling with fallbacks; adversarial and
  safety eval suites alongside the quality gate.
- **Multi-modal ingestion.** Images and audio in the ingestion router; high-res
  vision in retrieval/extraction.

---

*This file is the source of truth for direction — update it in the same PR that
moves an item between statuses, so the roadmap and the code never drift.*
