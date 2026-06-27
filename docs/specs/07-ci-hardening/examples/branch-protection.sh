#!/usr/bin/env bash
# ILLUSTRATIVE — run LAST, only after the hardened ci.yml is on `main` and has
# been observed green for one cycle (README "Test & rollout plan" step 4).
#
# Marks EXACTLY `lint` and `eval-gate-result` as required. Never `eval-gate`
# (it is skipped on forks → would wedge every fork PR; see design.md §1).
#
# Pre-flight (AC #6): the required-check names MUST match jobs the workflow
# actually emits, or the check stays "pending" and wedges all PRs. Verify first:
#   grep -E '^  (lint|eval-gate-result):' .github/workflows/ci.yml
set -euo pipefail

REPO="${1:?usage: branch-protection.sh <owner/repo>}"
BRANCH="main"

# Confirm the exact check names appeared on a recent run before requiring them.
gh api "repos/$REPO/commits/$BRANCH/check-runs" \
  --jq '.check_runs[].name' | sort -u

# Require both checks (strict = branch must be up to date before merge).
gh api -X PUT "repos/$REPO/branches/$BRANCH/protection" \
  --input - <<'JSON'
{
  "required_status_checks": {
    "strict": true,
    "contexts": ["lint", "eval-gate-result"]
  },
  "enforce_admins": false,
  "required_pull_request_reviews": null,
  "restrictions": null
}
JSON

# Break-glass for a provider outage (README "Risks"): temporarily drop the eval
# check, merge, then restore — keeping `lint` required throughout:
#   contexts: ["lint"]   (then re-PUT with both once the provider recovers)
echo "branch protection set: required = lint, eval-gate-result"
