# Examples — illustrative only

> **These files are a specification, not wired-in code.** They are not imported by
> the app, not on any build path, and intentionally live under `docs/`. They show
> the *real* signatures, file paths, and config/data shapes the implementation
> should match so a reviewer can judge the design before any code lands.
>
> - Python files are named `example_*.py` (not `test_*.py` / `*_test.py`), so
>   pytest's default discovery does **not** collect them while they live here.
> - Lines whose exact form is co-owned with PR #1 (the seam signatures) are
>   flagged inline with `# PR#1`.
>
> When implementing, port the relevant pieces into the real files under `app/`,
> `evals/`, `tests/`, the `Makefile`, and `pyproject.toml` — do **not** import from
> this directory, and do **not** copy the `example_` data files in verbatim without
> doing the real curation + redaction.

| File | Mirrors / would become | Shows |
|---|---|---|
| [`example_gate_config.yaml`](example_gate_config.yaml) | `evals/gate_config.yaml` (new) | floors, `slices.high_value`, weights policy, regression delta, N, price map, budgets |
| [`example_golden.jsonl`](example_golden.jsonl) | `evals/golden.jsonl` (grown) | the `id`/`slice`/`weight`/`expect` schema across all slices (illustrative cases) |
| [`example_baseline.json`](example_baseline.json) | `evals/baseline.json` (new, committed) | per-case + per-slice + overall scores, pins, timestamp |
| [`example_gateway.py`](example_gateway.py) | `app/gateway.py` | usage-capture seam: `chat(..., return_usage=...)` |
| [`example_agent.py`](example_agent.py) | `app/agent.py` | sampling seam: `ask(q, *, gen_kwargs=...)` threaded to `generate_node` |
| [`example_evals.py`](example_evals.py) | `app/evals.py` (rewrite) | config load + schema validation + slice routing + N-sampling + cost/latency + baseline diff + all gates |
| [`example_tests.py`](example_tests.py) | `tests/test_evals.py` | precise skip predicate + one offline test per acceptance criterion |
| [`example_Makefile.snippet`](example_Makefile.snippet) | `Makefile` | the `eval-baseline` target |
| [`example_pyproject.snippet.toml`](example_pyproject.snippet.toml) | `pyproject.toml` | the `pyyaml>=6` dependency line |

See [`../design.md`](../design.md) for why each shape was chosen and
[`../testing.md`](../testing.md) for how each acceptance criterion is proven and
gates merge.
