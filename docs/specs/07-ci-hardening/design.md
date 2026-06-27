# CI hardening — design notes

Deeper notes behind [`README.md`](./README.md): the required-check footgun and
why the summary-job pattern is the only safe shape, alternatives considered,
the job graph + result-job truth table, the digest-pinning procedure, and edge
cases. See [`examples/`](./examples/) for the illustrative YAML and
[`testing.md`](./testing.md) for how each acceptance criterion is proven.

> **Grounding.** PRs #1–#3 are now merged on `main` (`31fbafa`, `2534ad9`,
> `a9c5e6e`); the canonical workflow is `main:.github/workflows/ci.yml` and the
> three feature branches' copies are byte-identical to it (verified with
> `git diff origin/main:.github/workflows/ci.yml <branch>:…` → empty). The job
> names this spec hardcodes (`lint`, `gate-check`, `eval-gate`) and the output
> (`gate-check.outputs.enabled`) are confirmed against that file.

## 1. The required-check footgun (the core problem)

GitHub branch protection requires checks **by name**. Two behaviours interact
badly:

1. A required check that is **never emitted** for a given PR stays
   `expected`/`pending` forever — the PR can never go green. It is *not* treated
   as "skipped → neutral".
2. A job that is skipped via a **job-level `if:`** reports a *skipped* conclusion
   for that job name.

The current `eval-gate` has `if: needs.gate-check.outputs.enabled == 'true'`. On
a fork PR (or any run without `OPENAI_API_KEY`) it is skipped. If we marked
`eval-gate` itself required, every fork PR would wedge: the required `eval-gate`
check reports skipped, which branch protection does **not** accept as success in
all configurations, and worse, a name/typo mismatch leaves it eternally pending.

**Resolution:** never make the conditional job required. Add a *summary* job
(`eval-gate-result`) that **always runs** (`if: always()`), reads the upstream
job conclusions, and collapses them into one deterministic pass/fail. Branch
protection requires `lint` + `eval-gate-result` only — both are emitted on every
run, so they always resolve.

## 2. Job graph

```
         ┌────────┐        ┌────────────┐
 PR ───► │  lint  │        │ gate-check │  (reads secret presence → enabled=t/f)
         └───┬────┘        └─────┬──────┘
             │  (required)       │  outputs.enabled
             │            ┌──────┴───────────────┐
             │            ▼                       │
             │      ┌───────────┐                 │
             └─────►│ eval-gate │  if: enabled    │  (NOT required directly)
                    └─────┬─────┘                 │
                          │ result                │
                          ▼                       ▼
                   ┌─────────────────────────────────┐
                   │  eval-gate-result   if: always() │  (required)
                   │  needs: [gate-check, eval-gate]  │
                   └─────────────────────────────────┘
```

Note `eval-gate-result` does **not** `need: lint`. `lint` is its own required
check, so a lint failure already blocks the PR; adding it to the summary's
`needs` would only muddy the failure message (see truth table row D).

## 3. `eval-gate-result` truth table

Inputs: `GATE_CHECK = needs.gate-check.result`, `ENABLED =
needs.gate-check.outputs.enabled`, `RESULT = needs.eval-gate.result`.
(`eval-gate` is skipped by GitHub when any `needs` dep fails/skips, **or** when
its own `if:` is false.)

| # | Scenario | GATE_CHECK | ENABLED | RESULT | Summary | Why |
|---|----------|-----------|---------|--------|---------|-----|
| A | Fork / no secret | success | false | skipped | **pass** | intentional skip — disabled gate |
| B | Secret set, evals pass | success | true | success | **pass** | gate ran green |
| C | Secret set, evals fail | success | true | failure | **fail** | real regression blocks merge |
| D | Secret set, `lint` failed | success | true | skipped | **fail** | `enabled=true` but skipped ≠ disabled-skip → fail closed (lint already red too) |
| E | gate-check errored | failure/cancelled | (untrusted) | skipped | **fail** | can't trust `enabled` → fail closed |
| F | eval-gate cancelled (concurrency) | success | true | cancelled | **fail** | not success, not disabled-skip |
| G | malformed signal | success | "" / garbage | skipped | **fail** | `enabled` neither `true` nor `false` → can't trust it → fail closed |

The decisive predicate (matches the README snippet and `examples/ci.hardened.yml`
verbatim — keep all three in sync):

```sh
if [ "$GATE_CHECK" != "success" ]; then exit 1; fi          # row E
if [ "$ENABLED" != "true" ] && [ "$ENABLED" != "false" ]; then exit 1; fi  # row G
if [ "$RESULT" = "success" ] \
   || { [ "$ENABLED" = "false" ] && [ "$RESULT" = "skipped" ]; }; then
  exit 0                                                     # rows A, B
fi
exit 1                                                       # rows C, D, F
```

Ordering matters: the `GATE_CHECK` guard runs **before** the malformed-`enabled`
guard, because on row E `enabled` is itself untrusted — we must fail closed on the
errored gate-check first, not branch on its (possibly garbage) output.

Row D is the one acceptable wart: the summary says "eval gate failed" when the
true cause was lint. Acceptable because `lint` (also red) shows the real reason,
and a double-block is safe (never a false pass).

## 4. `--locked` vs `--frozen` (why the README is emphatic)

Verified against the sandbox's `uv 0.11.24`:

- `uv sync --frozen` — "sync without updating the `uv.lock` file." Installs from
  whatever lock is on disk; **does not** check it against `pyproject.toml`. A PR
  that adds a dep to `pyproject.toml` but forgets to re-lock installs the *old*
  resolution and passes green → false pass.
- `uv sync --locked` — "assert that the lock file will remain unchanged." Re-runs
  resolution against `pyproject.toml`; if the lock would change, exits non-zero.
- `uv lock --check` — same assertion, no install; gives a fast, clearly-labelled
  failure before the (slower) sync step.

Decision: use `uv sync --locked` in **both** jobs, and add `uv lock --check` as
the first step of `lint` for a crisp error. Both keep `--dev` behaviour (uv
installs the `dev` group by default for a non-package project), so `ruff` (lint)
and `pytest` (eval-gate) remain present — see AC #11.

## 5. Digest-pinning procedure (don't pin a dev build)

Two third-party **actions** and two **container images** float today.

Actions — pin to a commit SHA with a version comment so Dependabot can bump:

```
uses: actions/checkout@<40-char-sha>    # v4.x.y
uses: astral-sh/setup-uv@<40-char-sha>  # v5.x.y
```

Resolve a SHA from a tag (illustrative):

```sh
gh api repos/actions/checkout/git/refs/tags/v4.2.2 --jq .object.sha
```

Images — pin to a digest. For `litellm`, **resolve the digest from a tagged
release**, not from the floating `main-stable` tag (which can be an unreleased
dev build):

```sh
# pgvector (services.postgres.image)
docker buildx imagetools inspect pgvector/pgvector:pg16 --format '{{.Manifest.Digest}}'
# litellm — pick a release tag, NOT main-stable
docker buildx imagetools inspect ghcr.io/berriai/litellm:v1.74.0 --format '{{.Manifest.Digest}}'
```

Then in `ci.yml`: `pgvector/pgvector:pg16@sha256:…` in `services.postgres.image`
and `ghcr.io/berriai/litellm:v1.74.0@sha256:…` in the inline `docker run` ref.
`docker-compose.yml` is **out of scope** (local-dev only).

## 6. Alternatives considered

| Alternative | Why rejected |
|-------------|--------------|
| Mark `eval-gate` itself required | The footgun in §1 — skipped/typoed required check wedges every fork PR. |
| Make `eval-gate` always run; skip *inside* via step `if:` and exit 0 | Hides intent (job shows green even when it did nothing); harder to read than an explicit summary job; still needs a fail-closed check on the secret-detection step. |
| Use `--frozen` for speed | Does not fail on lock drift (§4) — defeats the primary goal. |
| Drive the stack with `docker compose` in CI and pin the compose file | The real `eval-gate` uses `services:` + inline `docker run` (litellm needs a mounted `--config`, which `services:` can't provide). Compose is local-dev only; pinning it is a separate optional follow-up. |
| Gate only on `main` / merge-queue | Cheaper and contains flake blast-radius, but weakens PR feedback. Left as an open question; default keeps PR gating with concurrency-cancel. |
| Pin `litellm:main-stable` digest | Freezes a possibly-unreleased dev build; pin a release tag's digest instead (§5). |

## 7. Edge cases & sequencing

- **Concurrency cancel** (`cancel-in-progress: true`, already present) yields
  `RESULT=cancelled` → summary fails (row F). Correct: a cancelled run is not a
  proven pass. A force-push supersedes it with a fresh run.
- **`gate-check` typo / rename in a future PR** breaks the hardcoded `needs`
  names. Mitigation: AC #6 requires re-verifying names against the merged
  workflow before marking checks required; document names in the PR body.
- **`main` lock drift** would red unrelated PRs under `--locked`. Mitigation:
  the same gate runs on `push: main`, so drift is caught at source; fix is a
  one-line `uv lock`.
- **Rollout ordering:** land the `ci.yml` edits, observe one green cycle on
  `main`, *then* flip branch protection — so a misnamed required check never
  wedges the repo. Reversible by unmarking the checks.
- **Single-source reconciliation is now a no-op on `main`** (all three PRs
  merged; one `ci.yml`). The spec's reconciliation step remains documented for
  the historical record and in case a long-lived branch reintroduces a copy.
