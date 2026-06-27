# Design notes — Agent orchestration (multi-node)

Deeper design behind [`README.md`](README.md). Covers the topology and state
machine, the seam decisions, alternatives considered (and why rejected),
interface sketches, and the edge cases that bite on resume. Illustrative code for
every interface lives in [`examples/`](examples/).

## 1. State machine & topology

`single` mode is unchanged: `START -> retrieve -> generate -> END`.

`multi` mode introduces a supervisor loop. The supervisor is the only node that
*decides*; specialists only *do work* and return to the supervisor. Every cap is
enforced on the supervisor's outgoing edge, and the only way to leave the graph is
through `END` (a clean answer) or `truncate -> END` (a capped partial answer).

```
                    +-------------------------------+
                    |                               |
  START --> supervisor --_route()--> retrieve ------+
                    |  \                             |
                    |   +-----------> research ------+
                    |   \                            |
                    |    +----------> synthesize ----+
                    |
                    |  (done?) ----------------> END
                    |
                    |  (cap hit OR invalid route) --> truncate --> END
                    +
```

- `supervisor_node` reads state, increments `iterations`, and writes `next`
  (the route decision) + any bookkeeping. It performs no retrieval/LLM "work"
  itself beyond deciding the route (the route decision *may* be an LLM call; for
  the proof slice a deterministic rule keyed on which fields are populated is
  enough and far cheaper to test — see §4).
- `_route(state)` is the conditional-edge function. It maps `state["next"]` to a
  node name, but first applies the **guards** (depth / iterations / token budget)
  and the **invalid-route fallback**. Guards win over the supervisor's stated
  intent: if a cap is exceeded, `_route` returns `"truncate"` regardless of what
  `next` says.
- Specialist nodes (`retrieve_node` is reused as-is; `research_node`,
  `synthesize_node` are new) each `add_edge(node, "supervisor")` so control always
  returns to the supervisor.
- `truncate_node` sets `truncated=True`, fills `answer` with the best-effort
  partial (e.g. a synthesis of whatever context/notes exist, or a fixed "budget
  exhausted" message if nothing usable), and edges to `END`.

### Why guards live in `_route`, not inside nodes

If a node raised on cap-exceeded, the failure would surface as an exception out of
`graph.invoke()` and the partial state would be lost to the caller. Routing to a
terminal `truncate` node instead keeps the result a normal return value carrying
`truncated=True` and the partial answer — which is exactly what the acceptance
criteria require ("returns a `truncated`/partial result … never raises
`GraphRecursionError` or hangs"). LangGraph's own `recursion_limit` stays a
backstop set comfortably above `max_iterations` (see §5).

## 2. State shape

`State` must stay JSON-serialisable for `PostgresSaver` (no live connections,
clients, or span objects in state). The `single`-mode `State` is a strict subset,
so the two graphs can share the type; `multi` adds the orchestration fields.

| Field | Type | Meaning |
| --- | --- | --- |
| `question` | `str` | the user question (existing) |
| `context` | `list[tuple[int,str]]` | retrieved `(doc_id, content)` (existing) |
| `answer` | `str` | final/partial answer (existing) |
| `next` | `str` | supervisor's route decision (`retrieve`/`research`/`synthesize`/`done`) |
| `depth` | `int` | current delegation depth |
| `iterations` | `int` | supervisor turns taken |
| `token_budget` | `int` | tokens allowed for the run |
| `tokens_used` | `int` | running total from `resp.usage.total_tokens` |
| `truncated` | `bool` | set by `truncate_node` |
| `notes` | `list[str]` | scratch space specialists append to (kept small) |

> Tuples round-trip through JSON as lists; the reducer/consumers must accept lists
> back. Confirm the saver's serde (msgpack/JSON) preserves `context` shape, or
> normalise to lists explicitly. Tracked as a test in [`testing.md`](testing.md).

## 3. Seam decisions

- **Graph stays behind `build_graph()` / `app.agent`.** Callers (`app/evals.py`,
  `__main__`, `tests/test_evals.py`) see no new required surface. The resumable
  API is strictly additive (`ask_resumable`, `AskResult`); `ask()` keeps
  `(question: str) -> str`.
- **Model calls stay behind the gateway.** The only gateway change is additive:
  `chat_with_usage()` returns text **and** `usage.total_tokens`. `chat()` is
  untouched, so retrieval, evals' judge, and the supervisor's optional LLM route
  call keep working. Rationale for not just changing `chat()`'s return type: it is
  called in three places that expect `str`; widening it would ripple. See
  [`examples/gateway_usage.py`](examples/gateway_usage.py).
- **Tracing stays behind `span()`.** Per-node spans wrap each node body exactly as
  `generate_node` does today. The only new need is parent-context continuity on
  resume (§6).
- **Checkpoint schema is owned by LangGraph**, applied by `setup()` at startup,
  never hand-written into `db/init.sql` (that file only runs on a fresh volume via
  `docker-entrypoint-initdb.d` and would silently skip existing volumes).

## 4. Alternatives considered

| Decision | Chosen | Alternatives & why rejected |
| --- | --- | --- |
| Checkpoint backend | `PostgresSaver` on the existing container | **`MemorySaver`**: not durable, fails the resume criterion. **Redis saver**: another moving part; we already run Postgres. **SqliteSaver**: not shared across the compose network / processes. |
| Cap enforcement | terminal `truncate` node via `_route` guards | **Raise an exception**: loses partial state, surfaces as a crash. **Rely only on `recursion_limit`**: raises `GraphRecursionError`, no partial answer, no per-cap distinction. |
| Token measurement | additive `chat_with_usage()` | **Re-tokenise locally** (tiktoken): drifts from provider accounting, extra dep. **Change `chat()` signature**: ripples to 3 callers. **Defer to spec 10's gateway spend**: preferred *if it lands first*; otherwise this is the minimal self-contained path. |
| Supervisor route decision | deterministic rule for the proof slice; LLM route optional | **Always LLM-routed**: more realistic but adds variance/cost to every turn and is harder to unit-test deterministically. A deterministic router (route by which state fields are populated) lets routing/cap/fallback tests be hermetic; an LLM router can layer on once the scaffolding is proven. |
| Resumable API shape | new `ask_resumable` + `AskResult` dataclass | **Overload `ask()` to return a tuple/dict**: breaks the `-> str` contract and the three existing callers. **Thread `thread_id` through `ask()` and change return**: same breakage. |
| Graph construction timing | lazy `get_graph()` for `multi`, import-time `GRAPH` for `single` | **Always build at import**: opens a Postgres connection on `import app.agent`, breaking offline/`single` use and any tool that imports the module without a DB. |

## 5. Caps & the recursion backstop

Three configured caps (`max_depth`, `max_iterations`, `token_budget`) plus
LangGraph's `recursion_limit`:

- `_route` checks `iterations >= max_iterations`, `depth > max_depth`, and
  `tokens_used >= token_budget` **before** honouring `next`. Any hit → `truncate`.
- `recursion_limit` is passed in the invoke config and set **above** the worst
  case the configured caps allow (each supervisor turn costs ~2 graph steps:
  supervisor + specialist, so `recursion_limit >= 2*max_iterations + slack`).
  Default suggestion: `recursion_limit = settings.recursion_limit` defaulting to
  e.g. `2 * max_iterations + 5`. The invariant under test: a deliberately-looping
  supervisor exits via `truncate` **before** `recursion_limit` is reached, so
  `GraphRecursionError` is never raised. See the looping-supervisor test in
  [`testing.md`](testing.md).

`depth` increments only when a node delegates to a *sub*-agent (a nested
`get_graph().invoke(...)` or a recursive supervisor turn that opens a new
sub-scope); the proof slice's flat specialist loop primarily exercises
`iterations`. The `max_depth` cap is wired and tested via a synthetic node that
increments `depth`, so the guard is real even though the shipped specialists are
flat. (Stated as an open item in the README: which real specialist first needs
true sub-delegation.)

## 6. Trace continuity across resume

On a fresh-process resume, the original run span no longer exists in memory. Two
acceptable outcomes (acceptance criterion allows either):

1. **Restore parent context** — serialise the OTel `SpanContext`
   (`trace_id`/`span_id`/`trace_flags`) into checkpoint state on first run, and on
   resume create resumed node spans as children of a `NonRecordingSpan` built from
   it. Cleanest, but couples state to OTel internals.
2. **Linked new trace** — start a new run span on resume and attach a `Link` to
   the stored original `trace_id`, plus a `resumed_from` attribute. Simpler, no
   coupling; the two traces are queryable as related.

Recommendation: ship (2) for the proof slice (a `resumed_from` attribute + link is
cheap and honest), and note (1) as a follow-up. Either way, **within a single
process run**, per-node spans nest under the run span automatically because
`span()` uses `start_as_current_span` and the node bodies run inside the
`agent.run` context. The test asserts nesting within a run and the
link/attribute across a resume. See [`examples/observability_resume.py`](examples/observability_resume.py).

## 7. Resume edge cases

- **Resume of an already-completed thread** → `get_state` shows it ended at `END`;
  `invoke(None, config)` is a no-op that returns the stored final state. Return it
  as-is (idempotent), don't re-run.
- **Resume with no checkpoint for `thread_id`** → treat as a fresh run (or raise a
  clear error). Decide and test; default: treat as fresh, since `thread_id` may be
  caller-supplied.
- **Non-idempotent node replay** — resuming re-enters the *interrupted* node. The
  proof slice keeps all nodes pure (retrieve/research/synthesize/truncate). Any
  future side-effecting node must be guarded by a state flag (see README risks).
- **Saver connection lifecycle** — `PostgresSaver` holds a live psycopg
  connection. The eval loop runs `ask()` sequentially in one process, so a single
  cached connection guarded for sequential use is acceptable for this slice; a
  `ConnectionPool` and an explicit close-on-shutdown are the path if runs go
  concurrent (open question in README). The lazy `get_graph()` caches one
  graph+saver per process.

## 8. Config defaults (proposed)

| Setting | Env var | Default | Notes |
| --- | --- | --- | --- |
| `orchestration_mode` | `ORCHESTRATION_MODE` | `single` | `single`/`multi` |
| `max_depth` | `MAX_DEPTH` | `2` | delegation depth ceiling |
| `max_iterations` | `MAX_ITERATIONS` | `6` | supervisor turns ceiling |
| `token_budget` | `TOKEN_BUDGET` | `20000` | per-run total tokens; 0 = unlimited |
| `recursion_limit` | `RECURSION_LIMIT` | `2*max_iterations+5` | LangGraph backstop, > cap ceiling |

All defaults keep today's behaviour: with `orchestration_mode=single` none of the
others are consulted.

**Import-time, frozen config — implications.** `app.config` builds a single frozen
`settings = Settings()` at import, and each field default captures `os.environ.get(...)`
**at class-definition time**. So changing an env var after import does not change
`settings` (and neither does constructing a new `Settings()`). Two practical
consequences:

- **Runtime:** `ORCHESTRATION_MODE` (and the caps) must be set in the environment
  **before** the process starts — i.e. compose/CI env, not flipped mid-process. This
  is exactly what makes `single` the safe default and the flip a deploy-time decision.
- **Tests:** drive config in-process by swapping the module global with a copy
  (`dataclasses.replace`), or use a fresh interpreter (subprocess) for env-driven
  cases. `monkeypatch.setenv` is a no-op against this pattern. See `testing.md` and
  `examples/test_orchestration.py::_use_settings`.

`recursion_limit` is implemented as a `@property` (so it can derive from
`max_iterations` and honour a `RECURSION_LIMIT` override read at call time) rather
than a plain frozen field; the README's config table lists it among the additions for
brevity, but it is a derived property, not a captured-at-import default.
