---
title: Guardrails
slug: guardrails
area: gateway
tier: Later
size: M
status: Backlog
depends_on: []
issue:        # set to the GitHub issue number when created
---

# Guardrails

> **Area** `gateway` · **Tier** `Later` · **Size** `M` · **Status** `Backlog` · **Depends on:** —

## Summary

_One-paragraph summary — expand._

## Problem / Motivation

No input/output validation, PII handling, or prompt-injection defense at the seam.

## Goals

- Input/output guardrails at the gateway.
- PII detection/redaction.
- Prompt-injection defenses (operator-channel separation).

## Non-goals

- A full DLP program.

## Proposed design

_TODO — expand. Sketch the approach, the components involved, and which seam this
lives behind (gateway config / extractor interface / eval harness / CI). Note any
schema, config, or API changes._

## Acceptance criteria

- [ ] A known-bad input is blocked/flagged with a recorded reason.
- [ ] PII in inputs is redacted before provider egress (demoed).
- [ ] Guardrail decisions are observable in spans.

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
