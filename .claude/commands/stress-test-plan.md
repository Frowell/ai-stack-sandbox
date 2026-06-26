---
description: Stress-test a plan — surface blockers by severity (Critical/High/Medium/Low), then refine the plan from the findings
argument-hint: "[path to plan/spec, or blank to use the current plan]"
allowed-tools: Read, Grep, Glob, Edit, Write, Bash(git:*)
---

You are stress-testing a plan as a skeptical senior engineer. Be adversarial
about the **ideas** and constructive about the **outcome**. The goal is to break
the plan on paper now so it doesn't break in production later.

## The plan

Target: $ARGUMENTS

- If that is a file or directory path, Read it **and** the closely related context
  (linked specs, the code/seams it touches, existing patterns it must fit).
- If it is empty, use the most recent plan/spec in this conversation. If there
  is none, ask which plan to review and stop.

## 1. Understand it first

Briefly restate the plan's **goal, key assumptions, scope, and success criteria**
as you understand them. If a load-bearing point is ambiguous, name it — do not
invent intent.

## 2. Stress-test the ideas

Probe hard; try to make it fail. For each angle ask *"what makes this fail in
production?"*, not *"is this fine?"*. Cover at least:

- **Assumptions** that are false or unstated.
- **Failure modes & edge cases** the plan doesn't handle (bad/empty/hostile input,
  partial failure, concurrency, retries, idempotency).
- **Dependencies & sequencing** — wrong order, missing prerequisites, circular
  deps, work that can't actually start yet.
- **Hidden complexity / scope** — the "simple" step that isn't.
- **Integration & interface risks** — where this meets existing code, seams, data,
  or third parties.
- **Correctness, security, data** — trust boundaries, secrets, migrations,
  irreversible actions.
- **Scale, performance, cost, latency** — what breaks at 10×.
- **Operability** — observability, rollback, config, on-call surface.
- **Testing & verification gaps** — what can't be tested as planned, and what
  evidence would actually prove it works.
- **Alternatives** — a materially simpler or more robust approach that was skipped.

## 3. Categorize the findings

Output a findings list, each tagged by severity using this rubric:

| Severity | Meaning |
|---|---|
| **Critical** | Blocks the plan, or causes failure / data loss / security hole if shipped as-is. Must fix before proceeding. |
| **High** | Likely significant rework, a missed goal, or an incident. Fix before or early in implementation. |
| **Medium** | Real risk or gap; fix during implementation or accept explicitly. |
| **Low** | Minor / polish; note and move on. |

For each finding give: **severity · the issue · why it matters (impact) · a
concrete recommended resolution**. Order by severity, highest first. State your
confidence and flag uncertainty rather than overstating. If there are no
Critical/High findings, say so plainly — do **not** manufacture severity.

## 4. Refine the plan from the findings

Revise the plan to resolve every **Critical** and **High** finding, plus
**Medium** findings that are cheap to address. For anything not fixed, record it
explicitly as an accepted risk or open question — never silently drop it.

- If the target is an editable plan/spec file, update it **in place** (tighten
  goals, fix sequencing, add missing steps, strengthen acceptance criteria, add a
  Risks / Open-questions section), then summarize what changed and why.
- Otherwise, output the refined plan.

## Verdict

End with a one-line verdict — **ready to implement** / **ready with fixes** /
**needs rework** — and the single most important thing to resolve next.
