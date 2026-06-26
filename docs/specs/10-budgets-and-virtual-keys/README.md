---
title: Budgets & virtual keys
slug: budgets-and-virtual-keys
area: gateway
tier: Later
size: M
status: Backlog
depends_on: []
issue:        # set to the GitHub issue number when created
---

# Budgets & virtual keys

> **Area** `gateway` · **Tier** `Later` · **Size** `M` · **Status** `Backlog` · **Depends on:** —

## Summary

_One-paragraph summary — expand._

## Problem / Motivation

No per-tenant cost control, rate limiting, or key management.

## Goals

- Virtual keys, per-tenant budgets and rate limits via the gateway.

## Non-goals

- Billing/invoicing.

## Proposed design

_TODO — expand. Sketch the approach, the components involved, and which seam this
lives behind (gateway config / extractor interface / eval harness / CI). Note any
schema, config, or API changes._

## Acceptance criteria

- [ ] A tenant key over budget is rejected with a clear error.
- [ ] Per-key usage/limits queryable.
- [ ] App still authenticates only to the gateway (no provider keys in app).

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
