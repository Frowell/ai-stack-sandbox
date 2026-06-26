---
title: Retrieval uses chunk metadata
slug: retrieval-uses-chunk-metadata
area: retrieval
tier: Next
size: S
status: Todo
depends_on: [PR #2]
issue:        # set to the GitHub issue number when created
---

# Retrieval uses chunk metadata

> **Area** `retrieval` · **Tier** `Next` · **Size** `S` · **Status** `Todo` · **Depends on:** PR #2

## Summary

_One-paragraph summary — expand._

## Problem / Motivation

Chunks now carry `meta` (section path, footnote ids, soon page) but retrieval returns only `id, content`, so the generate step can't cite provenance.

## Goals

- Thread `meta` through `retrieve()` into the generate node; cite section/page in answers; optionally filter by `meta` (format/section).

## Non-goals

- Re-ranking by metadata; UI.

## Proposed design

_TODO — expand. Sketch the approach, the components involved, and which seam this
lives behind (gateway config / extractor interface / eval harness / CI). Note any
schema, config, or API changes._

## Acceptance criteria

- [ ] `retrieve()` returns chunk `meta` alongside content.
- [ ] Generated answers cite section (and page when present) from `meta`.
- [ ] Existing citation behavior (the `[id]` convention) still works.

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
