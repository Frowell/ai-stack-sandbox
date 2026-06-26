---
title: Agent orchestration (multi-node)
slug: agent-orchestration
area: orchestration
tier: Later
size: L
status: Backlog
depends_on: []
issue:        # set to the GitHub issue number when created
---

# Agent orchestration (multi-node)

> **Area** `orchestration` · **Tier** `Later` · **Size** `L` · **Status** `Backlog` · **Depends on:** —

## Summary

_One-paragraph summary — expand._

## Problem / Motivation

A single `retrieve->generate` graph won't cover complex tasks; the thesis is 3-8 specialists with durable state, not one mega-agent.

## Goals

- Decompose into specialist nodes with focused prompts/tools.
- Durable checkpointed state.
- Principled subagent delegation.

## Non-goals

- A general agent-framework rewrite.

## Proposed design

_TODO — expand. Sketch the approach, the components involved, and which seam this
lives behind (gateway config / extractor interface / eval harness / CI). Note any
schema, config, or API changes._

## Acceptance criteria

- [ ] At least one multi-node workflow with checkpointed, resumable state.
- [ ] Per-node spans nested under the run in observability.
- [ ] Subagent delegation has explicit when-to-delegate rules.

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
