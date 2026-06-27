---
title: Eval-set maturity
slug: eval-set-maturity
area: eval
tier: Next
size: M
status: Todo
depends_on: [PR #1, PR #3]
issue:        # set to the GitHub issue number when created
---

# Eval-set maturity

> **Area** `eval` · **Tier** `Next` · **Size** `M` · **Status** `Todo` · **Depends on:** PR #1 (gate machinery), PR #3 (CI eval-gate)

## Summary

The golden set is `n=4` (`evals/golden.jsonl`) with no per-case slice or weight
metadata and no recorded baseline, while the current gate (`app/evals.py`) is a
single mean-score threshold (`THRESHOLD=0.7`). PR #1 introduces the richer gate
contract (weighted `overall` + per-slice floors + baseline-diff regression +
N-samples + pinned judge + versioned config). This feature makes that machinery
*mean something*: it grows and slices the golden set, assigns reviewed
business-value weights, records the first real baseline, and turns cost/latency
into gates. The output is a defensible regression suite, not a 4-row placeholder.

## Problem / Motivation

The golden set is `n=4` with placeholder keyword/reference cases, no slice or
weight metadata, and no recorded baseline -- so the weighted/per-slice gates that
PR #1 adds have nothing real to gate on. With `n=4`, a single flaky case swings
the mean by 0.25 and any "per-slice significance" test is statistically
meaningless. The suite cannot currently catch a real regression.

## Goals

- Grow the golden set (manual curation from production traces) and **expand slice
  coverage** so each high-value slice has enough cases to gate on.
- Define the **golden-case schema** additions (`slice`, `weight`) and assign
  **reviewed** business-value weights.
- Record the first **baseline** (`make eval-baseline` → `evals/baseline.json`) on
  a vetted, reproducible run and turn on baseline-diff regression detection.
- Capture **per-case cost and latency** and add cost/latency **budget gates**.
- Add **per-slice regression handling** that is honest about small-N variance.

## Non-goals

- Automated trace-mining pipeline (later) — curation is manual this pass.
- Changing the gate *contract* itself (that is PR #1's scope); this feature
  populates and parameterises it.
- A trace store / observability backend (spec 14) — traces are sourced manually.

## Proposed design

This lives behind the **eval harness seam** (`app/evals.py` + `evals/*` data +
config), feeding the **CI eval-gate** (PR #3). No gateway or app-runtime change.

> **Companion docs (this directory):** deeper rationale in [`design.md`](design.md)
> (seams, alternatives, gate order, edge cases); concrete codebase-specific shapes
> in [`examples/`](examples/) (illustrative — a spec, not wired-in code); and the
> proof-of-each-criterion + merge-gating plan in [`testing.md`](testing.md).

**Components & files touched.** Data + config + harness only:

| File | Change | Example |
|---|---|---|
| `evals/golden.jsonl` | grown to ≥20 cases with `id`/`slice`/`weight`/`expect` | [`examples/example_golden.jsonl`](examples/example_golden.jsonl) |
| `evals/gate_config.yaml` *(new)* | floors, `slices.high_value`, `slices.min_cases`, weights policy, regression delta, N, price map, budgets (`mean_cost_per_case`, p95 latency) | [`examples/example_gate_config.yaml`](examples/example_gate_config.yaml) |
| `evals/baseline.json` *(new, committed)* | per-case/slice/overall scores + pins + timestamp | [`examples/example_baseline.json`](examples/example_baseline.json) |
| `app/evals.py` | config load + schema validation + slice routing + N-sampling + cost/latency + baseline diff + all gates; `THRESHOLD` removed | [`examples/example_evals.py`](examples/example_evals.py) |
| `app/gateway.py` | usage-capture seam (`chat(..., return_usage=...)`); default `str` return unchanged | [`examples/example_gateway.py`](examples/example_gateway.py) |
| `app/agent.py` | sampling seam (`ask(q, *, gen_kwargs=..., return_meta=...)`); default behaviour unchanged | [`examples/example_agent.py`](examples/example_agent.py) |
| `tests/test_evals.py` | precise skip predicate + one test per acceptance criterion | [`examples/example_tests.py`](examples/example_tests.py) |
| `Makefile` | `eval-baseline` target (deliberate, human-run; never CI) | [`examples/example_Makefile.snippet`](examples/example_Makefile.snippet) |
| `pyproject.toml` | add explicit `pyyaml>=6` (today only transitive) for the YAML config loader | [`examples/example_pyproject.snippet.toml`](examples/example_pyproject.snippet.toml) |

**Data flow (gate run).** Structural validation runs *before* any model call so a
bad set can never be masked by a passing score; gates are then evaluated in a fixed
order and the run exits non-zero if **any** blocking gate fails (full ordering in
[`design.md`](design.md) §2):

```
gate_config.yaml ─┐
golden.jsonl ─────┤─ load + VALIDATE (bad row/unknown slice/dup id/bad weight/bad expect/
                  │                   high-value slice < min_cases → non-zero exit, no model call)
                  ▼
per case × N samples (temperature=0):
   ask(q, gen_kwargs, return_meta) ─→ answer + usage   (served-model seam)
   score: factual (0.5·kw + 0.5·judge)  OR  decline judge   (routed by `expect`)
   cost: price_map · usage (served-model only)   latency: wall clock
                  ▼
aggregate: per-slice means · weighted overall · mean served cost/case (total reported) · p95 latency
                  ▼
GATES (all reported): overall floor · per-slice floors · baseline-diff (overall +
   per-slice + per-case, id-aware) · mean-cost-per-case budget [blocking] · p95 latency [advisory]
                  ▼
exit 0/1  ──→  make eval / pytest  ──→  CI eval-gate (PR #3)
```

**1. Golden-case schema (versioned).** Extend each `evals/golden.jsonl` row with:
```jsonc
{
  "id": "stable-unique-id",          // for baseline diffing across runs
  "slice": "retrieval|reasoning|...", // one of the enumerated slices below
  "weight": 3,                        // business value, integer 1..5
  "expect": "answer",                // "answer" (default) | "decline" -> selects scorer
  "question": "...", "keywords": [...], "reference": "..."
}
```
Cases without `slice`/`weight`/`expect` default to `slice="unsliced"`,
`weight=1`, `expect="answer"` (keeps old rows valid). Schema is validated at load
time; the run exits non-zero if any of the following hold: a malformed row, an
**unknown slice**, a **duplicate `id`**, a `weight` outside the integer range
`1..5`, an `expect` other than `answer`/`decline`, or a **high-value slice with
fewer than `slices.min_cases` cases** (default 4 — see gate below). The min-cases
check makes the "≥4 cases per high-value slice" acceptance criterion
*self-enforcing* rather than review-only: deleting cases out of a high-value slice
(or routing a regression around it by emptying it) fails the run, and an empty
high-value slice can never make its per-slice floor pass vacuously.

**2. Config, not constants.** Thresholds, per-slice floors, weights policy, and
budgets move to a versioned `evals/gate_config.yaml` (loaded by PR #1's gate).
Reviewing weights = a PR that edits this file + the per-row `weight`, with a
human sign-off in the PR description. The hardcoded `THRESHOLD=0.7` in
`app/evals.py` is replaced by `gate_config.yaml: overall.floor`.

**3. Baseline.** `make eval-baseline` runs the suite with the **pinned served
model + pinned judge** (from PR #1) at `temperature=0`, **N samples per case**
(default `N=3`, mean per case) to damp judge non-determinism, and writes
`evals/baseline.json` (per-case + per-slice + overall scores, plus the model/judge
pins and timestamp). Regeneration is a deliberate, reviewed action — the gate
compares the live run to this committed file; a drop beyond `regression_delta`
fails. Baseline is never auto-updated by CI.

*Sampling control seam.* temperature=0 and N independent samples cannot be
requested today: `ask()` forwards no kwargs and `generate_node` hard-codes the
`chat()` call. The eval/baseline path must reach the served model with explicit
sampling params **without** changing the hot-path default — either `ask()` gains
an optional `**gen_kwargs` it forwards to `chat()`, or the eval harness drives the
graph through a sampling-aware entrypoint. This seam is shared with PR #1 (which
owns N-samples + pins); agree the exact signature at PR #1 integration. Until it
exists, baseline runs are not reproducible and this work cannot land.

*Baseline-diff lifecycle (per `id`).* The diff is keyed on stable `id`. A case
present in the live run but **absent from `baseline.json`** (newly added) is
exempt from the regression-delta check until the next deliberate baseline
regeneration — it is still subject to per-slice and overall floors. A case in the
baseline but **absent from the live run** (removed/renamed `id`) is ignored, not
treated as a regression. Changing a case's `question`/`reference` without changing
its `id` is a reviewer error the redaction/weights review must catch.

*Evasion vector (accepted risk).* Because a renamed `id` reads as one removed
case (ignored) plus one newly-added case (exempt from the regression-delta until
the next baseline regen), `id` churn can route a regressed answer *around* the
baseline-diff gate. The **per-slice hard floor and the weighted-overall floor
still apply** to the renamed case, so a true quality drop is not invisible — but
the *delta* check is bypassed. Mitigation is review-side: baseline regeneration is
a deliberate reviewed PR, and any change to the set of `id`s (adds/removes/renames)
must be called out and scrutinised in that PR. No automated `id`-stability check
this pass. **Accepted risk.**

**4. Cost/latency capture.** `chat()` currently returns only
`resp.choices[0].message.content` and **discards the response object**, so usage
is unreachable to any caller — a "thin wrapper around `chat()`" *cannot* recover
tokens. The capture path therefore needs a real seam, not a wrapper:

- **Tokens:** add an opt-in, backward-compatible return on the gateway seam —
  `chat(messages, *, return_usage=False)` returns the bare string by default and
  `(content, usage)` when `return_usage=True` (or an equivalent
  `chat_with_usage()` helper). Existing hot-path callers are untouched; only the
  eval path opts in. `usage` carries `prompt_tokens` / `completion_tokens`
  (fall back to `0` and mark the case `usage_estimated=true` if the gateway omits
  usage, e.g. for a streamed or judge-only response).
- **Latency:** measured by the eval harness around the call (wall clock /
  `span()` duration), not from the model.
- **Cost:** derived from a static per-model price map in `gate_config.yaml`
  (`$/1K prompt`, `$/1K completion`); recorded per case and summed for the run.

*Scope of the cost gate.* Cost is measured on **served-model (product) calls
only** — it deliberately **excludes judge-call cost**, because the gate guards the
product's cost profile, not the eval harness's overhead (which N-sample averaging
would otherwise dominate). Per case, cost is the **mean over the N samples**
(matching how scores are meaned). Judge tokens are recorded for visibility but are
not part of the budget number.

*The budget is a per-case figure, not an absolute run total.* The **primary cost
gate is `mean_cost_per_case`** = (sum of per-case served-model costs) / (number of
cases). This is stable as the golden set grows — which is an explicit goal of this
feature. A naive "total cost for the run" budget would rise mechanically every time
a case is added, so adding good cases would trip the cost gate for a reason
unrelated to any cost regression, and the budget would need re-tuning on every set
expansion. Gating on the **mean per case** decouples the cost signal from suite
size. The absolute run total is still **recorded and reported** for visibility.
**This pass gates cost on the absolute `mean_cost_per_case` budget in
`gate_config.yaml` only** (the coarse guardrail). A per-case **baseline-diff cost
delta** (live mean-per-case vs `baseline.json`, keyed like the score diff) is the
natural finer-grained regression signal but is **deferred — not implemented this
pass** (see Open questions); per-case `cost_usd` is already recorded in
`baseline.json`, so adding the delta later needs no baseline reshape. The
injected-over-budget test (inflated price-map entry) breaches `mean_cost_per_case`
and is unaffected by suite size.

Budget gate fails if **`mean_cost_per_case`** exceeds the configured budget
(stable signal, primary gate) or if **`p95 latency`** exceeds its budget
(secondary, advisory — see open question on small-N p95 noise). Latency is
measured against the served-model call only (mean over the N samples per case),
with the live gateway up; it is not gated when the suite runs without the stack.

**5. Per-slice handling, honest about N.** With realistic per-slice N (single
digits), a formal significance test has no power. Use a **two-part rule** instead:
(a) a per-slice **hard floor** (any slice mean below floor fails), and (b) a
**baseline-diff delta** per slice (slice mean dropping > `regression_delta` vs
baseline fails). Document this explicitly as the pragmatic stand-in for
significance; revisit a bootstrap CI only once a slice reaches N≥20.

**Slices (initial enumeration):** `retrieval`, `reasoning`, `observability`,
`safety/refusal`, `out-of-scope` (should-decline). Refine during curation.

**High-value slices** (the ones with a hard floor + the `≥4 cases` requirement)
are a property of the *slice*, not of per-case `weight`: for this pass they are
`retrieval`, `reasoning`, and `safety/refusal`, named explicitly in
`gate_config.yaml` under `slices.high_value`. (`weight` is a per-case business
multiplier feeding the weighted `overall`; it does not define which slices are
gated.)

**Scoring validity for decline slices.** The current scorer
(`0.5·keyword + 0.5·judge`) assumes a factual answer to match against a
`reference`. For `safety/refusal` and `out-of-scope` cases the desired behaviour
is a *decline*, where keyword overlap with a factual reference is meaningless. For
these slices the case carries `expect: "decline"` and is scored by a
decline-oriented judge prompt (1.0 if the answer appropriately refuses / says it
cannot answer from context, 0.0 if it fabricates an answer); `keywords` are
optional and the keyword half is skipped. Gating a decline slice with the factual
scorer would measure the wrong thing — this routing is required, not optional.

## Acceptance criteria

- [ ] Golden set grown to **≥ 20 total cases** with **≥ 4 cases in each
      high-value slice** (`retrieval`, `reasoning`, `safety/refusal`, named in
      `gate_config.yaml: slices.high_value`); every row has `id` (unique, stable),
      `slice`, `weight`. Schema validated at load time (bad row / unknown slice /
      duplicate id / `weight` outside 1..5 / `expect` not in {answer,decline} /
      **high-value slice below `slices.min_cases`** → non-zero exit). The
      min-cases check is what makes the "≥4 per high-value slice" requirement
      self-enforcing rather than review-only.
- [ ] Decline slices (`safety/refusal`, `out-of-scope`) carry `expect:"decline"`
      and are scored by the decline-oriented judge, not factual keyword overlap.
- [ ] Cases sourced from real traces are **redacted** per the redaction checklist
      below (no secrets, PII, API keys, internal hostnames); reviewer confirms in
      the PR description.
- [ ] `evals/gate_config.yaml` holds per-slice floors, `slices.high_value`,
      `slices.min_cases`, weights policy, regression delta, N-samples, the price
      map, and `mean_cost_per_case` / p95-latency budgets; the `THRESHOLD`
      constant is removed from `app/evals.py`.
- [ ] The gateway seam exposes per-call usage opt-in (`return_usage` /
      `chat_with_usage()`) with the default string-returning signature unchanged;
      no hot-path caller is modified.
- [ ] `evals/baseline.json` populated by `make eval-baseline` (pinned model+judge,
      `temperature=0`, N≥3), committed, and contains per-case/per-slice/overall
      scores + the pins + timestamp. Baseline-diff regression gate is
      **active (not skipped)**, and applies the added/removed-`id` lifecycle rule.
- [ ] A deliberately injected quality regression (lower a known-good answer) makes
      `make eval` / `pytest` exit non-zero via the baseline-diff gate.
- [ ] A deliberately over-budget change (inflated price-map entry →
      `mean_cost_per_case` breach) makes the **cost budget gate** fail.
- [ ] Deleting cases from a high-value slice until it falls below
      `slices.min_cases` makes the run exit non-zero at load-time validation
      (high-value slices cannot be silently emptied to route a regression around
      their per-slice floor).
- [ ] Per-slice floor breach in any single high-value slice fails the gate even
      when the weighted overall passes.
- [ ] `tests/test_evals.py` skips **only** on gateway-unreachability (connection
      probe / `APIConnectionError`); a forced scoring error, schema-validation
      failure, or any gate breach with the stack up produces a non-zero exit, not a
      skip (verifies the merge gate cannot be silently disabled).
- [ ] The cost budget gate is **`mean_cost_per_case`** = (sum of served-model
      per-case costs, each meaned over N samples) / (number of cases), excluding
      judge-call cost; judge tokens are recorded but unbudgeted. The absolute run
      total is reported but **not** the gated number (so growing the set does not
      trip the cost gate).

## Redaction checklist (trace-sourced cases)

Applied by the author and re-checked by the reviewer before any trace-derived case
is committed. Reviewer ticks each item in the PR description:

- [ ] No API keys, bearer tokens, secrets, or connection strings.
- [ ] No PII (names, emails, phone numbers, addresses, account/customer IDs).
- [ ] No internal hostnames, IPs, or non-public URLs/paths.
- [ ] No proprietary or customer-confidential document content; paraphrase to the
      minimum needed to exercise the slice.
- [ ] `question`/`reference` reflect the intended behaviour, not a verbatim dump.

**Accepted risk:** redaction is manual and fallible this pass; mitigated by review,
not automation (an automated scrubber is deferred with the trace-mining pipeline).

## Dependencies

- **PR #1** — provides the gate contract this feature parameterises: weighted
  `overall`, per-slice floors, baseline-diff, N-samples, pinned judge,
  served-model pins, config loading. **Blocking:** the golden-case schema
  (`slice`/`weight`) and `gate_config.yaml` shape must be agreed with PR #1; if
  PR #1 has not landed that machinery, this work cannot start.
- **PR #3** — provides the secret-gated CI `eval-gate` (stands up Postgres + the
  gateway) so "regression gate active" is enforced on PRs. No `.github/workflows`
  exists in-tree yet; the gate runs locally via `make eval` / `pytest` until #3
  lands.

## Open questions

- Where do production traces come from before spec 14 (observability backend)
  exists? Assumed: manual copy from local/dev runs this pass. **Accepted risk:**
  trace volume may be thin; supplement with hand-authored cases per slice.
- Exact per-slice floors and the cost/latency budget numbers — set provisionally
  from the first baseline, then tuned. Recorded as config, reviewable.
- **Per-case cost baseline-diff is deferred this pass.** Cost is gated only on the
  absolute `mean_cost_per_case` budget; a live-vs-baseline mean-per-case *delta*
  (analogous to the score baseline-diff) is a follow-up. `baseline.json` already
  records per-case `cost_usd`, so it can be added without reshaping the baseline.
  **Accepted risk:** a gradual cost creep that stays under the absolute budget is
  not caught this pass.
- Does PR #1 expose token usage to the eval layer, or must this feature add an
  eval-only capture wrapper? Resolve at PR #1 integration.
- Is N=3 enough to damp judge variance, or is N=5 needed? Decide from observed
  per-case score spread in the first baseline.
- **p95 latency on ~20 cases is statistically the near-max and noisy.** Kept as an
  *advisory* secondary gate (`mean_cost_per_case` is the primary budget gate); revisit
  promoting it to blocking once the suite is larger or latency is averaged over
  the N samples. **Accepted risk** for this pass.
- Exact gateway usage-seam signature (`return_usage` flag vs `chat_with_usage()`)
  is co-owned with PR #1; resolve at integration so the eval path and any future
  PR #1 cost reporting share one helper.
- **Eval-run wall-clock and cost in CI.** A full gate run is ≈ `cases × N`
  served-model calls **plus** one judge call per sample — at the target ≥20 cases
  and N=3 that is ~60 served + ~60 judge ≈ **120 sequential model calls per
  `make eval` / `pytest`**, on every PR once PR #3 enforces it. Sequentially this
  is minutes and real spend per run. **Decision for this pass:** (a) make N
  config-driven and allow PR-CI to run the *floor + baseline-diff* gates at a lower
  N (e.g. N=1) while **baseline regeneration** uses N≥3 to damp judge variance;
  (b) set an explicit job timeout in the PR #3 workflow; (c) consider bounded
  concurrency for the per-case loop as a follow-up. **Accepted risk:** until then,
  the suite runs sequentially and CI time/cost scales linearly with the set.
- **`out-of-scope` is a decline slice but is *not* high-value**, so it has no
  per-slice hard floor and no `min_cases` guard; an out-of-scope regression (model
  answers instead of declining) is caught only by the weighted-overall floor and
  the per-case baseline-diff, not a dedicated slice floor. **Accepted risk** for
  this pass; promote it into `slices.high_value` once it reaches `min_cases`.

## Risks & mitigations

- **Small-N statistics are meaningless** → use hard floors + baseline-diff deltas,
  not significance tests, until N≥20 per slice (documented above).
- **The per-slice floor is itself noisy at N=4** → on a 4-case slice a single
  flaky case swings the slice mean by ~0.25, so the hard floor can false-trip (or
  mask). N-sample averaging (N≥3) damps *judge* non-determinism per case but not
  *case-selection* variance. Mitigation: set per-slice floors conservatively from
  the first baseline with headroom, and prefer the baseline-diff delta as the
  primary slice signal; grow each high-value slice toward N≥20 to retire this.
  **Accepted risk for this pass.**
- **Baseline drift / flaky gate** from LLM non-determinism → pin model+judge,
  `temperature=0`, N-sample averaging, regression *delta* (not exact match);
  baseline only regenerated by deliberate reviewed PR, never by CI.
- **PII/secret leakage** from trace-sourced cases into a checked-in file →
  redaction checklist (strip keys/tokens, emails, names, internal hostnames,
  customer data) + mandatory reviewer sign-off in the PR. **Accepted risk:** human
  redaction is fallible; mitigated by review, not automation, this pass.
- **Cost gate brittleness** from a static price map going stale → price map lives
  in `gate_config.yaml`, versioned and reviewed; budget is a coarse guardrail, not
  billing-accurate.
- **Dependency slip** (PR #1/#3 not merged) → schema and config shapes are agreed
  up front so curation can proceed in parallel; gate activation waits on #1/#3.

## Test & rollout plan

- **Verification:** the acceptance-criteria checks above are the tests —
  injected-regression case, injected over-budget case, and per-slice floor breach
  each prove a distinct gate fires. Run via `make eval` and `uv run pytest -q`
  (the existing `tests/test_evals.py` merge gate). **Note:** these gates call the
  live served model + judge through the gateway, so they require the stack up
  (`make up`); without it `tests/test_evals.py` must skip rather than hard-fail
  (no gateway ≠ regression). The secret-gated `eval-gate` (PR #3) is what enforces
  them on PRs.
- **Precise skip predicate (required).** "Skip when the stack is down" must be
  scoped to **gateway unreachability only** — a cheap pre-flight reachability
  probe (or catching *only* the gateway client's connection/timeout error class,
  e.g. `openai.APIConnectionError`) decides the skip. Schema-validation errors,
  scoring errors, assertion failures, and any gate breach **must never be
  swallowed into a skip** — a broad `except: pytest.skip(...)` would silently
  disable the merge gate, which is the exact failure this whole feature exists to
  prevent. In CI under PR #3 (where the stack is always stood up) the skip path is
  unreachable; a skip there is itself a failure signal.
- **Rollout:** data + config only — no migration, no runtime/flag change. Land in
  one PR: schema + grown golden set + `gate_config.yaml` + `evals/baseline.json` +
  `make eval-baseline` target. Gate becomes enforcing in CI once PR #3 is in and
  branch protection requires `eval-gate`.

## References

- [Feature roadmap](../../ROADMAP.md)
- [Specs index](../README.md)
- This feature: [`design.md`](design.md) · [`examples/`](examples/) · [`testing.md`](testing.md)
- Current gate: `app/evals.py`, `tests/test_evals.py`, `evals/golden.jsonl`
- Seams reused: `app/gateway.py` (`chat(**kwargs)` already forwards to `create`),
  `app/agent.py` (`ask` / `generate_node`), `gateway/litellm_config.yaml`
  (alias→provider resolution the price map keys off)
