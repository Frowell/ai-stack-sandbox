---
title: Eval-set maturity
slug: eval-set-maturity
area: eval
tier: Next
size: M
status: Todo
depends_on: [PR #1]
issue:        # set to the GitHub issue number when created
---

# Eval-set maturity

> **Area** `eval` · **Tier** `Next` · **Size** `M` · **Status** `Todo` · **Depends on:** PR #1

## Summary

_One-paragraph summary — expand._

## Problem / Motivation

The golden set is n=4 with placeholder weights and no recorded baseline -- too thin for the weighted/per-slice gates to mean anything.

## Goals

- Grow the golden set from production traces; expand slice coverage; set real business-value weights.
- Record the first baseline (`make eval-baseline`) on a vetted run.
- Add per-slice statistical-significance handling and cost/latency budgets as gates.

## Non-goals

- Automated trace-mining pipeline (later).

## Proposed design

_TODO — expand. Sketch the approach, the components involved, and which seam this
lives behind (gateway config / extractor interface / eval harness / CI). Note any
schema, config, or API changes._

## Acceptance criteria

- [ ] >= N cases per high-value slice (define N); weights reviewed.
- [ ] `evals/baseline.json` populated; regression gate active (not skipped).
- [ ] A cost/latency budget gate fails a deliberately over-budget change.

## Dependencies

- PR #1

## Open questions

- _TODO_

## Risks & mitigations

- _TODO_

## Test & rollout plan

- _TODO — how this is verified (eval gate / unit / integration) and shipped
  (behind config? feature-flagged? migration needed?)._

## References

- [Feature roadmap](../../ROADMAP.md)
- [Specs index](../README.md)
