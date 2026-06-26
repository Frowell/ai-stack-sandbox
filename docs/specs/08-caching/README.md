---
title: Caching (semantic + prompt)
slug: caching
area: gateway
tier: Later
size: M
status: Backlog
depends_on: []
issue:        # set to the GitHub issue number when created
---

# Caching (semantic + prompt)

> **Area** `gateway` · **Tier** `Later` · **Size** `M` · **Status** `Backlog` · **Depends on:** —

## Summary

_One-paragraph summary — expand._

## Problem / Motivation

Every call hits a provider; repeated/near-duplicate requests and stable prompt prefixes cost full price and latency.

## Goals

- Gateway semantic cache.
- A deliberate prompt-caching strategy (stable-prefix discipline).
- Cache-hit rate as a first-class metric.

## Non-goals

- A bespoke cache store (use the gateway/Redis already present).

## Proposed design

_TODO — expand. Sketch the approach, the components involved, and which seam this
lives behind (gateway config / extractor interface / eval harness / CI). Note any
schema, config, or API changes._

## Acceptance criteria

- [ ] Cache hit/miss and savings visible in metrics.
- [ ] A repeated query is served from cache (demoed); correctness unaffected.
- [ ] Documented invalidation rules (prefix changes bust the cache).

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
