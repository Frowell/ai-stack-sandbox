---
title: Multi-modal ingestion
slug: multi-modal-ingestion
area: ingestion
tier: Horizon
size: L
status: Backlog
depends_on: [canonical-document-model, real-layout-backends]
issue:        # set to the GitHub issue number when created
---

# Multi-modal ingestion

> **Area** `ingestion` · **Tier** `Horizon` · **Size** `L` · **Status** `Backlog` · **Depends on:** [canonical-document-model](../02-canonical-document-model/README.md), [real-layout-backends](../03-real-layout-backends/README.md)

## Summary

_One-paragraph summary — expand._

## Problem / Motivation

Ingestion and retrieval are text-only; real corpora include images and audio.

## Goals

- Image/audio blocks in the canonical model + ingestion router.
- High-res vision in extraction/retrieval.

## Non-goals

- Audio transcription quality tuning (use a provider).

## Proposed design

_TODO — expand. Sketch the approach, the components involved, and which seam this
lives behind (gateway config / extractor interface / eval harness / CI). Note any
schema, config, or API changes._

## Acceptance criteria

- [ ] An image-bearing document ingests into canonical `figure`/image blocks.
- [ ] Retrieval can return and the agent can reason over a non-text chunk (demoed).

## Dependencies

- [canonical-document-model](../02-canonical-document-model/README.md)
- [real-layout-backends](../03-real-layout-backends/README.md)

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
- [Canonical document model](../../CANONICAL_MODEL.md)
