---
title: Governance & audit
slug: governance-and-audit
area: governance
tier: Horizon
size: XL
status: Backlog
depends_on: [observability-backend]
issue:        # set to the GitHub issue number when created
---

# Governance & audit

> **Area** `governance` · **Tier** `Horizon` · **Size** `XL` · **Status** `Backlog` · **Depends on:** [observability-backend](../14-observability-backend/README.md)

## Summary

_One-paragraph summary — expand._

## Problem / Motivation

EU AI Act high-risk obligations apply from Aug 2026: retained, queryable traces tied to a risk classification, with human-in-the-loop gates and audit trails on high-stakes decisions. (Epic -- split when promoted.)

## Goals

- Trace retention + queryability.
- Risk classification tagging.
- HITL escalation gates.
- Audit trail on high-stakes paths.

## Non-goals

- Legal compliance sign-off (this is the technical substrate, not legal advice).

## Proposed design

_TODO — expand. Sketch the approach, the components involved, and which seam this
lives behind (gateway config / extractor interface / eval harness / CI). Note any
schema, config, or API changes._

## Acceptance criteria

- [ ] Traces are retained and queryable by risk class.
- [ ] A high-stakes decision requires and records human approval.

## Dependencies

- [observability-backend](../14-observability-backend/README.md)

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
