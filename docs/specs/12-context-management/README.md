---
title: Context management
slug: context-management
area: orchestration
tier: Later
size: M
status: Backlog
depends_on: [agent-orchestration]
issue:        # set to the GitHub issue number when created
---

# Context management

> **Area** `orchestration` · **Tier** `Later` · **Size** `M` · **Status** `Backlog` · **Depends on:** [agent-orchestration](../11-agent-orchestration/README.md)

## Summary

_One-paragraph summary — expand._

## Problem / Motivation

Long runs blow the context window; nothing persists across sessions.

## Goals

- Compaction / context-editing for long runs.
- Cross-session memory.

## Non-goals

- A vector-memory product.

## Proposed design

_TODO — expand. Sketch the approach, the components involved, and which seam this
lives behind (gateway config / extractor interface / eval harness / CI). Note any
schema, config, or API changes._

## Acceptance criteria

- [ ] A long run stays under the window via compaction without losing task state.
- [ ] Memory persists and is reused across two sessions (demoed).

## Dependencies

- [agent-orchestration](../11-agent-orchestration/README.md)

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
