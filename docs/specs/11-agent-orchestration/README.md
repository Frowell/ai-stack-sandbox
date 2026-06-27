---
title: Agent orchestration (multi-node)
slug: agent-orchestration
area: orchestration
tier: Later
size: L
status: Backlog
depends_on: []
issue:        # set to the GitHub issue number when created
---

# Agent orchestration (multi-node)

> **Area** `orchestration` · **Tier** `Later` · **Size** `L` · **Status** `Backlog` · **Depends on:** — (soft: [guardrails](../09-guardrails/README.md), [budgets-and-virtual-keys](../10-budgets-and-virtual-keys/README.md))

## Summary

Grow `app/agent.py` from a fixed two-node `retrieve -> generate` graph into a
small **supervisor + specialists** graph with **durable, resumable checkpoints**
and **bounded subagent delegation**. State persists in the Postgres container we
already run (via LangGraph's `PostgresSaver`), every node emits an OTel span
nested under the run, and delegation is hard-capped on depth / iterations / token
budget so a misbehaving supervisor can't loop forever. Shipped behind a config
flag so the existing single-graph behaviour is the default until proven.

## Problem / Motivation

A single `retrieve->generate` graph won't cover complex tasks; the thesis is 3-8
specialists with durable state, not one mega-agent. Today the graph state is an
in-memory `TypedDict` (`app/agent.py`): if the process dies mid-run everything is
lost, there is no way to resume, and there is no mechanism for one node to hand
work to another with any guardrail on cost.

## Goals

- Decompose into specialist nodes with focused prompts/tools, coordinated by a
  supervisor node.
- Durable checkpointed state that survives a process restart and can be resumed
  by `thread_id`.
- Principled subagent delegation with **explicit, enforced** when-to-delegate
  rules and hard caps (depth, iterations, token budget).

## Non-goals

- A general agent-framework rewrite (we stay on LangGraph; no custom runtime).
- A production multi-tenant scheduler / queue. Single-process execution is fine
  for this slice; horizontal workers are a later concern.
- Building all 3-8 specialists. This slice ships **one** multi-node workflow plus
  **one** delegation example; the catalogue grows later.
- Cross-session long-term memory and compaction — that is
  [context-management](../12-context-management/README.md), which depends on this.

## Proposed design

**Seam.** Almost all changes live behind `app/agent.py` and the `build_graph()`
factory. Model calls still go through `app.gateway.chat`; tracing still goes
through `app.observability.span`. No change to the **retrieval** seam. The one
exception is the gateway: enforcing `token_budget` needs per-call token usage,
which `gateway.chat` currently throws away — see *Bounded delegation* for the
minimal additive change (the existing `chat()` signature is preserved).

**Topology.** A `supervisor` node routes to specialist nodes (e.g. `retrieve`,
`research`, `synthesize`) using LangGraph conditional edges and returns to the
supervisor until a `done` condition is met. The current `retrieve -> generate`
path remains valid as the degenerate single-pass case.

**Durable state (checkpointer).** Compile the graph with
`langgraph.checkpoint.postgres.PostgresSaver` pointed at the existing
`settings.database_url`. **This saver is NOT bundled with `langgraph`** — it ships
in the separate `langgraph-checkpoint-postgres` distribution and must be added to
`pyproject.toml` (the repo currently only pins `langgraph>=0.2.50`; the resolved
tree is `langgraph` 1.x + `langgraph-checkpoint` but **no** postgres saver). Add
the dependency and re-lock as the first implementation step.

LangGraph owns its checkpoint tables (`checkpoints`, `checkpoint_writes`,
`checkpoint_blobs`) created by `saver.setup()`. This is a **schema change** to the
shared Postgres instance. It must be applied by an **idempotent `saver.setup()`
called at process start when `ORCHESTRATION_MODE=multi`** — *not* by appending to
`db/init.sql`, because `db/init.sql` only runs on first boot of an empty volume
(`docker-entrypoint-initdb.d`) and would silently skip existing volumes, and the
saver's DDL is owned by LangGraph and should not be hand-transcribed into SQL.
`setup()` is safe to call repeatedly. State (`State` TypedDict) must stay
JSON-serialisable — no live connections / clients stored in state.

**Saver lifecycle / no import-time DB connection.** `PostgresSaver` holds a live
psycopg connection (or pool). Today `app/agent.py` builds `GRAPH = build_graph()`
**at import time**; compiling with a saver there would open a Postgres connection
on `import app.agent`, breaking offline/`single`-mode use and any tooling that
imports the module without a DB. The checkpointed graph (and its connection/pool)
must therefore be constructed **lazily** (first `ask()` call, or an explicit
`get_graph()`), and `single` mode must keep the connection-free, import-time path.

**Run identity & resumption.** `ask()` gains an optional `thread_id` (default: a
fresh UUID per call) so a caller can resume. **Backward compatibility is a hard
constraint:** `ask(question) -> str` is called by `app/evals.py`
(`ask(c["question"])`), by `app/agent.py`'s `__main__`, and indirectly by
`tests/test_evals.py`. The default return type therefore **stays `str`**; the
`thread_id`/run id is exposed via a separate, additive API — e.g. an
`ask_resumable(question, thread_id=None) -> AskResult` (a small dataclass carrying
`answer`, `thread_id`, `truncated`). No existing caller signature changes.
In **`multi`** mode `ask()` delegates to `ask_resumable(...).answer`; in **`single`**
mode `ask()` keeps the direct, connection-free `GRAPH.invoke` path (so single mode
opens no DB connection — see the lazy-saver section and AC). Resuming calls the
resumable API with the same `thread_id` and `configurable={"thread_id": ...}`; the
checkpointer replays committed state and continues from the last node.
**Single-mode resumption is a no-op:** with no checkpointer compiled, `thread_id` is
returned for API symmetry but nothing is persisted and a second call with the same
`thread_id` re-runs from scratch — resumption is a `multi`-mode-only guarantee.

**Bounded delegation.** State carries `depth` and `iterations` counters and a
`token_budget`. The supervisor refuses to delegate past `max_depth`
(default 2) or `max_iterations` (default 6), and the run aborts cleanly (returns
best-effort partial answer + a `truncated` flag) when the token budget is
exhausted. Caps are config, not magic numbers in code.

*Cap enforcement is via an explicit terminal path, not an exception.* When any
cap is hit the supervisor routes to a terminal `truncate` node that sets
`truncated=True` and goes to `END`. LangGraph's own `recursion_limit` (default 25)
is a **backstop only** and must be set comfortably above the configured
`max_iterations`/`max_depth` ceiling; the run must end via the `truncate` path
**before** `recursion_limit` is reached, so a misbehaving supervisor returns a
clean partial result rather than raising `GraphRecursionError`.

*Token-budget measurement requires surfacing usage.* `token_budget` cannot be
enforced today: `app.gateway.chat` returns only `resp.choices[0].message.content`
and **discards `resp.usage`**, so there is no per-call token count to subtract
from the budget. This makes the "no change to the gateway seam" statement above
**not literally true for the budget cap**. Resolve by the smallest gateway-compatible
change: add an additive `chat_with_usage()` (or have `chat` optionally return
usage) that exposes `resp.usage.total_tokens`, leaving the existing `chat()`
signature and all current callers untouched. Depth and iteration caps need no
gateway change and remain the primary, always-on guard; the token cap layers on
top. If spec 10 (budgets-and-virtual-keys) lands first, prefer its gateway-level
spend accounting over re-deriving token counts here.

**Config flag.** `ORCHESTRATION_MODE = single | multi` (default `single`) selects
which graph `build_graph()` compiles, so the change is dark-launchable and
instantly revertible. *Note:* `app.config` builds one **frozen** `settings` at
import and captures each env default at class-definition time, so the mode (and the
caps) must be set in the environment **before the process starts** — they are not
flipped mid-process. This makes the flip a deploy-time decision and is why in-process
tests must drive config via `dataclasses.replace`, not `monkeypatch.setenv` (see
[`testing.md`](testing.md) and [`design.md`](design.md) §8). `recursion_limit` is a
derived `@property` (from `max_iterations`, with a `RECURSION_LIMIT` override read at
call time), not a captured-at-import field.

**Components & touch-points.** Every change is additive; nothing existing is
removed. (Concrete, illustrative sketches for each live in [`examples/`](examples/);
the deeper rationale lives in [`design.md`](design.md).)

| Component | File | Change |
| --- | --- | --- |
| Config flags | `app/config.py` | Add `orchestration_mode`, `max_depth`, `max_iterations`, `token_budget` as env-driven frozen fields (same pattern as existing fields, captured at import) plus `recursion_limit` as a derived `@property` (from `max_iterations`, `RECURSION_LIMIT` override read at call time). |
| Graph factory | `app/agent.py` | `build_graph()` branches on `settings.orchestration_mode`; `single` returns today's byte-for-byte `retrieve->generate` compile, `multi` returns the supervisor graph. |
| Multi graph | `app/agent.py` | New `supervisor_node`, specialist nodes (`research_node`, `synthesize_node`, reuse `retrieve_node`), `truncate_node`, and `_route()` for conditional edges with invalid-route fallback. |
| Lazy checkpointed graph | `app/agent.py` | `get_graph()` builds + caches the `multi` graph with `PostgresSaver` on first use; `single` keeps the import-time `GRAPH`. No DB connection at import. |
| Resumable API | `app/agent.py` | New `AskResult` dataclass + `ask_resumable(question, thread_id=None) -> AskResult`; `ask()` delegates and returns `.answer` (signature unchanged). |
| Usage surfacing | `app/gateway.py` | Additive `chat_with_usage(messages, **kwargs) -> ChatResult` exposing `resp.usage.total_tokens`; existing `chat()` untouched. |
| Span nesting on resume | `app/observability.py` | Helper to attach a `resumed_from` link / restore parent context (see open question on trace continuity). |
| Checkpoint schema | startup hook (not `db/init.sql`) | Idempotent `PostgresSaver.setup()` when `mode==multi`. |
| Dependency | `pyproject.toml` + `uv.lock` | Add `langgraph-checkpoint-postgres` (the resolved tree today is `langgraph` 1.2.6 + `langgraph-checkpoint` 4.1.1 with **no** postgres saver). |

**Implementation sequencing.** Each step is independently reviewable and leaves
`single` mode (the default) green:

1. Add `langgraph-checkpoint-postgres` to `pyproject.toml`, re-lock, and assert
   `python -c "import langgraph.checkpoint.postgres"` in the app container.
2. Add the config flags to `Settings` (all defaults preserve today's behaviour).
3. Add `chat_with_usage()` to the gateway (additive; no caller changes).
4. Split `build_graph()` on the flag and add the `multi` topology + `truncate`
   node + routing fallback, still using the **in-memory** default checkpointer so
   it is testable without Postgres.
5. Wire `PostgresSaver` behind `get_graph()` (lazy) + idempotent `setup()`; add
   `ask_resumable`/`AskResult` and make `ask()` delegate.
6. Add span nesting / `resumed_from` link across resume.
7. Dark-launch `multi` in CI/eval; measure variance; only then consider flipping
   the default.

## Acceptance criteria

- [ ] With `ORCHESTRATION_MODE=multi`, at least one multi-node workflow
      (supervisor + ≥2 specialists) runs end-to-end and answers a golden question.
- [ ] State is checkpointed in Postgres: a run interrupted after a node commit can
      be resumed by `thread_id` in a **fresh process** and completes without
      re-running already-committed nodes. Proof requires a **durable** execution
      marker (e.g. a row/counter in Postgres written by the committed node), not an
      in-process counter — the killed process takes any in-memory count with it, so
      the test asserts the committed node's durable marker shows exactly one
      execution after resume (see [`testing.md`](testing.md) AC #2).
- [ ] Per-node spans are nested under the run span in observability; trace context
      is propagated across a resume so resumed nodes attach to the original run
      (or, if a new trace is unavoidable, they carry a `resumed_from` link).
- [ ] Subagent delegation has explicit, **enforced** when-to-delegate rules, and a
      run cannot exceed `max_depth`, `max_iterations`, or `token_budget`; exceeding
      any cap routes to the `truncate` terminal node and returns a `truncated`/partial
      result — verified by a test that a deliberately-looping supervisor ends via the
      cap path and **never raises `GraphRecursionError` or hangs**.
- [ ] `token_budget` enforcement consumes real per-call usage (`resp.usage`) surfaced
      from the gateway via the additive usage API; a unit test drives a run past a
      small budget and asserts `truncated=True`. (Depth/iteration caps are tested
      independently and require no gateway change.)
- [ ] `ask(question) -> str` keeps its exact signature and return type; `app/evals.py`,
      `app/agent.py.__main__`, and `tests/test_evals.py` run unchanged. Resumption is
      exercised only through the additive resumable API.
- [ ] `ORCHESTRATION_MODE=single` runs the **identical** `retrieve->generate` code
      path as today — same nodes, no checkpointer, no DB connection opened at import —
      so behaviour is unchanged (the graph wiring is byte-for-byte; LLM output is not
      asserted to be deterministic).
- [ ] `langgraph-checkpoint-postgres` is added to `pyproject.toml` and the lockfile,
      and `python -c "import langgraph.checkpoint.postgres"` succeeds in the
      app container.
- [ ] Supervisor routing is robust to an out-of-range/invalid route decision: an
      unrecognised next-node falls back to the `truncate`/`done` path rather than
      crashing the graph (tested).
- [ ] Caps are genuinely **config-driven, not hard-coded**: a test sets a *non-default*
      `max_iterations`/`token_budget` (via the supported in-process mechanism —
      `dataclasses.replace` on the module `settings`, since `monkeypatch.setenv` is a
      no-op against the import-time frozen config) and asserts the run truncates at the
      **configured** value, not the default. The token-budget unit test stays hermetic
      (pins `get_graph` to the in-memory multi graph; no `PostgresSaver` connection).
- [ ] The eval gate (`uv run pytest`) still passes in `multi` mode and does not
      become flaky (variance documented; threshold or scoring adjusted if needed).
- [ ] Checkpoint tables are created by an idempotent setup/migration step, not by
      hand, and `make up` on a clean volume still boots.

## Dependencies

- None hard. **Soft / sequencing:** the delegation caps should reuse, not
  reinvent, the spend controls from [budgets-and-virtual-keys](../10-budgets-and-virtual-keys/README.md)
  and the I/O checks from [guardrails](../09-guardrails/README.md) if those land
  first. [context-management](../12-context-management/README.md) depends on this
  spec's durable-state foundation.

## Open questions

- **Checkpoint backend:** decided — `PostgresSaver` (reuse the existing container,
  durable; one less moving part than Redis). Requires adding the
  `langgraph-checkpoint-postgres` package (resolved tree is LangGraph 1.x). Open
  sub-question: pin a compatible saver version against LangGraph 1.x and confirm the
  `setup()`/`get_state`/`configurable={"thread_id"}` API is stable on that pin.
- **Token-usage surfacing:** confirm the gateway/LiteLLM response reliably populates
  `usage.total_tokens` for the configured providers (some streaming/embedding paths
  omit it). If usage is unreliable, fall back to depth/iteration caps as the binding
  guard and treat `token_budget` as best-effort — or defer the token cap to spec 10.
- **Trace continuity across resume:** can we serialise/restore the OTel span
  context in the checkpoint, or do we accept a linked-but-new trace on resume?
- **Eval determinism:** does multi-node raise score variance enough to need a
  larger golden set ([eval-set-maturity](../06-eval-set-maturity/README.md)) or a
  seeded/temperature-0 judge before the gate is trustworthy?
- **Specialist catalogue:** which 2-3 specialists are worth building first, and do
  any need tools with side effects (which would force idempotency work)?
- **Saver connection lifecycle & thread-safety:** `PostgresSaver` holds one live
  psycopg connection. The eval gate runs `ask()` over many cases in one process
  (`app/evals.py`), and pytest may parallelise. Decide whether the lazily-built
  graph shares a single saver connection (must then be thread-safe / serialised) or
  uses a `ConnectionPool`, and where it is closed on shutdown so the process does
  not leak connections. Default for this slice: a single connection guarded for
  sequential use (eval loop is sequential today); revisit if runs go concurrent.
- **Checkpoint retention / growth:** every run writes multiple rows to
  `checkpoints`/`checkpoint_writes`/`checkpoint_blobs` and nothing prunes them, so
  the shared Postgres grows unbounded across runs. Decide a retention policy (TTL or
  periodic delete of completed `thread_id`s) before flipping the default to `multi`;
  acceptable to defer the cleanup job for the proof slice but not for default-on.

## Risks & mitigations

- **Runaway cost / infinite delegation loops** (highest risk). A supervisor that
  keeps spawning subagents burns tokens without bound. *Mitigation:* hard caps on
  depth/iterations/token budget enforced in the supervisor, plus the gateway-level
  budget from spec 10; an integration test asserts a deliberately-looping
  supervisor is killed at the cap.
- **Non-idempotent nodes + checkpoint replay.** Resuming re-enters the last node;
  a node with side effects could double-execute. *Mitigation:* keep the first
  slice's nodes pure (retrieve/generate/synthesize), and make any side-effecting
  node idempotent or guarded by a state flag before adding it.
- **Schema drift / migration.** Checkpoint tables are new state in the shared
  Postgres. *Mitigation:* idempotent `saver.setup()` / migration; `make down -v`
  + `make up` verified on a clean volume in CI.
- **Eval-gate flakiness.** More LLM calls per case → more variance and cost.
  *Mitigation:* measure variance before flipping the gate to multi mode; adjust
  threshold/scorer or grow the golden set first.
- **State bloat / serialisation errors.** Large or non-JSON state breaks the
  saver. *Mitigation:* keep `State` JSON-serialisable; cap context size carried in
  state (full solution deferred to context-management).

## Accepted risks / deferred

- Horizontal/distributed execution, queueing, and human-in-the-loop interrupts are
  **out of scope** for this slice (single-process only). Accepted.
- Long-term cross-session memory and compaction are deferred to
  [context-management](../12-context-management/README.md). Accepted.
- The full 3-8 specialist catalogue is deferred; this slice proves the pattern
  with one workflow + one delegation example. Accepted.
- A checkpoint **cleanup/retention job** is out of scope for the proof slice
  (`multi` is dark-launched, not default-on), so unbounded checkpoint-table growth
  is accepted *only while the default stays `single`*; a retention policy is a
  prerequisite for flipping the default. Accepted (with that gate).
- In `multi` mode `app/evals.py` consumes `ask() -> str` and therefore cannot see
  the `truncated` flag; a budget-truncated partial answer simply scores low at the
  gate rather than being flagged as truncated. Accepted for this slice.
- **Token cap is effectively dormant in normal runs.** The proof slice's
  deterministic supervisor runs each specialist roughly once (~3-4 iterations), well
  under the 20 000-token default, so the always-on guards are depth/iterations; the
  token cap is exercised mainly by its synthetic unit test. Accepted — it is a
  layered backstop, not the primary guard.
- **Config-model asymmetry:** every `Settings` field is captured at import, but
  `recursion_limit` is a derived `@property` that re-reads `RECURSION_LIMIT` at call
  time (so it can track `max_iterations` and honour a test/deploy override). This is
  intentional (documented in [`design.md`](design.md) §8) but is a deliberate
  inconsistency in the otherwise frozen-at-import config. Accepted (Low).

## Test & rollout plan

- **Unit:** supervisor routing decisions, including the **invalid/out-of-range route
  → fallback** case; cap enforcement (depth/iterations/budget route to `truncate`
  and set `truncated`, never raise `GraphRecursionError`); token cap consumes real
  `resp.usage`; `State` round-trips through the saver; `ask()` signature/return
  unchanged.
- **Integration:** kill a run after a node commit and resume by `thread_id` in a
  fresh process → completes without re-running committed nodes; a looping
  supervisor is terminated at the cap **via the `truncate` path before
  `recursion_limit`**; spans nest correctly under the run; `import app.agent` opens
  **no** DB connection in `single` mode.
- **Eval:** `uv run pytest` (the existing gate) passes in `multi` mode; record
  score variance vs. `single`.
- **Rollout:** ship behind `ORCHESTRATION_MODE` (default `single`). Apply the
  checkpoint migration, dark-launch `multi` in CI/eval, then flip the default once
  variance and cost are acceptable. Rollback = set the flag back to `single`; no
  data migration needed to revert (checkpoint tables are additive).

## References

- [`design.md`](design.md) — alternatives considered, interface sketches, the
  state machine / routing diagram, and resume edge cases.
- [`examples/`](examples/) — illustrative, codebase-specific sketches of every
  touch-point (config, multi graph, gateway usage API, checkpointer setup,
  tests). **Spec, not wired-in code.**
- [`testing.md`](testing.md) — how each acceptance criterion is proven and how it
  gates merge via the eval/CI gate. **Today the gate is the local `uv run pytest`
  (`make test`)**; the `eval-gate` CI *job* (and the `.github/` tree it lives in)
  does not exist yet — it lands with [ci-hardening](../07-ci-hardening/README.md).
  This spec only adds tests to that gate; it does not introduce a CI seam.
- [Feature roadmap](../../ROADMAP.md)
- [Specs index](../README.md)
- Touches: `app/agent.py` (graph + `build_graph`, lazy checkpointed graph,
  `ask_resumable`), `app/config.py` (new flags: `ORCHESTRATION_MODE`, `MAX_DEPTH`,
  `MAX_ITERATIONS`, `TOKEN_BUDGET`), `app/gateway.py` (additive usage-returning
  call for the token cap), `pyproject.toml` + `uv.lock` (add
  `langgraph-checkpoint-postgres`), `app/observability.py` (span nesting across
  resume), `app/evals.py` (gate in multi mode). The checkpoint schema is created
  by `saver.setup()` at startup, **not** by editing `db/init.sql`.
