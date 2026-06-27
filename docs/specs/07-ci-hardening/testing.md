# Testing & verification — CI hardening

How each acceptance criterion in [`README.md`](./README.md) is proven, and how
the change gates merge. This feature *is* the gate, so most "tests" are
workflow-level verifications (negative CI runs, simulated job conclusions, and
branch-protection state) rather than `pytest` cases. The repo's existing
`tests/test_evals.py` is what the gate *runs*; this spec does not change it.

> **What is and isn't unit-testable.** The hardening lives entirely in
> `.github/workflows/ci.yml` (+ `.github/dependabot.yml` and branch-protection
> settings). There is no app/runtime code to unit-test. The two pieces with real
> logic — the `eval-gate-result` shell predicate and lock-drift detection — are
> verified by (a) a local table-driven harness for the predicate and (b) a live
> negative CI run for drift. Everything else is asserted by inspection +
> `actionlint` + observing real runs.

## Test layers

| Layer | Tooling | What it covers |
|-------|---------|----------------|
| Static | `actionlint`, `yamllint` | workflow is well-formed; job/`needs` names resolve |
| Predicate unit | local `bash` table-driven harness (below) | `eval-gate-result` truth table rows A–G |
| Negative CI | throwaway PR ([`examples/negative-test-lock-drift.md`](./examples/negative-test-lock-drift.md)) | lock drift fails red |
| Live CI | real PR runs (fork, secret-set pass, secret-set fail) | end-to-end gate behaviour |
| Config state | `gh api .../branches/main/protection` | required checks = exactly `lint`, `eval-gate-result` |

## Acceptance-criteria → proof matrix

| # | Acceptance criterion | How it's proven | Gate |
|---|----------------------|-----------------|------|
| 1 | Both jobs use `uv sync --locked` / `uv lock --check`; un-relocked `pyproject.toml` fails CI | Negative CI run (examples/negative-test-lock-drift.md) → `lint` red at `uv lock --check`. Also `grep -c 'uv sync$' ci.yml == 0`. | `lint` (required) |
| 2 | `lint` + `eval-gate-result` required on `main`; `eval-gate` not directly required | `gh api repos/$R/branches/main/protection --jq '.required_status_checks.contexts'` → `["lint","eval-gate-result"]` exactly | branch protection |
| 3 | Fork PR (no secret) still merges: `eval-gate` skips, `eval-gate-result` green | Open a PR from a fork (or simulate row A locally) → `eval-gate` skipped, `eval-gate-result` ✅ | `eval-gate-result` |
| 4 | Secret present + eval genuinely fails → `eval-gate-result` red, merge blocked | Introduce a deliberate regression (below) on a branch with the secret → row C, `eval-gate-result` ❌ | `eval-gate-result` |
| 5 | `eval-gate-result` fails closed when `gate-check` errors | Force `gate-check` to exit non-zero (temp `exit 1`) → row E, summary ❌ | `eval-gate-result` |
| 6 | Script reads results from `env:` (no `${{ }}` in shell body) and uses `set -euo pipefail` | Inspect job: `env:` block present; body has no `${{`; first line `set -euo pipefail`. Predicate harness exercises rows A–G (incl. row G malformed-signal). | review + harness |
| 7 | Required names match emitted job names | Pre-flight in `examples/branch-protection.sh`: `gh api .../check-runs --jq .name` includes `lint` and `eval-gate-result` before the PUT | rollout step |
| 8 | Actions pinned to SHAs (w/ version comment); `litellm`+`pgvector` pinned to release-tag digests; `permissions` read-only; both image pins in `ci.yml` | `grep` for `@sha256:` on both images and 40-char SHAs on `uses:`; `permissions: contents: read` present; `git grep sha256 docker-compose.yml` empty (not modified) | review |
| 9 | Exactly one `ci.yml` on `main`; no per-branch drift | `git ls-files '.github/workflows/*' \| wc -l == 1`; `git diff origin/main:…ci.yml <branch>:…ci.yml` empty for live branches | review |
| 10 | Dep cache keyed on `uv.lock` | `grep 'cache-dependency-glob: "uv.lock"'` in both `setup-uv` steps; observe cache key changes only when `uv.lock` changes across two runs | review + run logs |
| 11 | `uv sync --locked` still installs the `dev` group (ruff, pytest) | `lint` runs `uv run ruff check`; `eval-gate` runs `uv run pytest` — both succeed → tools present after locked sync. Local: `uv sync --locked && uv run ruff --version && uv run pytest --version` | `lint` + `eval-gate` |
| 12 | `eval-gate-result` fails closed on a **malformed** `enabled` (neither `true` nor `false`) | Predicate harness **row G** (`GATE_CHECK=success ENABLED="" RESULT=skipped → FAIL`); plus inspect `eval-gate-result` body for the `[ "$ENABLED" != "true" ] && [ "$ENABLED" != "false" ]` guard *before* the pass branch | review + harness |
| 13 | `litellm` pin validated before landing (gateway healthy + `eval-gate` green on the candidate digest; `tag → sha256` recorded in PR) | One live CI cycle on the candidate digest shows the gateway reaching `/health/liveliness` and `eval-gate` green; PR description records the exact `tag → sha256`. **Rollout gate, not a static check.** | live CI + PR review |
| 14 | `.github/dependabot.yml` enables the `github-actions` ecosystem (weekly) | File exists with `package-ecosystem: github-actions`, `directory: "/"`, `schedule.interval: weekly`; `actionlint`/Dependabot config validation passes. See `examples/dependabot.yml`. | review |

## Concrete test #1 — `eval-gate-result` predicate harness (table-driven)

The predicate is the only branching logic this spec adds. Extract it verbatim
and drive it over the `design.md` §3 truth table. Idiomatic for a YAML-embedded
shell step: a standalone `bash` harness so it's runnable without GitHub.

```bash
# scratch/test_eval_gate_result.sh  (ILLUSTRATIVE — verification artifact, not shipped)
set -uo pipefail

decide() {  # args: GATE_CHECK ENABLED RESULT  -> echo PASS|FAIL
  GATE_CHECK="$1" ENABLED="$2" RESULT="$3"
  (
    set -euo pipefail
    if [ "$GATE_CHECK" != "success" ]; then exit 1; fi
    if [ "$ENABLED" != "true" ] && [ "$ENABLED" != "false" ]; then exit 1; fi  # row G: malformed -> fail closed
    if [ "$RESULT" = "success" ] || { [ "$ENABLED" = "false" ] && [ "$RESULT" = "skipped" ]; }; then exit 0; fi
    exit 1
  ) && echo PASS || echo FAIL
}

fail=0
check() { got=$(decide "$2" "$3" "$4"); [ "$got" = "$5" ] && echo "ok  $1=$got" || { echo "BAD $1: want $5 got $got"; fail=1; }; }

#     row  GATE_CHECK  ENABLED  RESULT     expect
check A    success     false    skipped    PASS   # fork / no secret
check B    success     true     success    PASS   # evals pass
check C    success     true     failure    FAIL   # real regression
check D    success     true     skipped    FAIL   # lint failed upstream (double-block)
check E    failure     true     skipped    FAIL   # gate-check errored -> fail closed
check F    success     true     cancelled  FAIL   # concurrency cancel
check G    success     ""       skipped    FAIL   # malformed enabled -> fail closed

exit $fail
```

Run: `bash scratch/test_eval_gate_result.sh` → all `ok`, exit 0.

## Concrete test #2 — deliberate regression (proves AC #4)

Force the live gate red on a branch that *has* `OPENAI_API_KEY`, using the real
`app/evals.py` contract: `run()` returns `passed=False` when any check fails —
the absolute floors from `evals/gate.json` (`gates.overall ≥ 0.70`,
`gates.weighted ≥ 0.75`), the per-slice hard floors, the baseline-regression
check, or the served-model pin. (There is **no single `THRESHOLD` constant**; the
gate is multi-check — see `app/evals.py::evaluate`.) Cheapest lever that doesn't
touch the asserting code: corrupt the corpus so answers miss the golden
keywords/reference, which drops `overall`/`weighted` below their floors.

```bash
# on a throwaway branch with the secret available to Actions
printf '%s\n' '{"source":"x","content":"intentionally irrelevant filler"}' > data/corpus.jsonl
git commit -am "test: force eval regression (DO NOT MERGE)"
# CI: ingest succeeds, pytest test_quality_gate fails -> eval-gate result=failure
#     -> eval-gate-result red (row C) -> merge blocked. Close PR after.
```

Local dry-run of the same assertion (needs the stack up, real key):

```sh
make up && make ingest && uv run pytest -q   # -> test_quality_gate FAILS
```

## Concrete test #3 — fail-closed (proves AC #5)

Temporarily make secret-detection error and confirm the gate does **not**
silently pass:

```yaml
# in gate-check.steps[].run, prepended on a throwaway branch:
exit 1   # simulate a broken secret-detection job
```

Expected: `gate-check` ❌ → `eval-gate` skipped → `eval-gate-result` ❌ with
`gate-check did not succeed (failure) — failing closed`. Revert before merge.

## How it gates merge

1. Every PR/push runs `lint`, `gate-check`, and (when the secret is present)
   `eval-gate`; `eval-gate-result` always runs and summarizes.
2. Branch protection on `main` requires **`lint`** and **`eval-gate-result`**.
   A red eval suite (`tests/test_evals.py::test_quality_gate`, which fails when
   `app/evals.run()` reports `passed=False` against the `evals/gate.json` floors)
   makes `eval-gate` fail → `eval-gate-result` red → merge blocked. Lock drift
   makes `lint` red → merge blocked. Fork PRs skip `eval-gate` but
   `eval-gate-result` still reports green, so they remain mergeable.
3. **Rollout ordering (do not invert):** land the workflow, watch one green
   cycle on `main`, *then* run `examples/branch-protection.sh`. Marking a
   mis-named check required before it's observed green wedges every PR (AC #6/#7).
4. **Break-glass:** for a provider outage, drop `eval-gate-result` from required
   contexts (keep `lint`), merge, then restore — see `examples/branch-protection.sh`.

## Fixtures / prerequisites

- `OPENAI_API_KEY` set as an Actions secret (for the pass/fail live paths only).
- `gh` authenticated with admin on the repo (branch-protection PUT).
- `actionlint` (static check); `uv 0.11.24+` locally for the drift repro.
- No new Python fixtures: the eval gate reuses `evals/golden.jsonl`,
  `evals/gate.json`, `data/corpus.jsonl`, `db/init.sql`, and
  `gateway/litellm_config.yaml` exactly as on `main`.
