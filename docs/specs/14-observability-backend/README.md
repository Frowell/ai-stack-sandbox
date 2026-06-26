---
title: Observability backend
slug: observability-backend
area: observability
tier: Horizon
size: M
status: Backlog
depends_on: []
issue:        # set to the GitHub issue number when created
---

# Observability backend

> **Area** `observability` · **Tier** `Horizon` · **Size** `M` · **Status** `Backlog` · **Depends on:** —

## Summary

_One-paragraph summary — expand._

## Problem / Motivation

Spans are emitted but go nowhere by default; evals and traces aren't linked.

## Goals

- Wire a real backend (Langfuse/Phoenix/Braintrust) via OTLP.
- Link traces to eval results.
- Sample online evals from live traffic.

## Non-goals

- Building a custom tracing UI.

## Proposed design

_TODO — expand. Sketch the approach, the components involved, and which seam this
lives behind (gateway config / extractor interface / eval harness / CI). Note any
schema, config, or API changes._

## Acceptance criteria

- [ ] Traces visible in a backend by setting an env var only (no code change).
- [ ] An eval result is navigable from its trace.

## Dependencies

- None

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
