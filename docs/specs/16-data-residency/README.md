---
title: Data residency
slug: data-residency
area: governance
tier: Horizon
size: M
status: Backlog
depends_on: [model-failover]
issue:        # set to the GitHub issue number when created
---

# Data residency

> **Area** `governance` · **Tier** `Horizon` · **Size** `M` · **Status** `Backlog` · **Depends on:** [model-failover](../01-model-failover/README.md)

## Summary

Let a caller require that a request be served from a specific geography (e.g. `eu`,
`us`) and record which region actually handled it. Region selection is a gateway
routing decision expressed through the existing `chat`/`embeddings` aliases, not
app code. Because residency is a *constraint*, an unsatisfiable region must **fail
closed** rather than silently spill to another geography — which is precisely
where this feature collides with, and must bound, [model-failover](../01-model-failover/README.md).

## Problem / Motivation

No control over where inference runs; some workloads (regulated data, contractual
data-locality) require region pinning, and there is currently no record of where a
request was served. Today the stack calls a single global provider deployment
(`openai/gpt-4o-mini`), which exposes no region to pin and returns no region in its
response — so neither pinning nor recording is possible without design work.

## Goals

- **Inference-geo / region pinning through the gateway.** A request declares a
  required region; the gateway routes only to a deployment in that region.
- **Record where inference was routed.** Surface the configured/routed region per
  request (span attribute + response metadata), with an honest distinction between
  *configured* region and *provider-attested* region (see Open questions).
- **Bound failover by residency.** A residency-constrained request may only fail
  over within the same region; cross-region failover is forbidden for such
  requests.
- **Cover both inference paths** — `chat` *and* `embeddings`. Embeddings carry
  document/query text out of region just as chat does (see `app/ingest.py`, which
  embeds the entire corpus).

## Non-goals

- Building region infrastructure (rely on provider regions: Azure OpenAI regional
  deployments, AWS Bedrock regions, Vertex AI locations).
- Residency of **data at rest** — pgvector embeddings and the Redis query-embedding
  cache are derived data and out of scope here (called out as an accepted risk
  below, not silently ignored).
- Cryptographic/contractual *attestation* that a provider physically served from a
  region (best-effort recording only; see Open questions).
- Per-tenant residency policy / virtual-key enforcement (belongs with
  [budgets-and-virtual-keys](../10-budgets-and-virtual-keys/README.md) and
  [governance-and-audit](../15-governance-and-audit/README.md)).

## Proposed design

Lives behind the **gateway seam** (`gateway/litellm_config.yaml`) plus a thin
request-tagging convention in `app/gateway.py`. No provider names enter app code.
The graph *orchestration* in `app/agent.py` is unchanged, but `app/gateway.py` is
**not** a no-op edit — see "Seam changes required", below. (The earlier framing of
"zero app code" was inaccurate against the current signatures.)

1. **Region-scoped deployments in LiteLLM.** Register the same alias multiple
   times, one per region, tagged by region — e.g. `chat` with
   `litellm_params.tags: ["region:eu"]` pointing at an EU deployment and another
   at a US deployment. Same for `embeddings`. This reuses LiteLLM's tag-based
   routing rather than inventing per-region alias names (`chat-eu`, `chat-us`),
   so the app keeps calling `chat`/`embeddings`. Enable it with
   `router_settings.enable_tag_filtering: true`.
2. **Expressing the requirement.** The caller passes a `region` to the gateway
   helpers; `app/gateway.py` translates it into the request body LiteLLM reads for
   tag routing — **not** as a raw SDK kwarg. The current `chat(messages, **kwargs)`
   forwards `**kwargs` straight into `client.chat.completions.create(...)`, where a
   top-level `region="eu"` is an unknown argument: the OpenAI SDK raises
   `TypeError`, or (under LiteLLM's `drop_params: true` — which **is** set in the
   current `gateway/litellm_config.yaml`) it is silently dropped and the constraint
   is **lost** — the worst outcome for a fail-closed feature. The helper must
   instead route the constraint through LiteLLM's tag channel via `extra_body`.
   **The exact body shape is version-dependent and must be confirmed, not
   assumed:** depending on the pinned LiteLLM version, tags are read from either
   `extra_body={"tags": [...]}` *or* `extra_body={"metadata": {"tags": [...]}}`.
   Picking the wrong field silently sends a tag the router never reads — i.e. the
   constraint is dropped and the request spills out of region, the same
   fail-*open* hazard as the raw kwarg. The implementation pins the version and
   the routing test (below) asserts the *actually-effective* shape end-to-end (the
   request routes in-region / fails closed), not merely that "some tag was sent."
   Unset region = "no constraint" → current behavior (backward compatible).
3. **Fail closed.** If no deployment matches the required region, LiteLLM must
   return an error the app surfaces; it must **not** fall back to an out-of-region
   deployment. Two concrete config invariants: (a) with `enable_tag_filtering`, a
   constrained alias must have **no untagged "default" deployment** — LiteLLM
   routes a tagged request to *untagged* deployments when no tag matches, so an
   untagged `chat`/`embeddings` entry would be a silent cross-region spill; (b) the
   model-failover `fallbacks` chain must be region-scoped so it never crosses a
   region boundary for a constrained request. Both are asserted by the fail-closed
   acceptance test.
   - **(c) Tension with backward compat — the unconstrained request must still
     route (load-bearing).** Invariant (a) forbids an untagged "default"
     deployment, but the backward-compat goal requires an *unconstrained* (no
     `region`) request to keep working. Under `enable_tag_filtering`, a request
     carrying **no** tag routes only to untagged deployments (or to a deployment
     bearing a `default` tag) — so once every deployment is region-tagged and (per
     (a)) no untagged default exists, an unconstrained request would have **no
     eligible deployment and fail closed**, silently breaking every request that
     does not pin a region. Invariant (a) and the backward-compat criterion are
     therefore mutually exclusive unless unconstrained routing is handled
     *explicitly*, not by an untagged catch-all. **Resolution:** unconstrained
     requests are routed via an explicit `default` tag — exactly one region's
     deployments also carry `tags: ["region:<r>", "default"]`, and the gateway
     attaches the `default` tag (not "no tag") when no `region` is supplied. This
     keeps "no *untagged* deployment" true (fail-closed preserved for an unmatched
     *region* tag) while giving unconstrained traffic a deterministic home region.
     Consequence to state plainly: "behaves exactly as today" now means *routed to
     the configured default region*, which is a deliberate, documented choice of
     default region — see the (now load-bearing) default-region open question. The
     exact tag-filtering semantics for a no-tag-vs-all-tagged request are
     version-dependent and MUST be confirmed against the pinned LiteLLM version and
     locked by the config-invariant + backward-compat tests (below), not assumed.
4. **Recording.** Add a `gen_ai.residency.region` (configured/requested) attribute
   to the `generate` span in `app/agent.py`. Recording the *requested* region is
   trivial (the call site knows it). **Scope note for embeddings (corrected
   against the code):** the two embed call sites differ. The *query* path **does**
   run inside an existing span — `app/retrieval.py::retrieve` opens a `retrieve`
   span (line 84) and, within it, `dense()` → `_cached_embed()` → `embed()` — so
   `gen_ai.residency.region` for the query embedding can be hung on that existing
   `retrieve` span with **no new span** (recorded only on cache *misses*; see the
   cache note under Risks). The *ingest* path is the only genuinely span-free
   site: `app/ingest.py` calls `embed()` bare, outside any span. So the earlier
   framing ("`_cached_embed` calls it inside no span") was inaccurate — the query
   path needs only an attribute, not a structural change; only ingest recording
   needs a new span. *Routing/fail-closed* for both embed paths is in scope and
   tested (see below); *recording* is: query path = attribute on the existing
   `retrieve` span (cheap), ingest path = either a minimal `embed`/`ingest` span
   or an explicit, documented deferral (chat recording is the must-have). Do **not** leave it implicitly assumed —
   the `generate`-span mechanism covers chat only. A **provider-attested**
   `served_region`,
   however, requires response metadata/headers that the gateway currently
   discards: `chat()` returns only `resp.choices[0].message.content` and `embed()`
   only the vectors. So `gen_ai.residency.served_region` is in scope **only if**
   the gateway return shape is widened to surface that metadata (see Seam changes
   and the attestation Open question); otherwise it is explicitly deferred. The
   two attributes are never conflated.

### Seam changes required (small, but real — not "zero code")

- `app/gateway.py::chat` — add optional `region`; translate to `extra_body` tags.
  Do **not** forward `region` as a raw kwarg.
- `app/gateway.py::embed` — **currently takes no `**kwargs` at all**; it must gain
  an optional `region` parameter so the ingest and query embedding paths can be
  pinned. Without this, the embeddings goal is unreachable through the seam.
- `app/agent.py` / `app/ingest.py` — graph topology unchanged, but the `generate`
  span gains the `gen_ai.residency.region` attribute and call sites must thread a
  region through. Attribute/argument-level, not structural.
- (Optional, for attestation) widen `chat`/`embed` return values to carry response
  metadata if `served_region` is pursued.

Schema/config/API changes: `litellm_config.yaml` gains region-tagged deployments
and `enable_tag_filtering`; `app/gateway.py` gains an optional `region` parameter
on **both** `chat` and `embed`, translated into `extra_body` tags; new span
attributes (no DB migration).

## Acceptance criteria

- [ ] A `chat` request tagged `region:eu` is served only by an EU deployment; a
  request requiring a region with **no** matching deployment **fails closed**
  (explicit error), never silently served elsewhere — demoed both ways.
- [ ] The region is carried through LiteLLM's `extra_body` tag channel
  (`region:<r>`), **not** a raw SDK kwarg; a test asserts no `region=` argument is
  passed to `client.*.create` (so it can't be eaten by `drop_params`). The test
  proves the tag is in the *effective* field for the pinned LiteLLM version by
  asserting end-to-end routing/fail-closed behavior — not merely that "a tag was
  attached" (a tag in the wrong `extra_body` field is silently ignored and spills
  out of region).
- [ ] Config invariant test: for any constrained alias there is **no untagged
  default deployment**, and `enable_tag_filtering` is on — so an unmatched tag
  errors instead of falling through to an untagged deployment.
- [ ] The same holds for `embeddings` (ingest and query paths), proven by a test
  that pins the embedding call — which requires `embed()` to accept a `region`
  parameter (the function currently takes none).
- [ ] A residency-constrained request whose primary in-region deployment is down
  fails over **only** to another in-region standby (or fails closed if none) —
  i.e. the model-failover chain is region-bounded. Demoed against a killed
  in-region primary. **Scope contingency:** this criterion is meaningful for the
  `chat` path because model-failover defines a `chat` fallback chain; model-failover
  currently leaves **embeddings** failover as an open question (possibly out of
  scope, since a different embedding model corrupts the vector space). So
  "region-bounded failover" for embeddings only applies *if* model-failover ships an
  embeddings standby — if it does not, there is no embeddings failover pool to
  bound, and the embeddings residency guarantee reduces to in-region routing +
  fail-closed (no cross-region failover because there is no failover at all). State
  which case holds once model-failover resolves its embeddings open question.
- [ ] The configured/requested region is recorded per request on the **chat** path
  as the `gen_ai.residency.region` span attribute (on the existing `generate`
  span), distinct from any provider-attested `gen_ai.residency.served_region` (no
  conflation). If `served_region` is not implemented (attestation deferred), the
  span carries only the requested region and the deferral is documented.
- [ ] **Embeddings recording is explicitly resolved, not assumed.** The *query*
  path already runs inside the existing `retrieve` span, so its
  `gen_ai.residency.region` is an attribute on that span (recorded on cache misses;
  a test asserts it). The *ingest* path (`app/ingest.py`) is span-free; the spec
  must either add a minimal `embed`/`ingest` span carrying the attribute (with a
  test) or record an explicit ingest-recording deferral. Routing/fail-closed for
  both embed paths is required regardless (covered above); only *recording* may be
  deferred, and only for the ingest path.
- [ ] `app/agent.py` graph topology is unchanged; the only app-code edits are the
  documented gateway-seam additions (`region` param on `chat`/`embed`, tag
  translation, span attribute). "Zero app code" is **not** a criterion.
- [ ] **Unconstrained routing is proven, not assumed (reconciles fail-closed with
  backward compat).** With `enable_tag_filtering` on and **no untagged default
  deployment**, a request with **no** `region` still routes successfully (it does
  not fail closed): a test asserts an unconstrained `chat`/`embeddings` call lands
  on the designated default-region deployment (via the explicit `default` tag),
  while an unsatisfiable *region* tag still fails closed. This is the test that
  catches the (a)-vs-backward-compat contradiction; both must hold simultaneously.
- [ ] An unpinned request behaves exactly as today (backward compatible) — the app
  passes no `region` argument and the caller's request body is unchanged; "as
  today" explicitly means *routed to the configured default region* (see the
  unconstrained-routing criterion above and the default-region open question), not
  "routed to an untagged global pool" (which (a) forbids).

## Dependencies

- [model-failover](../01-model-failover/README.md) — **hard.** Residency must
  *constrain* the failover routing pool, so it depends on how failover expresses
  `fallbacks`/load-balancing pools. **Status update (2026-06):** that pool
  expression is now *specified* — model-failover has an expanded
  [`design.md`](../01-model-failover/design.md) defining load-balanced alias
  entries, the ordered `fallbacks: [{"chat": ["chat-standby"]}]` chain (under
  `router_settings`), and stable `model_info.id` labels. So residency can now be
  *designed* against a concrete pool shape rather than a stub. What remains hard:
  model-failover is still `status: Todo` and **not landed as code**, and the
  region-scoping of those pools (invariant (b)) cannot be tested until the
  failover config actually exists. Residency design can proceed; residency
  *implementation/merge* must wait for failover to land.
- A provider with real regional endpoints wired into the stack (Azure OpenAI /
  Bedrock / Vertex). The default global `openai` deployment cannot demonstrate
  this feature.

## Open questions

- **Attestation gap (load-bearing).** Providers generally do *not* return the
  physical serving region in the response. We can reliably record the region we
  *routed to* (the deployment we selected), but not independently verify the
  provider honored it. Is "configured/routed region" sufficient for the target
  compliance story, or is provider attestation (regional endpoint hostnames,
  contractual guarantees, BAAs) a hard requirement? This determines whether the
  feature is "best-effort routing + audit" or a true compliance control.
- Which concrete provider(s) are the reference implementation for the demo
  (Azure OpenAI deployments vs Bedrock regions vs Vertex locations)?
- **How is the default/unconstrained region chosen (load-bearing for backward
  compat).** With no untagged default deployment (fail-closed invariant (a)),
  "truly unconstrained routing" is not available — an unconstrained request needs
  an explicit `default` tag pointing at a chosen region, so the default region is a
  *policy decision that must be made before implementation*, not a free-fall. Which
  region is the default, and is that acceptable for callers who relied on today's
  single global deployment? This blocks the backward-compat acceptance criterion.
- Does residency policy belong per-request, or per-virtual-key (push to
  [budgets-and-virtual-keys](../10-budgets-and-virtual-keys/README.md))?
- **LiteLLM tag-filtering semantics are version-dependent.** Confirm, against the
  pinned LiteLLM version, the exact fallthrough behavior when a tag matches no
  deployment (error vs. route-to-untagged) and that `enable_tag_filtering` +
  no-untagged-default actually yields a hard error. The fail-closed guarantee
  rests entirely on this; pin the version and lock it with the config-invariant
  test rather than assuming it.

## Risks & mitigations

- **Recorded region ≠ proof of residency.** Recording the routed region can give
  false compliance confidence. *Mitigation:* name the attribute for what it is
  (`...residency.region` = configured), keep any attested value separate, and
  document the limitation in the audit story. *Until resolved this is the single
  blocking question (see Open questions).*
- **Cross-region failover silently breaches residency.** If failover pools are not
  region-scoped, an outage routes EU data to US — the exact harm this feature
  exists to prevent. *Mitigation:* region-bounded failover pools + fail-closed
  default; covered by an explicit acceptance test.
- **Embeddings overlooked.** Pinning only chat leaves the corpus/query embedding
  path leaking out of region. *Mitigation:* embeddings are in scope and tested.
- **Accepted risk — data at rest.** Embedded vectors (pgvector) and cached query
  embeddings (Redis) are derived data stored wherever those services run, outside
  this feature's control. Accepted and documented as a non-goal; revisit if a
  workload requires at-rest residency.
- **Query-embedding cache interacts with recording, not with leakage.** A Redis
  cache *hit* in `app/retrieval.py::_cached_embed` returns a stored vector and
  makes **no** inference call, so a hit cannot leak text out of region (nothing is
  sent) — residency is trivially satisfied. But two consequences follow: (a) a
  cached query produces **no** per-request region record, so the audit trail will
  show region only for cache *misses* (acceptable if recording is "where inference
  ran," but state it); and (b) the cache key is `emb:{hash(text)}` with no region
  component — harmless because the same model yields the same vector regardless of
  serving region, but if region ever changes the embedding *model*, the key must
  gain a region component to avoid cross-region vector reuse. Both are accepted and
  noted, not silently inherited.
- **Gateway seam is not free.** Both `chat` and `embed` need a `region` parameter
  and tag translation, and `embed` has no `**kwargs` today. *Mitigation:* scoped
  explicitly under "Seam changes required"; kept mechanical (no new graph nodes,
  no DB change). The cost is small but must be planned, not discovered.
- **Accepted risk — `served_region` may ship as deferred.** Surfacing a provider-
  attested region needs the gateway to return response metadata it now discards.
  If attestation is judged out of scope for the first cut, only the requested
  region is recorded and the gap is documented (not silently absent).
- **Accepted risk — far-horizon dependency.** Blocked on model-failover (a stub
  with a `_TODO_` design) and on a regional provider being wired in; do not begin
  implementation until both exist.
- **Not ready for full expansion yet (sequencing).** Two hard prerequisites must
  land first: (1) model-failover must define how `fallbacks`/load-balancing pools
  are expressed — residency *constrains* that pool and cannot be designed against a
  stub; and (2) the load-bearing attestation open question must be answered
  (configured-region audit vs. true compliance control), because the answer
  changes the feature's shape and acceptance bar. Expand this spec into a feature
  directory only after both are resolved; expanding now would design against
  moving targets.

## Test & rollout plan

- **Unit/integration:** a test pins `chat` and `embeddings` to a region and
  asserts (a) in-region routing, (b) fail-closed on an unsatisfiable region, and
  (c) failover stays in-region. Routing/fail-closed must be exercised against a
  real LiteLLM router with two *fake* region-tagged deployments (recorded provider
  responses alone can't prove routing — the decision happens in the router), so
  the test needs no real multi-region provider keys (mirrors the secret-gated
  `eval-gate` pattern from PR #3). Add a cheap unit test asserting the gateway
  emits `extra_body` tags and never a raw `region=` kwarg.
- **Observability:** assert the `gen_ai.residency.region` span attribute is set
  (extends the served-model recording introduced by model-failover / PR #1).
- **Rollout:** behind config — region tags are additive in `litellm_config.yaml`;
  unset region preserves today's behavior. No migration. Ship after model-failover
  lands and a regional provider is available.

## References

- [Feature roadmap](../../ROADMAP.md)
- [Specs index](../README.md)
- [model-failover](../01-model-failover/README.md) (routing pools this constrains)
- [governance-and-audit](../15-governance-and-audit/README.md) (where the region
  record feeds the audit trail)
