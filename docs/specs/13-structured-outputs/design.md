# Structured outputs — design notes

> Companion to [`README.md`](README.md). This file holds the *deeper* design:
> alternatives considered and rejected, interface sketches, a sequence diagram of
> the failure/retry path, and the edge cases that drove the load-bearing
> decisions. Illustrative code lives in [`examples/`](examples/); the test plan in
> [`testing.md`](testing.md). **None of this is wired-in code** — it is a spec.

## 1. Where the feature lives (the seam)

```
                    app/agent.py            app/evals.py
                     generate_node           judge_score        <- call sites
                         |                       |
                  chat() : str          chat_structured(): BaseModel   <- NEW sibling
                         \                      /
                          \                    /
                           app/gateway.py  (the hot-path seam)
                                   |
                          OpenAI() client -> settings.gateway_base_url
                                   |
                    gateway/litellm_config.yaml  (LiteLLM proxy, drop_params: true)
                                   |
                        provider (openai/gpt-4o-mini today)
```

`chat_structured()` sits **beside** `chat()`, not inside it. Both speak the same
OpenAI-compatible protocol to the gateway and name an *alias* (`settings.chat_model`),
never a provider — preserving the seam invariant stated at the top of
`app/gateway.py`. The only thing that differs is the return contract:

| function           | sends                                   | returns        | on bad output        |
| ------------------ | --------------------------------------- | -------------- | -------------------- |
| `chat()`           | messages                                | `str`          | returns the string   |
| `chat_structured()`| messages + `response_format` json_schema| `BaseModel`    | retries once, then raises `StructuredOutputError` |

## 2. Why a sibling and not an overload of `chat()`

**Rejected: add a `schema=...` kwarg to `chat()` and branch the return type.**
`chat()` is annotated `-> str` and every existing caller (`generate_node`,
`judge_score`) relies on that. A union return (`str | BaseModel`) pushes an
`isinstance` check onto every call site and defeats the type checker exactly where
we are trying to *add* type safety. A second function with a distinct signature
keeps `chat()`'s contract intact and makes the typed path opt-in and greppable.

**Rejected: a generic `chat_json()` that returns `dict`.** Returning a parsed
dict still leaves every caller to validate keys/types by hand — the same class of
bug as `float(...)`, just one layer up. Returning a *validated pydantic instance*
is the point: the schema is the contract and validation is mandatory.

## 3. Validation is client-side and authoritative (the core invariant)

The single most important property, restated from the README because it drives
the whole design: **the gateway is not trusted to enforce the schema.** Two
distinct ways the provider can hand us non-conforming text *without* any transport
error:

1. **`drop_params: true`** (`gateway/litellm_config.yaml`). If the served model
   does not support `response_format`, LiteLLM silently drops the param and
   returns ordinary free text. The HTTP call **succeeds (200)**. There is nothing
   to `except`.
2. **A model that accepts the param but still emits prose** (wraps the number,
   refuses, adds a markdown fence).

In both cases the only thing standing between us and a corrupt value is
`schema.model_validate_json(raw)` running *in our process*. That is why the design
forbids ever returning the raw string from `chat_structured()`: a path that
returns text on validation failure would silently reintroduce the `0.0` bug one
level down.

## 4. Strict-mode schema post-processing (`_strict_schema`)

OpenAI `strict: true` rejects (HTTP 400) any schema where an object omits
`additionalProperties: false` or leaves a property out of `required`. pydantic v2's
`model_json_schema()` emits **neither**, and adds a `title` key. So the raw
pydantic schema must be transformed before it goes on the wire.

```
JudgeVerdict.model_json_schema()              _strict_schema(JudgeVerdict)
------------------------------------          ----------------------------------------
{                                             {
  "title": "JudgeVerdict",          ->          "type": "object",
  "type": "object",                             "properties": {"score": {"type": "number"}},
  "properties": {                               "required": ["score"],
    "score": {"title": "Score",                 "additionalProperties": false
              "type": "number"}               }
  },                                          # title stripped, required filled,
  "required": ["score"]   # already here on    # additionalProperties forced false
                          # a required field
}
```

Note `score` (no default) is already in `required`; the helper still **forces**
all properties into `required` to be correct for fields that pydantic would treat
as optional, and to be robust to schema authors who add a default later.

### Scope decision: flat-only for v1 (load-bearing)

`JudgeVerdict` is flat (one scalar field), so the helper only needs to handle the
top-level object. A **nested** model emits `$ref`/`$defs`, which would need
recursive descent into every `$defs` entry. Two honest options:

- **Chosen for v1:** handle the flat case; if `_strict_schema` is handed a schema
  containing `$defs`, **raise a clear error** (`ValueError("nested schemas not
  supported in v1; see spec 13")`) rather than emit something the provider 400s
  on. This fails loud at the developer, not at runtime in CI.
- **Rejected for v1:** full recursive post-processing. More code than the single
  v1 call site justifies; deferred until a second, nested call site exists.

See [`examples/example_gateway.py`](examples/example_gateway.py) for the sketch.

## 5. Failure / retry sequence

```
chat_structured(messages, JudgeVerdict)
   |
   | attempt 1: create(response_format=json_schema strict)
   v
 raw = choices[0].message.content
   |
   |-- model_validate_json(raw) -- OK -----------------> return JudgeVerdict(...)
   |
   '-- ValidationError
            |
            | append the rejected reply as an *assistant* turn, then a
            |   *user* corrective turn: "Return JSON matching the schema,
            |   nothing else."  (NOT a 2nd system turn — some providers honor
            |   only the first; NOT a 2nd consecutive user turn — Anthropic,
            |   the advertised alias swap, rejects those)
            v
        attempt 2: create(...) again        <-- stub sees create() called TWICE
            |
            |-- model_validate_json(raw2) -- OK ---------> return JudgeVerdict(...)
            |
            '-- ValidationError
                     |
                     v
            raise StructuredOutputError(raw=raw2, cause=ValidationError)
```

Edge cases the diagram encodes:

- **Retry count = 1** (bounded). One corrective round-trip, then give up. A
  retry loop that never terminates would turn a stubborn judge into a hang in CI.
- **Corrective turn is appended as `user`, not `system`.** Documented gotcha:
  some providers only honor the first system message, so a second system turn can
  be silently ignored.
- **Role alternation is preserved across providers.** The existing messages end
  with a `user` turn (`judge_score` sends `[system, user]`), so the retry must not
  append a *bare* second `user` turn: OpenAI tolerates consecutive same-role turns
  but **Anthropic rejects them**, and `gateway/litellm_config.yaml` explicitly
  advertises swapping the `chat` alias to Anthropic as "the seam." So the retry
  appends two turns — the model's rejected reply as an `assistant` turn, then the
  `user` corrective — yielding `[system, user, assistant, user]`, valid under both
  mappings, and giving the model its own mistake as context.
- **The stub-sees-`create`-twice fact** is a *test* consequence the schema-invalid
  test must account for (assert call count / side_effect with two entries). Called
  out so the test author doesn't write a single-return mock and get a confusing
  `StopIteration`.
- **Empty assistant content on retry.** A refusal can return *empty* content; the
  re-appended `assistant` turn would then be empty, which Anthropic (the advertised
  alias swap) rejects with a 400 — defeating the cross-provider alternation this
  retry exists to preserve. The implementation substitutes a placeholder
  (`last_raw or "(empty response)"`); see `examples/example_gateway.py`.

## 6. `StructuredOutputError` shape

A typed exception that carries enough context to debug a CI failure from the log
alone:

- `raw: str` — the final non-conforming model output (so the report can show what
  the judge actually said).
- `__cause__` — the underlying pydantic `ValidationError` (chained via
  `raise ... from e`).
- `schema_name: str` — `schema.__name__`, so the message names the contract that
  was violated.

It lives in `app/gateway.py` next to `chat_structured` (same module that raises
it). Callers `import` it from there. This keeps the seam self-contained and gives
spec 09-guardrails a single place to align on a shared error vocabulary later.

## 7. The eval-gate refactor (why `judge_score` alone is not enough)

Today scoring is inlined in `run()`:

```python
score = 0.5 * keyword_score(...) + 0.5 * judge_score(...)   # one float, no per-case error channel
```

`judge_score` can only *return a float*, so it cannot record "I failed for an
infra reason." The README spells out the full refactor; the design rationale for
each piece:

- **`mean_score` over evaluated cases only.** This is the literal fix for the
  original bug: a judge that fails must not contribute a `0.0` that drags the mean
  under `THRESHOLD` and fails CI for the wrong reason. Errored cases are *excluded
  from the denominator*, not scored zero.
- **Empty-denominator guard.** If every case errors, `mean_score` is `None` (not
  `0.0`, which would be a silent "quality is terrible" lie, and not a
  `ZeroDivisionError`).
- **`gate_status` distinguishes `eval_error` from `quality_fail`.** The gate still
  goes red on infra failure — *fail loud* — but the report now says **why**. That
  distinction is the entire point of the spec: a parse failure is no longer
  indistinguishable from a quality drop.
- **`None`-safety in the `__main__`/format path.** Once `score` and `mean_score`
  can be `None`, the current `f"{...:.2f}"` formatting and `score >= THRESHOLD`
  comparison raise `TypeError`. The CLI must guard all three sites (per-case
  score, per-case `passed`, summary line) or a single judge failure *crashes the
  CLI* instead of producing the labeled `eval_error` report. See
  [`examples/example_evals.py`](examples/example_evals.py).

### Range handling: clamp, don't constrain (load-bearing)

`JudgeVerdict.score` is **not** bounded with `Field(ge=0, le=1)`. A judge that
says `5` or `1.0001` is a *scale slip*, not a structural failure. Keeping the
existing `max(0.0, min(1.0, verdict.score))` clamp preserves today's tolerant
behavior; a schema range bound would turn a benign overshoot into an `eval_error`
and flip the gate red. Schema validation still catches the *real* failures —
prose, refusal, missing field, wrong type. This is a deliberate semantic choice,
recorded so an implementer doesn't "tidy up" by adding a range bound.

## 8. Alternatives considered (summary table)

| Alternative                                   | Why rejected for v1 |
| --------------------------------------------- | ------------------- |
| Overload `chat()` with a `schema` kwarg       | breaks `-> str` contract; union return spreads `isinstance` to all callers (§2) |
| Return parsed `dict` instead of `BaseModel`   | leaves per-key validation to callers — same bug class (§2) |
| Trust the gateway / `response_format` to enforce | `drop_params: true` makes silent free-text a *successful* response (§3) |
| `Field(ge=0, le=1)` on the score              | turns a benign scale slip into a red gate (§7) |
| Recursive `_strict_schema` for nested models  | more code than the single flat v1 call site needs; raise-on-`$defs` instead (§4) |
| Unbounded retry until valid                   | a stubborn judge becomes a CI hang; bound to 1 (§5) |
| Keep `json_object` only (no `json_schema`)    | weaker provider-side hinting; but kept as the documented fallback if the proxy doesn't forward `json_schema` (README open question) |

## 9. Open design questions (carried from README)

- Does the pinned LiteLLM proxy forward `{"type":"json_schema"}` for
  `openai/gpt-4o-mini`, or only `{"type":"json_object"}`? Verify against the
  running gateway; fall back to `json_object` + the *same* client-side validation
  if not. Either way §3 holds — validation is ours.
- Is one retry enough in practice? Default 1; revisit only if live verification
  shows judges fail more often.
