---
title: Data residency
slug: data-residency
area: governance
tier: Horizon
size: M
status: Backlog
depends_on: [model-failover]
issue:        # set to the GitHub issue number when created
---

# Data residency

> **Area** `governance` · **Tier** `Horizon` · **Size** `M` · **Status** `Backlog` · **Depends on:** [model-failover](../01-model-failover/README.md)

## Summary

_One-paragraph summary — expand._

## Problem / Motivation

No control over where inference runs; some workloads require region pinning.

## Goals

- Inference-geo / region pinning through the gateway.
- Record where inference ran.

## Non-goals

- Building region infrastructure (rely on provider regions).

## Proposed design

_TODO — expand. Sketch the approach, the components involved, and which seam this
lives behind (gateway config / extractor interface / eval harness / CI). Note any
schema, config, or API changes._

## Acceptance criteria

- [ ] A request can be pinned to a region; the served region is recorded.

## Dependencies

- [model-failover](../01-model-failover/README.md)

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
