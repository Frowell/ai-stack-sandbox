---
title: Safety & red-teaming
slug: safety-and-red-teaming
area: safety
tier: Horizon
size: L
status: Backlog
depends_on: [PR #1]
issue:        # set to the GitHub issue number when created
---

# Safety & red-teaming

> **Area** `safety` · **Tier** `Horizon` · **Size** `L` · **Status** `Backlog` · **Depends on:** PR #1

## Summary

_One-paragraph summary — expand._

## Problem / Motivation

The eval gate measures quality, not safety; no adversarial coverage or refusal handling.

## Goals

- Refusal handling with fallbacks.
- Adversarial/safety eval suites run alongside the quality gate.

## Non-goals

- A bug-bounty / external pentest program.

## Proposed design

_TODO — expand. Sketch the approach, the components involved, and which seam this
lives behind (gateway config / extractor interface / eval harness / CI). Note any
schema, config, or API changes._

## Acceptance criteria

- [ ] A safety suite runs in CI and can block a merge.
- [ ] Refusals are handled gracefully with a recorded fallback path.

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
