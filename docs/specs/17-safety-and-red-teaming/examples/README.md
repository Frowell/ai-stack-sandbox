# Examples — illustrative only

> **These files are a specification, not wired-in code.** They show the intended
> signatures, file paths, and shapes for [spec 17](../README.md). They are **not**
> imported by the app, are **not** on any import path, and must not be copy-pasted
> without review. Real signatures must be reconciled with specs 01 (the `chat()`
> success seam) and 06/PR #1 (the gate contract, `gate_config.yaml`,
> `redteam.jsonl` schema) at integration time.

| File | Mirrors | Shows |
|---|---|---|
| [`app_safety.py`](app_safety.py) | new `app/safety.py` | closed label set, `detect_refusal`, deterministic fallback + `safety.refusal` span, per-slice `EXPECTED_LABELS` contract, `case_passed`, ASR rollup |
| [`app_agent.py`](app_agent.py) | changed `app/agent.py` | `ask(question, inject_context=…)`, post-retrieval splice in `retrieve_node`, refusal handling in `generate_node` |
| [`app_gateway.py`](app_gateway.py) | changed `app/gateway.py` (with spec 01) | `chat()` raising on transport failure so empty-success ≠ transport-empty; stays a thin transport |
| [`app_evals_safety.py`](app_evals_safety.py) | added to `app/evals.py` | `safety_judge` (pinned, closed-label), `run_safety()` (inverted scorer, ASR, baseline-diff gate) |
| [`redteam.jsonl`](redteam.jsonl) | new `evals/redteam.jsonl` | adversarial corpus across the 5 slices; golden-compatible + safety fields |
| [`redteam_baseline.json`](redteam_baseline.json) | new `evals/redteam_baseline.json` | recorded per-slice ASR baseline the gate compares against |
| [`gate_config.safety.yaml`](gate_config.safety.yaml) | snippet added to `evals/gate_config.yaml` (owned by spec 06/PR #1) | `safety` slice floor, pinned safety-judge model, ASR-regression delta, blocking subset |
| [`ci_eval_gate.safety.yaml`](ci_eval_gate.safety.yaml) | snippet added to `.github/workflows/ci.yml` (owned by PR #3/spec 07) | running the safety suite in the secret-gated `eval-gate` (report-only → blocking subset; full nightly) |
| [`Makefile.safety.snippet`](Makefile.safety.snippet) | targets added to `Makefile` | `make eval-safety`, `make safety-baseline` |

Concrete tests are in [`../testing.md`](../testing.md), in the project's pytest idiom.
