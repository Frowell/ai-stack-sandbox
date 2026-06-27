---
title: Context management
slug: context-management
area: orchestration
tier: Later
size: M
status: Backlog
depends_on: [agent-orchestration]
issue:        # set to the GitHub issue number when created
---

# Context management

> **Area** `orchestration` · **Tier** `Later` · **Size** `M` · **Status** `Backlog` · **Depends on:** [agent-orchestration](../11-agent-orchestration/README.md)

## Summary

Keep long, multi-turn agent runs inside the model's context window (compaction /
context-editing) and let a small amount of state survive across sessions
(cross-session memory). Both live behind the orchestration seam (the LangGraph in
`app/agent.py`) and reuse the existing Postgres/Redis substrate rather than adding
a new store. This is **not** a vector-memory product; it is the minimum honest
slice that demonstrates the two seams: a token-budgeted working context and a
durable, scoped memory record.

## Problem / Motivation

Long runs blow the context window; nothing persists across sessions. Today
`app/agent.py` is single-shot: `ask(question) -> answer` with a `State` of
`{question, context, answer}` and no message history, no checkpointer, and no
token accounting. There are literally no "long runs" or "sessions" yet — those
are introduced by [agent-orchestration](../11-agent-orchestration/README.md)
(durable checkpointed state). This spec is the layer that bounds and persists
that state once it exists.

## Goals

- **Compaction / context-editing** for long runs: when the working context
  approaches a budget, summarize or drop the oldest turns while preserving live
  task state (open subtasks, decisions, citations).
- **Cross-session memory**: a small, explicitly-scoped record that can be written
  during a run and reloaded into a later run for the same scope.
- A **token budget seam** that is provider-aware even though the model is hidden
  behind a gateway alias.

## Non-goals

- A vector-memory product / general long-term memory store.
- Semantic recall over arbitrary history (that overlaps retrieval + caching #8;
  out of scope here).
- Multi-tenant memory isolation as a product feature (we reuse the scoping key
  from [budgets-and-virtual-keys](../10-budgets-and-virtual-keys/README.md) if
  present, but do not build tenancy here).

## Proposed design

> Sketch only — full design/examples/tests are deferred until the dependency
> (#11) is expanded. See Open questions for what must be settled first.

**Seam:** orchestration. Implemented as graph nodes/middleware around the
existing `retrieve -> generate` graph, plus two stores reusing existing infra.

1. **Working context + compaction.**
   - Introduce a `messages: list[dict]` (running transcript) into the graph
     `State`, populated only once #11 adds multi-turn runs + a checkpointer.
   - A `compact` step runs before `generate`. The budget is measured against the
     **full assembled prompt** `generate_node` will send — system prompt +
     retrieved context + injected memory + `messages` — **not the transcript
     alone**. (Only `messages` and over-budget retrieved context are compactable;
     the system prompt is fixed overhead.) If `estimate_tokens(full_prompt) >
     budget`, summarize the oldest turns into a single "running summary" message,
     preserving a structured **task-state block** (open items, decisions,
     citation ids) verbatim so summarization cannot silently drop it.
   - **Fixed-part overflow.** If the non-compactable parts (system prompt +
     minimum retrieved context + injected memory) *alone* exceed the budget,
     compacting `messages` cannot help. The step then trims lowest-priority parts
     in a defined order (injected memory → oldest retrieved context, **never** the
     system prompt) and, if still over budget, fails loud (raises / sets a
     `truncated` flag) rather than sending an over-budget prompt. Has its own
     unit test.
   - **Token estimation:** the app calls the gateway through the **OpenAI SDK**
     (`app/gateway.py`), not `litellm` directly — litellm runs inside the gateway
     container, so `litellm.token_counter` is **not** an app-side import today.
     Client-side counting needs one of: (a) add `litellm` as an app dependency,
     (b) use `tiktoken` directly, or (c) count approximately and reconcile against
     the gateway's real `resp.usage` after each call (see Open questions).
     **Seam note:** today `gateway.chat()` returns only the message string and
     **discards `resp.usage`**, so options (c) and the tokens-after observability
     attribute (below) both require usage to be surfaced. #11 already introduces
     exactly this as an *additive* usage-returning variant (`chat_with_usage()` /
     optional usage return) exposing `resp.usage.total_tokens` while leaving the
     `chat()` signature intact; this spec **consumes that #11 seam and must not
     add a competing gateway change**. The
     budget is keyed on the *resolved* provider model, not the alias: the gateway
     hides the provider, so the window is derived at the gateway seam (config map
     of `alias -> context_window`, with a conservative default and a config
     override) rather than hard-coded. `resp.usage.total_tokens` (via #11's usage
     API) is also the source for the tokens-after observability attribute.
   - Compaction is itself an LLM call (cost/latency/lossy): cap it to one pass
     per step, fall back to oldest-turn-drop if the summary still exceeds budget
     (no unbounded recursion), and make it idempotent on retry (keyed by run id +
     turn count).

2. **Cross-session memory.**
   - New Postgres table `memory(scope_key TEXT, key TEXT, value JSONB, updated_at
     TIMESTAMPTZ, PRIMARY KEY(scope_key, key))`. Postgres, not a new service.
   - **Table creation must mirror #11's pattern, not append to `db/init.sql`.**
     `db/init.sql` runs **only on first boot of an empty volume**
     (`docker-entrypoint-initdb.d`) and is silently skipped on every already-
     initialized volume — exactly the pitfall #11 calls out for the checkpointer
     tables. If the `memory` table were added by appending to `db/init.sql`, no
     existing dev/CI environment would ever get the table, and combined with the
     "no-op when absent" fallback below the memory feature would **silently never
     work** (and the cross-session persistence test would pass only on a freshly
     wiped volume). Instead, create it with an **idempotent `CREATE TABLE IF NOT
     EXISTS memory (...)` executed at process start when `CONTEXT_MGMT_ENABLED`**
     (the same place/way #11 runs `saver.setup()`), so the schema converges on
     existing volumes too. The "no-op when absent" behaviour is retained only as a
     **defensive fallback** for the disabled path, never as the primary creation
     mechanism.
   - Write path: an explicit `remember(scope_key, key, value)` helper; the agent
     writes only whitelisted, structured facts — never raw model output verbatim
     — to limit memory-poisoning blast radius.
   - Read path: on run start, load **at most `MEMORY_MAX_ROWS`** rows for
     `scope_key`, ordered `updated_at DESC` (most-recent-wins), and inject them
     into the system prompt under a clearly delimited, **untrusted** section;
     instruct the model to treat them as hints, not instructions. The cap is
     mandatory: an unbounded scope could otherwise inject enough memory to blow
     the token budget on its own (see Fixed-part overflow). Injected memory
     counts toward the budget and is the first thing trimmed under overflow.
   - `scope_key` defaults to a CLI/session arg; if virtual keys (#10) exist, use
     that identity. Concurrency handled by `INSERT ... ON CONFLICT DO UPDATE`.

3. **Config / flags.** New fields on `app/config.py:Settings` (the frozen
   `Settings` dataclass, read from env): `CONTEXT_MGMT_ENABLED` (single switch),
   `CONTEXT_BUDGET_TOKENS`, and `MEMORY_MAX_ROWS`. Off by default; with it off the
   graph behaves exactly as today. Memory load/write no-ops as a defensive
   fallback if the table is somehow absent, but the table is created idempotently
   at process start when the flag is on (see Cross-session memory) so the happy
   path does not depend on `db/init.sql` having run.

## Acceptance criteria

- [ ] **Compaction keeps task state.** A scripted ≥N-turn run whose raw
      transcript exceeds the configured budget completes with
      `estimate_tokens(prompt) <= budget` on every `generate` call, and the
      preserved task-state block (a specific citation id / fact introduced in an
      early turn) is **still present verbatim in the assembled prompt** after
      compaction. The primary assertion is on the structured block's presence
      (deterministic), **not** on the model's final NL answer, to keep the CI gate
      from flaking on LLM-summarizer non-determinism; an optional softer judge
      check on the final answer may run but must not be the gating assertion.
      Verified by an automated test, not a manual demo.
- [ ] **No unbounded growth / recursion.** Compaction terminates in ≤1 summary
      pass + bounded oldest-turn drop even when the summary itself is over budget;
      covered by a unit test with a stub summarizer.
- [ ] **Fixed-part overflow is handled, not silently violated.** When the
      non-compactable parts (system prompt + minimum context + injected memory)
      alone exceed the budget, the step trims in the defined order and, if still
      over, raises / flags `truncated` — it never emits an over-budget prompt.
      Unit test asserts both the trim order and the loud-failure path.
- [ ] **Injected memory is bounded.** Read path loads ≤ `MEMORY_MAX_ROWS` rows
      (most-recent-first) regardless of how many rows exist for the scope; test
      with > `MEMORY_MAX_ROWS` rows asserts the cap and ordering.
- [ ] **Budget is provider-aware.** Switching the `chat` alias to a
      different-window provider (gateway config) changes the effective budget via
      the alias→window map, with a safe default when the model is unknown. Test
      asserts the resolved budget, not a hard-coded number.
- [ ] **Memory persists and is reused across sessions.** Process A writes a fact
      under `scope_key`; a fresh process B for the same `scope_key` produces an
      answer that depends on that fact; a process C with a *different* `scope_key`
      does **not** see it (scoping/isolation check). Automated. The test runs
      against an **already-initialized volume** (it must not wipe/recreate the DB
      first), proving the table is created by the runtime idempotent path rather
      than relying on `db/init.sql` having run on a fresh volume.
- [ ] **Safe-by-default.** With `CONTEXT_MGMT_ENABLED` unset the graph output is
      byte-for-byte unchanged from today; injected memory is delimited and
      labeled untrusted in the prompt.
- [ ] **Observability.** `compact` and memory read/write emit nested spans under
      the run (per #11's per-node-span convention), including tokens-before /
      tokens-after on compaction.

## Dependencies

- [agent-orchestration](../11-agent-orchestration/README.md) — **hard
  prerequisite.** Multi-turn runs, a message transcript in `State`, and a durable
  checkpointer must exist before there is anything to compact or persist. This
  spec cannot start until #11 is expanded and at least minimally implemented.
- Optional: [budgets-and-virtual-keys](../10-budgets-and-virtual-keys/README.md)
  for the memory `scope_key` identity.
- Reuses existing Postgres (`db/init.sql`) and the gateway seam
  (`app/gateway.py`).

## Open questions

- **(Blocking)** What is the concrete shape of a "run"/"session" and the
  checkpointer in #11? Until that exists, `State.messages`, run ids, and
  `scope_key` lifetime are undefined. Resolve in #11 first.
- **(Blocking)** How is the resolved provider model obtained behind the gateway
  alias for token counting — config map only, or can LiteLLM report the resolved
  model on the response? If only via config, the alias→window map is a new
  maintenance surface that must stay in sync with `gateway/litellm_config.yaml`.
- Compaction strategy: LLM summary vs. structured trimming vs. hybrid — which
  gives acceptable task-state retention at lowest cost? Needs a small eval.
- Memory schema scope: flat key/value JSONB (chosen above) vs. typed records —
  enough for the demo?
- **Eval-gate shape (depends on #11).** The current gate runs `ask(question) ->
  answer` over `evals/golden.jsonl` (one question → one answer). A multi-turn
  compaction case cannot be expressed until #11 reshapes `ask()` to accept a
  multi-turn run / `thread_id`. So the long-run eval case is gated on #11's
  harness change; the *memory* case (write-then-read) may be expressible sooner.
- Token-counting dependency: add `litellm` to app deps, use `tiktoken`, or rely
  on gateway-reported `resp.usage`? Each has a different accuracy/footprint
  trade-off; pick before implementation (see Proposed design).

## Risks & mitigations

- **Lossy compaction drops task state** → preserve a verbatim structured
  task-state block outside the summarized region; assert it in the eval.
- **Memory poisoning / cross-session prompt injection** → write only whitelisted
  structured facts (never raw model output), inject under an untrusted-delimited
  prompt section, and scope strictly by `scope_key` (isolation tested).
- **PII / data retention in the memory table** → memory is opt-in, scoped, and
  small; document a TTL/clear path. *Accepted risk for the sandbox: no
  encryption-at-rest beyond Postgres defaults.*
- **Cost/latency from compaction LLM calls** → one bounded pass per step, only
  when over budget, with a non-LLM fallback.
- **Budget drift vs. real provider window** → conservative default window and
  config override; alias→window map reviewed when the gateway config changes.
- **Client-side token estimate ≠ gateway tokenizer** → estimate is approximate
  (different SDK/tokenizer than the resolved provider); keep a safety margin
  below the true window and reconcile against `resp.usage` post-call. An estimate
  that under-counts could still send an over-window prompt and get a provider
  error — the margin absorbs this.
- **Flaky CI from LLM-based compaction in the eval gate** → the gating
  assertions (task-state retention, fixed-part overflow, termination) are
  deterministic — structured-block presence + budget math + a stub summarizer in
  unit tests — never the model's free-text answer. Any judge-scored check on the
  final NL answer is advisory only, so a non-deterministic summary cannot flap the
  merge gate.
- **Accepted risk:** semantic/long-term recall and multi-tenant isolation are
  explicitly out of scope (Non-goals); revisit only if a concrete need appears.

## Test & rollout plan

- **Unit:** token-estimate budget math over the *full assembled prompt*;
  fixed-part overflow (trim order + loud failure); compaction termination with a
  stub summarizer; memory upsert/scoping (`ON CONFLICT`, cross-scope isolation)
  and the `MEMORY_MAX_ROWS` cap + ordering.
- **Integration:** scripted long run under a small budget (task-state retention);
  two-process write-then-read memory demo + negative cross-scope case.
- **Eval gate:** add a long-run / memory case to the golden set
  (`evals/golden.jsonl`, exercised by `app/evals.py`) so compaction regressions
  that lose task state fail CI. *The long-run case requires #11 to first reshape
  `ask()` for multi-turn input — see Open questions; until then only the
  single-turn memory case is wireable.*
- **Rollout:** off by default behind `CONTEXT_MGMT_ENABLED`. The `memory` table
  is created by an **idempotent `CREATE TABLE IF NOT EXISTS memory ...` run at
  process start when the flag is on** — *not* by appending to `db/init.sql`,
  which only runs on first boot of an empty volume and would skip existing
  volumes (the same pitfall #11 documents for its checkpointer tables). For
  parity, `db/init.sql` may optionally also carry the statement so brand-new
  volumes get it on boot, but the runtime path is the source of truth. No
  destructive change; reversible by disabling the flag (memory table can be left
  in place or dropped).

## References

- [Feature roadmap](../../ROADMAP.md)
- [Specs index](../README.md)
- [agent-orchestration](../11-agent-orchestration/README.md) (prerequisite)
- [budgets-and-virtual-keys](../10-budgets-and-virtual-keys/README.md) (optional scope key)
