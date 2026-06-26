---
title: Reranker
slug: reranker
area: retrieval
tier: Next
size: S
status: Todo
depends_on: []
issue:        # set to the GitHub issue number when created
---

# Reranker

> **Area** `retrieval` · **Tier** `Next` · **Size** `S` · **Status** `Todo` · **Depends on:** —

## Summary

_One-paragraph summary — expand._

## Problem / Motivation

`rerank()` is identity (RRF order); we claim a rerank stage but don't have one, and can't show it helps.

## Goals

- A real reranker behind the existing hook -- cross-encoder (local) or hosted (Cohere/Voyage) **through the gateway seam**; config-selectable.

## Non-goals

- Training a custom reranker.

## Proposed design

_TODO — expand. Sketch the approach, the components involved, and which seam this
lives behind (gateway config / extractor interface / eval harness / CI). Note any
schema, config, or API changes._

## Acceptance criteria

- [ ] `rerank()` reorders candidates via the chosen backend; provider is config, not code.
- [ ] The eval gate shows a measurable retrieval-quality delta vs identity (reported, not hand-asserted).
- [ ] Reranker latency/cost visible in spans.

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
- [Canonical document model](../../CANONICAL_MODEL.md)
