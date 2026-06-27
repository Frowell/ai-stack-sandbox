---
title: CI hardening
slug: ci-hardening
area: ci
tier: Next
size: S
status: Todo
depends_on: [PR #2, PR #3]
issue:        # set to the GitHub issue number when created
---

# CI hardening

> **Area** `ci` · **Tier** `Next` · **Size** `S` · **Status** `Todo` · **Depends on:** PR #2, PR #3

> **Companion files:** [`design.md`](./design.md) (required-check footgun, job
> graph, truth table, digest-pinning procedure, alternatives) ·
> [`examples/`](./examples/) (illustrative hardened `ci.yml`, summary-job
> snippet, Dependabot, branch-protection script, lock-drift negative test) ·
> [`testing.md`](./testing.md) (per-criterion proof matrix + how it gates merge).
>
> **Repo status (2026-06-27):** PRs #1–#3 are now **merged on `main`** (`a9c5e6e`,
> `2534ad9`, `31fbafa`); the canonical workflow is `main:.github/workflows/ci.yml`
> and the three feature branches' copies are byte-identical to it. The hard
> blockers in *Dependencies* are therefore satisfied — this spec is ready to
> implement, and the single-source reconciliation (step 5) is already a no-op.

## Summary

PR #3 lands a working two-tier CI (`lint` always; secret-gated `eval-gate` that
stands up Postgres + the gateway and runs the eval merge gate). That workflow is
permissive on purpose so the open PRs can land. This spec hardens it once the
open PRs merge: make the lock authoritative (CI fails on lock drift), make the
gates *required* on `main` without breaking forks, pin the supply chain
(third-party actions + the gateway image), and collapse the per-branch copies of
`ci.yml` into a single source of truth.

## Problem / Motivation

The workflow introduced by PR #3 (`.github/workflows/ci.yml`) is deliberately
loose so that PRs #1–#3 can merge despite past lock drift:

- `lint` and `eval-gate` both run `uv sync` (non-frozen) — a stale/incorrect
  `uv.lock` is silently re-resolved instead of failing the build.
- Neither gate is enforced via branch protection, so red CI does not block merge.
- Three open PRs (#1, #2, #3) each carry their own divergent view of the
  workflow; whichever merges last must reconcile by hand.
- `setup-uv`, `actions/checkout`, and the `litellm:main-stable` image float on
  mutable tags — no supply-chain pinning for a workflow that holds the
  merge-gate keys and a real provider secret.

## Goals

- **Lock is authoritative.** CI fails when `uv.lock` is out of date w.r.t.
  `pyproject.toml`. Use `uv sync --locked` (and/or a dedicated `uv lock --check`
  step), **not** `--frozen` — `--frozen` installs from the existing lock without
  checking freshness and would *not* fail a stale-lock PR (correctness note
  below).
- **Gates required on `main`, forks not broken.** `lint` is a required check.
  The eval gate is enforced through an **always-running summary job** (see design)
  so that a *required* check still reports green when `eval-gate` is
  intentionally skipped (no secret / fork PR), instead of blocking merge forever.
- **Deterministic, cached deps.** Keep `setup-uv` caching, key the cache on
  `uv.lock`, and pin all third-party actions to commit SHAs and the gateway
  image to a digest.
- **Single source of truth.** After PRs #1–#3 merge, reconcile to one canonical
  `ci.yml` on `main`; document that feature branches inherit it rather than
  carrying private copies.

## Non-goals

- Multi-OS / multi-Python matrix; release automation; deploy pipelines.
- Self-hosted runners; caching the Docker layer for the gateway image.
- Changing what the eval gate *asserts* (that is PR #1 / spec 06's scope).

## Proposed design

This lives **entirely** behind the **CI seam** (`.github/workflows/ci.yml`); no
app, gateway, db, or eval *runtime* code changes, and **no `docker-compose.yml`
change**. This was verified against the actual workflow on all three open
branches (`ci/github-actions`, `feat/hybrid-ingestion`, `feat/eval-release-gate`),
which are currently **byte-identical**: the eval gate does **not** use
`docker compose`. It stands up the two images directly inside `ci.yml`:

- **Postgres/pgvector** via a GitHub Actions `services:` block
  (`image: pgvector/pgvector:pg16`).
- **LiteLLM** via an inline `docker run … ghcr.io/berriai/litellm:main-stable …`
  step (the gate uses `docker run`, not `services:`, precisely because `litellm`
  needs its mounted `--config` file, which `services:` containers cannot provide).

Therefore **both image digests are pinned in `ci.yml` itself** — the
`services.postgres.image` field and the `docker run` image ref. `docker-compose.yml`
is local-dev only and is **out of scope** here (pinning its floating tags for local
determinism is a separate, optional follow-up, not required by this spec). Sequenced
after PRs #1–#3 are on `main`.

1. **Authoritative lock.** Replace `uv sync` with `uv sync --locked` in both
   jobs. Optionally add an explicit first step `uv lock --check` in `lint` for a
   clear, fast failure message distinct from the install step. (`uv 0.11.24`:
   `--locked` = "assert the lock will remain unchanged"; `--frozen` = "sync
   without updating the lock file" — only the former fails on drift.)

2. **Required-gate pattern that survives skips.** Add a final job that always
   runs and is the *only* eval-related required check:

   ```yaml
   eval-gate-result:
     needs: [gate-check, eval-gate]
     if: always()
     runs-on: ubuntu-latest
     env:
       # Pass job outputs via env; never interpolate ${{ }} into the shell body.
       GATE_CHECK: ${{ needs.gate-check.result }}
       RESULT:     ${{ needs.eval-gate.result }}
       ENABLED:    ${{ needs.gate-check.outputs.enabled }}
     steps:
       - run: |
           set -euo pipefail
           # FAIL CLOSED: if gate-check itself did not succeed we cannot trust
           # `enabled`, so block rather than silently pass the required check.
           if [ "$GATE_CHECK" != "success" ]; then
             echo "gate-check did not succeed ($GATE_CHECK) — failing closed"; exit 1
           fi
           # FAIL CLOSED on a malformed signal too: `enabled` must be exactly
           # "true" or "false". An empty/garbage value (e.g. a future edit to
           # gate-check that forgets to emit on some path) must NOT be treated as
           # "disabled" — that would silently green-light a skipped gate.
           if [ "$ENABLED" != "true" ] && [ "$ENABLED" != "false" ]; then
             echo "gate-check emitted unexpected enabled=$ENABLED — failing closed"; exit 1
           fi
           # Pass when the gate ran green OR was intentionally skipped because it
           # is disabled (no secret / fork PR). Fail on real failure/cancel.
           if [ "$RESULT" = "success" ] || { [ "$ENABLED" = "false" ] && [ "$RESULT" = "skipped" ]; }; then
             echo "eval gate ok (result=$RESULT, enabled=$ENABLED)"; exit 0
           fi
           echo "eval gate failed: result=$RESULT enabled=$ENABLED"; exit 1
   ```

   Branch protection then requires `lint` and `eval-gate-result` (stable job
   names) — never the conditionally-skipped `eval-gate` directly. This avoids the
   GitHub footgun where a *required* check that is skipped via job-level `if:`
   blocks the PR indefinitely, and lets fork PRs (which never receive the secret)
   still merge. The result job **fails closed** when `gate-check` errors, so a
   broken secret-detection job can never silently disable the gate.

   > **`needs` interaction with `lint`.** In the actual PR #3 workflow,
   > `eval-gate` declares `needs: [lint, gate-check]`, so a failing `lint` also
   > *skips* `eval-gate`. `eval-gate-result` deliberately does **not** list `lint`
   > in its `needs` — `lint` is its own required check, so a lint failure already
   > blocks the PR. The fail-closed logic still behaves correctly here: a lint
   > failure with `enabled=true` yields `result=skipped`, which is **not** the
   > "disabled" skip, so `eval-gate-result` goes red (harmless double-block). The
   > only cosmetic wart: the red message says "eval gate failed" when the true
   > cause was lint — acceptable; the `lint` check shows the real reason.

   > **Job-name coupling — verify against the merged PR #3.** This pattern
   > hardcodes the `gate-check`, `eval-gate`, `eval-gate-result` job names and the
   > `gate-check.outputs.enabled` output. They must match exactly what PR #3
   > actually lands. If PR #3 names the secret-detection job/output differently,
   > reconcile the names here **before** marking any check required — a required
   > check whose name doesn't match an emitted check stays "expected/pending" and
   > wedges every PR.

3. **Supply-chain pinning.** Pin `actions/checkout` (currently `@v4`),
   `astral-sh/setup-uv` (currently `@v5`), and the `pgvector` service / `litellm`
   image to immutable refs (action commit SHAs with a version comment; the images
   to a digest, **both in `ci.yml`** per the scope note above — `pgvector` in the
   `services.postgres.image` field, `litellm` in the inline `docker run` ref). For
   `litellm`, **resolve the digest from a tagged
   release** (e.g. `v1.x.y`), not from the floating `main-stable` tag — pinning a
   `main`-built digest freezes a possibly-unreleased dev build. Keep
   `permissions: contents: read` (already minimal).

   > **Validate the litellm pin before landing it.** The workflow is built and
   > observed-green against `main-stable`. A tagged release may differ in config
   > schema or health endpoint (`/health/liveliness`). Before pinning, run one CI
   > cycle on the candidate digest and confirm (a) the gateway becomes healthy and
   > (b) `eval-gate` passes; record the exact `tag → sha256` mapping in the PR so
   > the pin is reproducible. Do **not** bump the litellm major/minor line as part
   > of this spec beyond what is needed to obtain a non-`main` digest.

   **Dependabot is a required deliverable, not just a mitigation.** SHA/digest
   pinning disables the auto-security-updates that mutable tags provide, so this
   spec must also land a `.github/dependabot.yml` enabling the `github-actions`
   ecosystem (weekly) so the pins receive maintained bump PRs. (Docker-image
   digest bumps for `pgvector`/`litellm` are tracked manually or via a separate
   `docker` Dependabot entry — optional follow-up.)

4. **Caching.** Keep `setup-uv` `enable-cache: true`; set
   `cache-dependency-glob: "uv.lock"` so the cache key tracks the lock.

5. **Single-source reconciliation.** The three open branches currently carry a
   **byte-identical** `ci.yml`, so today reconciliation is a no-op — but they can
   diverge before they merge, and Git will not auto-merge a same-path add three
   ways. After PRs #1–#3 merge, ensure exactly one `ci.yml` on `main`; delete any
   branch-local variants. Re-diff the three copies just before the last merge so a
   late edit to one branch isn't silently lost; document in the PR description
   which workflow version is canonical.

## Acceptance criteria

- [ ] Both jobs use `uv sync --locked` (and/or `uv lock --check`); a PR that
      edits `pyproject.toml` without re-locking **fails** CI with a lock-drift
      error. (Explicitly verify this fails — a green run with `--frozen` would be
      a false pass.)
- [ ] `lint` and `eval-gate-result` are required checks on `main`; `eval-gate`
      itself is **not** directly required.
- [ ] A PR **from a fork** (no `OPENAI_API_KEY`) can still merge: `eval-gate`
      skips, `eval-gate-result` reports green.
- [ ] When the secret *is* present and the eval suite genuinely fails,
      `eval-gate-result` is red and merge is blocked.
- [ ] **`eval-gate-result` fails closed when `gate-check` errors.** Simulate a
      failing/cancelled `gate-check` (e.g. force a non-zero exit) → `eval-gate`
      skips, `eval-gate-result` is **red** (not a silent green). This proves a
      broken secret-detection job cannot disable the required gate.
- [ ] **`eval-gate-result` fails closed on a malformed signal.** When
      `gate-check` succeeds but emits `enabled` that is neither `true` nor `false`
      (empty/garbage), `eval-gate-result` is **red**, not a silent green. This
      closes the symmetric output-value footgun to the job-name coupling below.
- [ ] The `eval-gate-result` script reads job results from `env:` (no `${{ }}`
      interpolated into the shell body) and runs under `set -euo pipefail`.
- [ ] Required-check names (`lint`, `eval-gate-result`) **exactly match** the job
      names emitted by the merged PR #3 workflow; verified before any check is
      marked required (a name mismatch leaves the check "pending" and wedges PRs).
- [ ] All third-party actions are pinned to commit SHAs (with version comment) and
      the `litellm` + `pgvector` images to digests resolved from tagged releases;
      `permissions` stays read-only. **Both image pins live in `ci.yml`** (`pgvector`
      in `services.postgres.image`, `litellm` in the inline `docker run` ref);
      `docker-compose.yml` is not modified by this spec.
- [ ] The `litellm` pin is **validated before landing**: one CI cycle on the
      candidate digest shows the gateway healthy and `eval-gate` green, and the
      `tag → sha256` mapping is recorded in the PR description.
- [ ] A `.github/dependabot.yml` enabling the `github-actions` ecosystem is
      present, so SHA-pinned actions still receive maintained bump PRs.
- [ ] Exactly one `ci.yml` exists on `main` after #1–#3 merge; no per-branch
      drift.
- [ ] Dep cache is keyed on `uv.lock` (cache invalidates when the lock changes).
- [ ] `uv sync --locked` still installs the `dev` group (ruff for `lint`, pytest
      for `eval-gate`) — i.e. the gate's tools are present after a locked sync.

## Dependencies

- **PR #3** — introduces `.github/workflows/ci.yml`; this spec edits it. Hard
  blocker: nothing here can start before #3 is on `main`.
- **PR #2** — hybrid ingestion. **Confirmed (diffed `main...feat/hybrid-ingestion`):**
  it modifies `.github/workflows/ci.yml` (its own copy), `app/ingest.py`, and
  `db/init.sql` — all of which the `eval-gate` job exercises (schema-create +
  `python -m app.ingest` steps), and it adds deps to `pyproject.toml`/`uv.lock`.
  The dependency therefore **stands**: reconciling to a single `ci.yml` and
  re-locking must happen *after* #2 lands so the canonical workflow matches the
  merged ingestion path and the lock includes #2's new packages.

## Open questions

- ~~**Why exactly is PR #2 a dependency?**~~ **Resolved** (see Dependencies): the
  diff confirms #2 touches `ci.yml`, `app/ingest.py`, `db/init.sql`, and the lock,
  so the dependency stands. (Verified `main...feat/hybrid-ingestion`.)
- **`--locked` vs `--frozen`.** Confirmed for `uv 0.11.24` (the version in this
  sandbox): `--frozen` does *not* fail on stale lock. Spec standardizes on
  `--locked`. The current lock is consistent — `uv lock --check` exits 0 on the
  pre-merge tree — so flipping to `--locked` will not retroactively red the first
  PR. (After #1/#2 merge their new deps, re-verify the merged lock the same way
  before marking checks required.)
- ~~**Should `eval-gate` run on PRs at all, or only on push to `main`?**~~
  **Decided: keep on PRs** (stronger — catches regressions before merge). Rationale:
  (a) `concurrency: cancel-in-progress` already collapses redundant runs per PR,
  (b) the gate averages `samples_per_case: 3` (see `evals/gate.json`), damping
  judge variance, and (c) the break-glass below covers a provider outage. Per-PR
  provider spend is the accepted cost (see Risks). Revisit only if spend or
  flake-rate becomes a problem — switching to `main`/merge-queue-only is a
  one-line `on:`/trigger change and is the documented escape hatch.

## Risks & mitigations

- **Required real-LLM gate couples merge to provider uptime/rate limits/cost.**
  An OpenAI outage or 429 storm could block *all* merges. *Mitigation:* the gate
  already runs behind `concurrency` cancel-in-progress; document a break-glass
  (admin merge / temporarily unmark the required check) for provider outages.
  *Accepted risk:* per-PR provider spend.
  - **Retry must be scoped to transient errors only.** Do **not** blanket-retry
    the whole pytest step: the eval gate is non-deterministic (see next risk), so
    a blind retry would re-roll a genuine quality regression into a lucky pass and
    mask it. If retry is added, gate it on detectable transient failures
    (HTTP 429 / 5xx / connection error from the gateway) and never on an
    assertion failure from `test_quality_gate`.
- **The eval gate is non-deterministic, and this spec makes it merge-blocking.**
  `app/evals.py` blends a keyword score with an **LLM-as-judge** call
  (`judge_score` → `gateway.chat`) against the thresholds in `evals/gate.json`
  (`overall: 0.70`, `weighted: 0.75`; `judge_weight: 0.5`). Note the calls
  **already run at `temperature=0`** — `app/gateway.chat` sets
  `kwargs.setdefault("temperature", settings.temperature)` and
  `settings.temperature` defaults to `0` — but greedy decoding is *not*
  deterministic: there is **no seed**, `drop_params` can strip `temperature`
  invisibly, and floating-point non-associativity + provider-side batching remain
  (see the determinism note in `app/evals.py`'s own docstring). The gate averages
  `samples_per_case: 3`, which damps but does not remove this variance —
  near-threshold cases will still flip between runs, so promoting the gate to
  *required* converts residual judge variance into flaky merge-blocking failures.
  *Mitigation (cross-spec):* adding a judge **seed** (best-effort on OpenAI) and/or
  widening the pass margin is **spec 06 / PR #1's scope** (a non-goal here — this
  spec must not change what the gate asserts; `temperature=0` is already in place). For
  this spec: (1) the break-glass above covers a flaky red, and (2) the
  "gate on PRs vs. only on `main`" open question is the lever — gating only on
  `main`/merge-queue contains flake blast-radius to post-merge. *Accepted risk
  until spec 06 lands determinism:* occasional re-runs of `eval-gate` on
  near-threshold PRs.
- **Pinning to SHAs adds maintenance.** Mutable tags auto-receive security
  fixes; SHAs do not. *Mitigation:* enable Dependabot for `github-actions` to PR
  pin bumps.
- **`--locked` can block unrelated PRs if `main`'s lock drifts.** *Mitigation:*
  the same gate runs on `main` push, so drift is caught at source; re-lock is a
  one-line `uv lock` fix.
- **Concurrency cancel can leave a `cancelled` required check on a superseded run.**
  When a new push cancels an in-progress run, the older run's `eval-gate-result`
  may report `cancelled`. *Accepted:* GitHub gates mergeability on the latest run
  for the head SHA, so the superseding run's fresh status governs; a stale
  `cancelled` status does not wedge the PR. No mitigation needed beyond awareness.

## Test & rollout plan

1. **Verify drift detection (negative test):** open a throwaway PR that bumps a
   dep in `pyproject.toml` without re-locking → confirm CI fails on the lock
   step (not silently passes).
2. **Verify fork path:** open/simulate a fork PR (no secret) → `eval-gate` skips,
   `eval-gate-result` green, mergeable.
3. **Verify failure path:** with the secret set, introduce a deliberate eval
   regression → `eval-gate-result` red, merge blocked.
4. **Roll out branch protection last,** after the workflow changes are on `main`
   and observed green for one cycle, so a misconfigured required check never
   wedges the repo. No app/runtime migration; reversible by unmarking the
   required checks.

## References

- [Design notes](./design.md) · [Examples (illustrative)](./examples/) · [Testing plan](./testing.md)
- [Feature roadmap](../../ROADMAP.md)
- [Specs index](../README.md)
- [Spec 06 — Eval-set maturity](../06-eval-set-maturity/README.md) (owns judge
  determinism / what the gate *asserts*; this spec only makes the gate required)
- PR #3 workflow: `.github/workflows/ci.yml` (branch `ci/github-actions`)
- [uv `sync` flags](https://docs.astral.sh/uv/reference/cli/#uv-sync) (`--locked` vs `--frozen`)
