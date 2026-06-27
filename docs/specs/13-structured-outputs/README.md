---
title: Structured outputs
slug: structured-outputs
area: orchestration
tier: Later
size: S
status: Backlog
depends_on: []   # no hard dep; overlaps with 09-guardrails (output validation) — coordinate, don't block
issue:        # set to the GitHub issue number when created
---

# Structured outputs

> **Area** `orchestration` · **Tier** `Later` · **Size** `S` · **Status** `Backlog` · **Depends on:** —

## Summary

Add a schema-constrained model-call path through the gateway so that call sites
which consume *machine-readable* output (today: the LLM-as-judge in `app/evals.py`)
get a validated, typed object instead of a string they must parse by hand. The
client-side schema validation — not the provider — is the source of truth: an
output that does not match the schema is raised as a typed error, never silently
returned as text. The first concrete win is killing the brittle `float(...)` parse
in `judge_score`, where a malformed reply currently collapses to `0.0` and silently
corrupts the eval gate.

## Problem / Motivation

Anywhere the app parses model output, string parsing is brittle. The live example
is `app/evals.py::judge_score`:

```python
verdict = chat([...])                 # free-text reply, e.g. "0.8" — or "I'd say 0.8/1.0"
try:
    return max(0.0, min(1.0, float(verdict.strip().split()[0])))
except (ValueError, IndexError):
    return 0.0                         # a parse failure looks identical to a 0-quality answer
```

A judge that wraps the number in prose, or refuses, scores `0.0` — which drags the
mean down and can **fail the CI gate for an infrastructure reason, not a quality
regression**. Every future machine-readable call site (extractors, routers, tool
arg synthesis) inherits the same fragility. Structured outputs move the contract
from "hope the string parses" to "validate against a schema or raise."

## Goals

- A schema-constrained call path through the **gateway seam** (`app/gateway.py`),
  provider-agnostic, that returns a validated Python object.
- Client-side validation is authoritative: output that fails the schema raises a
  typed error; it is **never** returned as unvalidated text.
- Convert at least one real call site (`judge_score`) to it, and make a
  validation/infra failure *distinguishable* from a low-quality answer in the eval
  report (so the CI gate isn't poisoned by parse errors).

## Non-goals

- Replacing all free-text responses (the agent's prose answer in `generate_node`
  stays free text for now).
- Building a general guardrails/output-policy framework — that is
  [09-guardrails](../09-guardrails/README.md); this spec is only typed extraction.
- Provider-native "strict tool calling" beyond what `response_format` gives us.

## Proposed design

> **Companion docs (this directory):** [`design.md`](design.md) — alternatives
> considered, the failure/retry sequence diagram, edge cases;
> [`examples/`](examples/) — illustrative code for every touched file
> (`app/gateway.py`, `app/evals.py`, `gateway/litellm_config.yaml`,
> `pyproject.toml`, tests); [`testing.md`](testing.md) — how each acceptance
> criterion is proven and how it gates merge.

**Architecture at a glance.**

```
 app/evals.py:judge_score ──▶ chat_structured(messages, JudgeVerdict) ─┐
                                                                        │  (new sibling of chat())
 app/agent.py:generate_node ─▶ chat(messages) ─▶ str  (unchanged) ──┐  │
                                                                    ▼  ▼
                                          app/gateway.py  (the hot-path seam)
                                                    │  OpenAI client → alias `chat`
                                                    ▼
                          gateway/litellm_config.yaml  (drop_params: true)
                                                    │
                                       provider (openai/gpt-4o-mini today)

 chat_structured: send response_format=json_schema(strict) ─▶ model_validate_json(raw)
   ok → typed instance   |   ValidationError → 1 retry → still bad → StructuredOutputError
   (validation is CLIENT-SIDE and authoritative — even a silently dropped param is caught)
```

**Seam.** Add a sibling to `chat()` in `app/gateway.py` rather than overloading it
(`chat()` returns `str`; the structured path returns a typed object — different
return contract):

```python
# Illustrative — note the retry/error handling required by the Failure contract
# below is NOT shown here; the real implementation wraps this in one bounded retry.
def chat_structured(messages: list[dict], schema: type[BaseModel], **kwargs) -> BaseModel:
    resp = _client.chat.completions.create(
        model=settings.chat_model,
        messages=messages,
        response_format={                       # OpenAI-compatible; LiteLLM forwards it
            "type": "json_schema",
            "json_schema": {"name": schema.__name__,
                            "schema": _strict_schema(schema),   # see "strict-mode schema" below
                            "strict": True},
        },
        **kwargs,
    )
    raw = resp.choices[0].message.content or ""
    return schema.model_validate_json(raw)       # <-- authoritative validation; raises on mismatch
```

- **Strict-mode schema requirement (load-bearing).** OpenAI `strict: true`
  rejects a schema unless **every** object sets `additionalProperties: false` and
  lists all of its properties in `required`. pydantic's default
  `model_json_schema()` emits neither (it also adds a `title` key). So the schema
  must be post-processed before sending: set `additionalProperties: false`, mark
  all fields required, and (optionally) strip `title`. Implement this as a small
  `_strict_schema(schema)` helper, or define schemas with
  `model_config = ConfigDict(extra="forbid")` and inject `additionalProperties`
  per object. If the post-processing is skipped the **provider** errors out
  (a 400, not a validation error) — distinct from the dropped-param path below.
  This is the second reason to keep a `json_object` fallback (see Open questions).

- **Validation library.** Use **pydantic v2** (`model_validate_json`,
  `model_json_schema` are v2 APIs; the resolved version is `2.13.4`). It is already
  resolved transitively in `uv.lock` (via langgraph) but **not** a direct
  dependency — add `pydantic>=2` to `[project].dependencies` in `pyproject.toml`
  so it's declared, not accidental. Pin `>=2` explicitly so a future resolver
  can't drop to a v1 line that lacks these APIs.
- **Gateway config interaction (load-bearing).** `gateway/litellm_config.yaml`
  sets `drop_params: true`. For a provider/model that does not support
  `response_format`, LiteLLM will **silently drop the param** and return ordinary
  free text — the request *succeeds*, so there is no transport error to catch.
  This is exactly why validation must be **client-side and mandatory**: even when
  the param is honored we re-validate; when it's dropped, `model_validate_json`
  raises on the unconstrained text. Document this in the config comment; do not
  rely on the gateway to enforce the schema.
- **Failure contract.** On a validation error: do **one** bounded retry, then if
  it still fails raise a typed `StructuredOutputError` that wraps the underlying
  `ValidationError` and the raw text. Callers decide policy. Note the retry means
  a stub in tests will see the `create` call invoked **twice** on the failure path.
  - **Retry message shape (load-bearing, cross-provider).** The corrective turn is
    appended as a `user` turn, **not** a second `system` turn (some providers only
    honor the first system message). But the existing messages already *end* with a
    `user` turn (`judge_score` sends `[system, user]`), so naively appending a
    second `user` turn produces two consecutive `user` turns. OpenAI tolerates that;
    **Anthropic requires strictly alternating user/assistant turns** and the gateway
    config explicitly advertises swapping the `chat` alias to Anthropic as "the
    seam" — so the retry must not assume OpenAI's leniency. Therefore the retry
    appends **two** turns: first the model's raw (rejected) reply as an `assistant`
    turn, then the corrective `user` turn ("return JSON matching the schema, nothing
    else"). This both preserves role alternation across providers **and** gives the
    model its own prior mistake as context, which is a strictly better correction
    than a bare re-ask. `_strict_schema(schema)` is re-evaluated once per attempt
    (cheap; the result is identical).
- **First call site & gate plumbing (load-bearing).** Today `run()` computes
  `score = 0.5*keyword + 0.5*judge_score(...)` inline, so `judge_score` cannot
  itself record a per-case error — it only returns a float. The refactor:
  - **`JudgeVerdict` shape + range decision (load-bearing).** Define
    `class JudgeVerdict(BaseModel): score: float` with `extra="forbid"`. Decide
    range handling explicitly: do **not** constrain with `Field(ge=0, le=1)` —
    keep the existing `max(0.0, min(1.0, verdict.score))` clamp in `judge_score`
    instead. Rationale: a judge that says `5` or `1.0001` is a *scale slip*, not a
    structural failure; clamping preserves today's tolerant behavior, whereas a
    schema range bound would turn it into an `eval_error` and flip the gate red
    for a benign overshoot. (Schema validation still catches the real failure —
    prose, refusal, missing field.) Record this as the chosen semantics so the
    implementer doesn't silently add a range bound.
  - `judge_score` calls `chat_structured(..., JudgeVerdict)`, applies the clamp
    above, and lets `StructuredOutputError` propagate (it no longer swallows
    failures into `0.0`).
  - `run()` wraps the per-case scoring in `try/except StructuredOutputError`. On
    error it sets the case's `error` field and **does not** compute a numeric
    score for that case.
  - **`mean_score` is computed over evaluated (non-errored) cases only**, so a
    broken judge does not drag quality down (the original bug). Guard the empty
    denominator: if *every* case errored, `mean_score` is `None`, not `0.0`.
  - **Gate semantics:** `passed` is `False` if any case errored **or** the mean of
    evaluated cases is below `THRESHOLD`. The report distinguishes the two via a
    `gate_status` of `"quality_fail"` vs `"eval_error"` (or `"pass"`), and the
    CLI/exit path prints which. **Precedence (load-bearing):** if a run has *both* an
    errored case *and* a sub-threshold mean of the remaining cases, `gate_status` is
    `"eval_error"` — the infra failure is reported first because it means the
    quality number is computed over an incomplete set and can't be trusted as the
    reason. (`passed` is `False` either way; only the label's precedence is fixed
    here so two implementers don't disagree.) The gate still goes red on infra
    failure — that is intentional (fail loud) — but the report now says *why*, which
    is the whole point: a parse failure is no longer indistinguishable from a
    quality drop.
  - Per-case result shape: `{question, score: float | None, error: str | None,
    passed: bool}`.
  - **`None`-safety in the CLI/format path (load-bearing).** Once `score` and
    `mean_score` can be `None`, the existing `__main__` block breaks: it does
    `f"{r['score']:.2f}"` and `f"{report['mean_score']:.2f}"` (a `:.2f` on `None`
    raises `TypeError`) and the per-case `passed = score >= THRESHOLD` (a
    `None >= float` raises `TypeError` in Python 3). The refactor must guard all
    three: errored cases print their `error`/`"ERR"` instead of a formatted
    score, `passed` for an errored case is `False` without comparing `None`, and
    the summary line tolerates `mean_score is None`. Otherwise a single judge
    failure crashes the CLI instead of producing the labeled `eval_error` report
    that is the whole point of this spec.

## Acceptance criteria

- [ ] `app/gateway.py` exposes a structured call path that takes a pydantic schema
      and returns a validated instance; it routes through the existing gateway
      client (no provider named in app code).
- [ ] Output that does not match the schema raises a typed `StructuredOutputError`
      after one bounded retry — it is a caught, typed error, **never** a returned
      string or a silent default.
- [ ] The corrective retry preserves role alternation: it appends the model's
      rejected reply as an `assistant` turn followed by a `user` corrective turn (no
      two consecutive `user` turns and no second `system` turn), so the request is
      valid under both the OpenAI and the advertised Anthropic mapping of the `chat`
      alias. A unit test inspects the messages passed to the second `create` call
      and asserts the roles alternate and the last turn is the `user` corrective.
- [ ] Behavior is correct even when the provider does **not** honor
      `response_format` (param dropped by `drop_params`): client-side validation
      still rejects the free-text output. Covered by a test that stubs the gateway
      to return non-conforming text and asserts the typed error.
- [ ] The schema sent under `strict: true` carries `additionalProperties: false`
      and all fields in `required` (via `_strict_schema()` or `extra="forbid"`),
      so the provider does not 400 on a bare pydantic schema. A unit test asserts
      the generated schema has these properties.
- [ ] `judge_score` uses the structured path and no longer returns `0.0` on
      failure; a malformed/refused judge reply propagates and `run()` records it as
      a per-case `error`.
- [ ] `mean_score` is computed over **evaluated (non-errored) cases only**; an
      errored case never contributes a numeric score. If all cases error,
      `mean_score` is `None` (no divide-by-zero, no silent `0.0`).
- [ ] The CLI/format path tolerates `None`: an errored case and an all-errored
      `mean_score` print a label (e.g. `ERR` / the error string), never crash on
      a `:.2f` format or a `None >= THRESHOLD` comparison. A test exercises the
      `__main__`/report-print path with at least one errored case.
- [ ] The gate fails (`passed == False`, non-zero exit) when any case errored, and
      the report distinguishes `"eval_error"` from `"quality_fail"` so the failure
      reason is unambiguous in CI output.
- [ ] `pydantic>=2` is declared in `pyproject.toml` `[project].dependencies`.
- [ ] The **new** unit tests run with **no network/API key** by stubbing the
      gateway seam (schema-valid case, schema-invalid case, dropped-param case).
      The schema-invalid test accounts for the retry (stub `create` invoked
      twice). `uv run pytest` collecting only these tests passes offline.
- [ ] The **existing** `tests/test_evals.py::test_quality_gate` calls `run()`
      live and today requires a reachable gateway + key. The refactor must not
      change that quietly: either (a) guard it to `pytest.skip()` when no gateway
      / key is configured, or (b) refactor it to stub the seam like the new
      tests. Pick one and state it — do **not** leave a suite that claims
      "offline" but silently needs network. Acceptance: `uv run pytest` is green
      offline (skips clearly reported, no errors) **and** green online.
- [ ] `gateway/litellm_config.yaml` documents the `drop_params` ↔ `response_format`
      interaction in a comment.

## Dependencies

- None hard. Add direct dep on `pydantic` (already transitively present).
- Overlaps with [09-guardrails](../09-guardrails/README.md) (output validation at
  the seam) — coordinate naming/seam so the two don't diverge; not a blocker.

## Open questions

- Does the pinned LiteLLM proxy version forward `{"type":"json_schema"}` for the
  configured chat model (gpt-4o-mini), or only the older `{"type":"json_object"}`?
  Verify against the running gateway before committing the request shape; fall back
  to `json_object` + pydantic validation if `json_schema` strict isn't forwarded.
- Bounded-retry default = 1. Accepted unless verification shows judges fail more
  often; revisit if so.
- **Empty-content retry edge (Low / handled).** A refusal can return *empty*
  content. The corrective retry re-appends the rejected reply as an `assistant`
  turn; an empty assistant turn is a 400 on Anthropic (the advertised alias swap),
  which would defeat the very cross-provider alternation this retry buys. The
  implementation substitutes a placeholder (`last_raw or "(empty response)"`, see
  `examples/example_gateway.py`) so the corrective turn stays valid. Recorded so an
  implementer doesn't drop the guard.
- **`**kwargs` collision (Low / accepted).** `chat_structured(messages, schema,
  **kwargs)` forwards `**kwargs` to `create`. A caller that passes `model=` or
  `response_format=` in `kwargs` would hit a duplicate-keyword `TypeError`. The
  single v1 call site (`judge_score`) passes no such kwargs; accepted for v1 rather
  than adding guard code with no caller to exercise it.
- **`_strict_schema` scope (Low / accepted for v1).** The only schema in v1
  (`JudgeVerdict`) is flat, so the helper need only set `additionalProperties:
  false` + `required` on the top-level object and strip `title`. Nested models
  emit `$ref`/`$defs`, which would need recursive post-processing; that is **out
  of scope for v1** and the helper should either handle the flat case correctly
  or raise a clear error if handed a schema with `$defs`, rather than silently
  emit something the provider rejects with a 400.
- **Accepted risk:** making any errored case flip the gate to red means a flaky
  judge can block merges even when answer quality is fine. This is deliberate
  (fail loud, labeled `eval_error`), but if judge flakiness proves common the
  bounded retry (above) is the first lever, and a "tolerate N eval errors" knob is
  the documented escape hatch — out of scope for v1.
- **Accepted risk (scope of the `run()` try/except):** `run()` wraps only
  `StructuredOutputError` (the judge path) per-case. Any *other* exception — e.g.
  `ask()` (the agent call) raising on a network/gateway outage, or a JSON decode
  error on a malformed golden line — still propagates and aborts the whole run.
  That is acceptable and unchanged from today: those are not the parse-vs-quality
  ambiguity this spec exists to fix, and a missing gateway *should* fail the run
  loudly rather than be silently labeled an `eval_error`. Broadening per-case error
  capture to all exceptions is out of scope for v1; if added later it must keep the
  `eval_error` vs `quality_fail` distinction meaningful (an agent outage is neither).

## Risks & mitigations

- **Silent param drop → unvalidated output (highest risk).** `drop_params: true`
  means a non-supporting provider returns free text with no error. *Mitigation:*
  client-side pydantic validation is mandatory and authoritative; tested explicitly
  with a stub that returns non-conforming text.
- **Eval gate poisoned by infra errors.** A judge failure scoring `0.0` fails CI
  for the wrong reason. *Mitigation:* the new `error` field separates infra failure
  from quality; gate logic treats them distinctly.
- **Strict schema raises refusal/latency.** `strict: true` can increase refusals or
  latency on some models. *Mitigation:* keep the schema minimal (single `score`
  field), one retry, and verify against the live gateway during implementation.
- **Accepted risk:** only one call site is converted; the agent's prose answer
  stays free-text. Out of scope by design (see Non-goals).

## Test & rollout plan

- **Unit (no network):** stub the gateway client at the seam; assert (a) valid JSON
  → typed instance, (b) invalid JSON → `StructuredOutputError` after retry, (c)
  dropped-param free text → same typed error. These are the proof the schema, not
  the provider, is authoritative.
- **Integration (opt-in, needs key):** run `judge_score` against the live gateway
  for the configured model; confirm `json_schema` is forwarded (the open question).
- **Eval gate:** the existing `tests/test_evals.py::test_quality_gate` runs `run()`
  end-to-end and **needs a live gateway + key** — it is not offline-safe today.
  Reconcile it per the acceptance criterion (skip-without-key *or* stub the seam)
  so the suite isn't silently network-bound. Then add a test that
  forces a `StructuredOutputError` from the judge (stub the seam) and asserts the
  case shows `error` set, `score` is `None`, the case is excluded from
  `mean_score`, and the report's status is `"eval_error"` (not a `0.0` quality
  fail). Add a guard test for the all-cases-errored case (`mean_score is None`,
  no divide-by-zero).
- **Rollout:** no migration. Purely additive (new function + one call-site swap +
  one dependency line + a config comment). No feature flag needed; revertible by
  pointing `judge_score` back at `chat()`.

## References

- [Feature roadmap](../../ROADMAP.md)
- [Specs index](../README.md)
- Companion design notes: [`design.md`](design.md)
- Illustrative code: [`examples/`](examples/) ([gateway](examples/example_gateway.py),
  [evals](examples/example_evals.py), [tests](examples/example_tests.py))
- Test & verification plan: [`testing.md`](testing.md)
- Related: [09-guardrails](../09-guardrails/README.md) (output validation at the
  same seam — coordinate, don't block), [07-ci-hardening](../07-ci-hardening/README.md)
  (the CI `eval-gate` this spec's gate runs under)
- Touches: `app/gateway.py`, `app/evals.py`, `gateway/litellm_config.yaml`,
  `pyproject.toml`, `tests/test_evals.py` (+ new `tests/test_structured_outputs.py`)
