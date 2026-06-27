# Examples — CI hardening (ILLUSTRATIVE)

> **These files are a specification, not wired-in code.** They show the *intended*
> shape of the hardened `.github/workflows/ci.yml` and its supporting config,
> grounded in the real merged workflow (`main:.github/workflows/ci.yml`). Do
> **not** copy them into `.github/` as-is: the SHAs and `sha256:` digests below
> are **placeholders** (`<...>`) that must be resolved at implementation time via
> the commands in [`../design.md` §5](../design.md). Diff against the live file
> before applying.

| File | What it illustrates |
|------|---------------------|
| `ci.hardened.yml` | The full target workflow: `--locked`, pinned actions+images, cache-on-lock, and the `eval-gate-result` summary job. |
| `eval-gate-result.snippet.yml` | The summary job in isolation, with inline comments mapping to the truth table in `design.md`. |
| `dependabot.yml` | Auto-PR action-SHA bumps so pinning doesn't rot. |
| `branch-protection.sh` | `gh` commands to mark `lint` + `eval-gate-result` required (run **last**). |
| `negative-test-lock-drift.md` | The throwaway PR that proves drift detection (AC #1). |
