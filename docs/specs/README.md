# Feature Specs

One directory per feature, each with an expandable `README.md` spec (and room for
design notes, diagrams, sub-specs). Seeded from the [roadmap](../ROADMAP.md);
expand each in its own PR. Every spec maps to one GitHub issue / board item.

## Board structure

**Project:** "AI Stack Roadmap" (GitHub Projects v2).

**Status field (columns):** `Backlog → Todo → In Progress → In Review → Done`.

**Custom fields:**
- **Tier** — `Next` · `Later` · `Horizon`
- **Area** — `gateway` · `ingestion` · `retrieval` · `eval` · `ci` · `observability` · `governance` · `safety` · `orchestration`
- **Size** — `S` (≤1d) · `M` (2–4d) · `L` (~1wk) · `XL` (multi-wk / epic)

**Labels:** `type:feature` / `type:chore`, plus `area:*`, `tier:*`, `size:*`.

**Milestones:** `Next` (near-term batch). Later/Horizon tracked by the **Tier** field.

| Roadmap status | Board column | Notes |
| --- | --- | --- |
| ✅ Shipped | Done | base slice; no issues |
| 🔄 In review | In Review | link PRs #1, #2, #3 — no new spec |
| 🔜 Next | Todo | milestone `Next` |
| 🧭 Later | Backlog | `tier:later` |
| 🌅 Horizon | Backlog | `tier:horizon` |

## In review (link to PRs)

- Eval as a production release gate → PR #1 (`area:eval`)
- Hybrid ingestion → PR #2 (`area:ingestion`)
- CI: lint + self-enforcing eval gate → PR #3 (`area:ci`)

## Next

| Feature | Area | Size | Depends on |
| --- | --- | --- | --- |
| [Model failover / resilience](01-model-failover/README.md) | `gateway` | `M` | — |
| [Canonical document model](02-canonical-document-model/README.md) | `ingestion` | `M` | PR #2 |
| [Real layout backends](03-real-layout-backends/README.md) | `ingestion` | `L` | [canonical-document-model](02-canonical-document-model/README.md) |
| [Reranker](04-reranker/README.md) | `retrieval` | `S` | — |
| [Retrieval uses chunk metadata](05-retrieval-uses-chunk-metadata/README.md) | `retrieval` | `S` | PR #2 |
| [Eval-set maturity](06-eval-set-maturity/README.md) | `eval` | `M` | PR #1 |
| [CI hardening](07-ci-hardening/README.md) | `ci` | `S` | PR #2, PR #3 |

## Later

| Feature | Area | Size | Depends on |
| --- | --- | --- | --- |
| [Caching (semantic + prompt)](08-caching/README.md) | `gateway` | `M` | — |
| [Guardrails](09-guardrails/README.md) | `gateway` | `M` | — |
| [Budgets & virtual keys](10-budgets-and-virtual-keys/README.md) | `gateway` | `M` | — |
| [Agent orchestration (multi-node)](11-agent-orchestration/README.md) | `orchestration` | `L` | — |
| [Context management](12-context-management/README.md) | `orchestration` | `M` | [agent-orchestration](11-agent-orchestration/README.md) |
| [Structured outputs](13-structured-outputs/README.md) | `orchestration` | `S` | — |

## Horizon

| Feature | Area | Size | Depends on |
| --- | --- | --- | --- |
| [Observability backend](14-observability-backend/README.md) | `observability` | `M` | — |
| [Governance & audit](15-governance-and-audit/README.md) | `governance` | `XL` | [observability-backend](14-observability-backend/README.md) |
| [Data residency](16-data-residency/README.md) | `governance` | `M` | [model-failover](01-model-failover/README.md) |
| [Safety & red-teaming](17-safety-and-red-teaming/README.md) | `safety` | `L` | PR #1 |
| [Multi-modal ingestion](18-multi-modal-ingestion/README.md) | `ingestion` | `L` | [canonical-document-model](02-canonical-document-model/README.md), [real-layout-backends](03-real-layout-backends/README.md) |

---

*Each `README.md` follows the same template: Summary · Problem · Goals · Non-goals
· Proposed design · Acceptance criteria · Dependencies · Open questions · Risks ·
Test & rollout · References. Expand a spec in the PR that implements it, set its
`issue:` frontmatter when the issue is created, and keep the [roadmap](../ROADMAP.md)
status in sync.*
