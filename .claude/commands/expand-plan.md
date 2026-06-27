---
description: Expand a plan/spec into a full feature directory — spec, design, code examples, acceptance criteria, tests
argument-hint: "[path to plan/spec or feature dir, or a short plan description]"
allowed-tools: Read, Grep, Glob, Write, Edit, Bash(git:*)
---

You are expanding a lightweight plan into a complete, implementation-ready feature
package laid out as its own directory.

## Input

Target: $ARGUMENTS

- If it is an existing spec file or feature directory (e.g.
  `docs/specs/<nn-slug>/`), Read it and treat its content as the seed; expand in
  place, preserving any YAML frontmatter.
- If it is a path that doesn't exist yet, or a short description, pick a slug and
  create a new directory. **Match the repo's existing convention** if there is one
  (this repo uses `docs/specs/<nn-slug>/README.md` — follow it); otherwise default
  to `docs/specs/<slug>/`.
- If empty, use the most recent plan in this conversation. If there is none, ask
  and stop.

## Ground yourself in the repo first

Before writing anything, Read enough of the codebase to make the expansion
**concrete and correct**: the seams/interfaces the feature touches, existing
patterns to match (config, tests, CI, error handling), and naming/structure
conventions. Code examples must fit **this** codebase — real signatures and file
paths, not generic pseudocode.

## Produce the directory

Create `<feature-dir>/` with the files that fit the feature (don't pad; omit one
that genuinely doesn't apply and say why):

- **`README.md`** — the full spec: summary, problem/motivation, goals &
  non-goals, **proposed design** (architecture, components, the seam it lives
  behind, data/schema/config/API changes, sequencing), dependencies, risks &
  mitigations, open questions, rollout plan, references. If a seed README exists,
  keep its frontmatter and flesh out the stub sections (especially *Proposed
  design*).
- **`design.md`** (when the design is non-trivial) — deeper notes: alternatives
  considered and why rejected, interface sketches, diagrams (ASCII is fine),
  sequencing/edge cases.
- **`examples/`** — concrete, codebase-specific code examples: the key new/changed
  functions, config snippets, and call sites, with real signatures and paths.
  Clearly mark them as **illustrative** (a spec, not wired-in code).
- **Acceptance criteria** — inside `README.md`, a checklist of **testable**
  statements (observable behavior, not "works well").
- **`testing.md`** — the test & verification plan: unit / integration / eval
  coverage, fixtures needed, how **each** acceptance criterion is proven, and how
  it gates merge (tie into the project's CI / eval gate if one exists). Include at
  least one concrete example test in the project's idiom.

## Rules

- **Do not implement the feature.** Produce spec + examples + tests-as-spec, not
  shipped code. Examples illustrate; they are not wired into the app.
- Cross-link the roadmap/index and any specs this depends on.
- Be honest about unknowns — list them as open questions instead of papering over
  them.

## Finish

End with a tree of what you created and a one-line assessment: **ready to
implement?** (and if not, the top gap to close).
