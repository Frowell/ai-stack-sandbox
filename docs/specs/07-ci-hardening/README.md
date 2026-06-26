---
title: CI hardening
slug: ci-hardening
area: ci
tier: Next
size: S
status: Todo
depends_on: [PR #2, PR #3]
issue:        # set to the GitHub issue number when created
---

# CI hardening

> **Area** `ci` · **Tier** `Next` · **Size** `S` · **Status** `Todo` · **Depends on:** PR #2, PR #3

## Summary

_One-paragraph summary — expand._

## Problem / Motivation

CI uses `uv sync` (non-frozen) to tolerate past lock drift; gates aren't required; no dependency caching enforcement.

## Goals

- Tighten to `uv sync --frozen` (lock is now consistent).
- Make `lint` (and `eval-gate` once `OPENAI_API_KEY` is set) required via branch protection.
- Cache deps; single-source the workflow after the open PRs merge.

## Non-goals

- Multi-OS/Python matrix; release automation.

## Proposed design

_TODO — expand. Sketch the approach, the components involved, and which seam this
lives behind (gateway config / extractor interface / eval harness / CI). Note any
schema, config, or API changes._

## Acceptance criteria

- [ ] CI uses `--frozen`; a stale-lock PR fails CI.
- [ ] `lint` is a required check on `main`; `eval-gate` required once the secret exists.
- [ ] Workflow is identical across branches (no `ci.yml` merge conflicts).

## Dependencies

- PR #2
- PR #3

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
