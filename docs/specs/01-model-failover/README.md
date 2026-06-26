---
title: Model failover / resilience
slug: model-failover
area: gateway
tier: Next
size: M
status: Todo
depends_on: []
issue:        # set to the GitHub issue number when created
---

# Model failover / resilience

> **Area** `gateway` · **Tier** `Next` · **Size** `M` · **Status** `Todo` · **Depends on:** —

## Summary

_One-paragraph summary — expand._

## Problem / Motivation

A single provider/region outage takes the app down even though the gateway seam could route around it. We have no redundancy and no visibility into which deployment served a request.

## Goals

- Same-model, multi-provider redundancy in `litellm_config.yaml` (e.g. Anthropic + Bedrock on the same model) as active/active load balancing.
- Ordered `fallbacks` for an explicit primary->standby chain.
- Surface the served model/provider in responses and metrics (`response.model`).

## Non-goals

- Cross-*model* fallback to an unvetted model (separate, deliberate decision).
- Multi-region data residency (see data-residency).

## Proposed design

_TODO — expand. Sketch the approach, the components involved, and which seam this
lives behind (gateway config / extractor interface / eval harness / CI). Note any
schema, config, or API changes._

## Acceptance criteria

- [ ] Killing the primary deployment still serves requests via the standby (demoed).
- [ ] App code unchanged -- only `litellm_config.yaml` + env differ.
- [ ] Served model is logged/observable per request; standby activation is visible in metrics, not silent.
- [ ] `drop_params` reconciles param differences across providers without 400s.

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
