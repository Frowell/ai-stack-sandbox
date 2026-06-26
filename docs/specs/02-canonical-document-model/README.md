---
title: Canonical document model
slug: canonical-document-model
area: ingestion
tier: Next
size: M
status: Todo
depends_on: [PR #2]
issue:        # set to the GitHub issue number when created
---

# Canonical document model

> **Area** `ingestion` · **Tier** `Next` · **Size** `M` · **Status** `Todo` · **Depends on:** PR #2

## Summary

_One-paragraph summary — expand._

## Problem / Motivation

Extractors normalize to an *implicit* `LayoutDoc`/`Element` shape with a free-string `kind` and token-based footnotes -- fine for md/html, but it won't survive geometry-only backends (PDF) or third-party IRs.

## Goals

- Implement the v1 contract in `CANONICAL_MODEL.md`: closed `BlockType`, `schema_version`, stable block IDs, optional `Locator`, footnotes as a `footnote_ref` relation.

## Non-goals

- `caption_of`/`contains`/`cross_ref` as required output.
- Structured table cells; multi-modal blocks (deferred per the spec).

## Proposed design

_TODO — expand. Sketch the approach, the components involved, and which seam this
lives behind (gateway config / extractor interface / eval harness / CI). Note any
schema, config, or API changes._

## Acceptance criteria

- [ ] `Document`/`Block`/`Locator`/`Relation` types exist; md/html/csv extractors emit valid v1 docs (token-only fields omitted).
- [ ] Chunker attaches footnotes by walking `footnote_ref` relations, not the `[^id]` token; existing footnote tests pass.
- [ ] `schema_version` stamped and surfaced into `documents.meta`.
- [ ] Unknown-major `schema_version` is rejected, not mis-parsed.

## Dependencies

- PR #2

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
