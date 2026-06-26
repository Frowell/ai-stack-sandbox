---
title: Structured outputs
slug: structured-outputs
area: orchestration
tier: Later
size: S
status: Backlog
depends_on: []
issue:        # set to the GitHub issue number when created
---

# Structured outputs

> **Area** `orchestration` · **Tier** `Later` · **Size** `S` · **Status** `Backlog` · **Depends on:** —

## Summary

_One-paragraph summary — expand._

## Problem / Motivation

Anywhere the app parses model output, string parsing is brittle.

## Goals

- Schema-constrained responses (structured outputs / strict tools) through the gateway where the app consumes machine-readable output.

## Non-goals

- Replacing all free-text responses.

## Proposed design

_TODO — expand. Sketch the approach, the components involved, and which seam this
lives behind (gateway config / extractor interface / eval harness / CI). Note any
schema, config, or API changes._

## Acceptance criteria

- [ ] At least one call site returns schema-validated output; invalid output is a caught error, not a parse crash.
- [ ] Works through the gateway seam (provider-agnostic).

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
