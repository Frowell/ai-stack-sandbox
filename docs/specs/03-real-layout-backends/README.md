---
title: Real layout backends
slug: real-layout-backends
area: ingestion
tier: Next
size: L
status: Todo
depends_on: [canonical-document-model]
issue:        # set to the GitHub issue number when created
---

# Real layout backends

> **Area** `ingestion` · **Tier** `Next` · **Size** `L` · **Status** `Todo` · **Depends on:** [canonical-document-model](../02-canonical-document-model/README.md)

## Summary

_One-paragraph summary — expand._

## Problem / Motivation

PDF/DOCX/XLSX are register-a-backend stubs today; the most common real-world layout/footnote sources can't actually be ingested.

## Goals

- `pymupdf` (PDF -- incl. page/bbox locators and geometry-based footnote linkage).
- `python-docx` (DOCX -- native footnotes/headings).
- `openpyxl` (XLSX -- records).
- `docling` as a heavier opt-in adapter (documented, not pinned).
- One golden fixture + extractor test per format.

## Non-goals

- OCR for scanned PDFs.
- Multi-column reading-order reconstruction.

## Proposed design

_TODO — expand. Sketch the approach, the components involved, and which seam this
lives behind (gateway config / extractor interface / eval harness / CI). Note any
schema, config, or API changes._

## Acceptance criteria

- [ ] Each backend produces a valid canonical `Document`; `register()` wires it with no router/chunker changes.
- [ ] A footnoted PDF round-trips with the footnote attached to its citing chunk via relation (no inline token present).
- [ ] Per-format tests in CI; heavy deps stay out of the default lock.

## Dependencies

- [canonical-document-model](../02-canonical-document-model/README.md)

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
