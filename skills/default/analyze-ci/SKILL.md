---
name: analyze-ci
description: Analyze CI failures by fetching logs and producing a structured fix plan. Use before attempting automated CI fixes.
---

# CI Failure Analysis Skill

You are given a list of failed CI checks. Fetch the actual logs, identify the root cause of each failure, and produce a structured fix plan.

## Workflow

### Step 0 — Check for prior attempts (always do this first)

Before downloading any logs, check whether a previous fix attempt already ran:

1. If `.forge/fix-plan.md` exists, read it. Identify which failures were previously fixable and which were skipped, and what fix was applied.
2. Run `git log --oneline -10` to see what changes were committed by prior fix attempts.
3. For each test still failing: was it fixed before? If yes, the prior fix did not work — do not repeat it.

### Step 1 — Fetch logs

Create `.forge/logs/` if it doesn't exist: `mkdir -p .forge/logs`

For each failed check, download the log:

```bash
# Generic URL (works for most CI systems)
curl -sL "{log_url}" -o .forge/logs/{check-name}.txt

# GitHub Actions job log
gh api repos/{owner}/{repo}/actions/jobs/{job-id}/logs > .forge/logs/{check-name}.txt

# Compressed archive
curl -sL "{url}" -o .forge/logs/{check-name}.tar.gz
tar -xzf .forge/logs/{check-name}.tar.gz -C .forge/logs/{check-name}/
```

Analyze downloaded files locally — do not print large log content to the conversation.

### Step 2 — Categorize each failure

**Fixable by code change:**
- `compile` — build or compilation errors with clear error messages
- `lint` — lint rule violations (`ruff`, `golangci-lint`, `eslint`, etc.)
- `format` — formatting violations (`gofmt`, `ruff format`, `prettier`)
- `codegen-outdated` — generated files out of sync with source
- `unit-test` — test assertion failures caused by a code bug
- `e2e-code-bug` — end-to-end test fails consistently with the same assertion error pointing to a logic defect in the code under test
- `commit-message` — commit title/trailer validation (`check-commits`, commitlint, missing `Signed-off-by`, topic prefix, title length). These require amending existing commit messages, not new file commits.

**Not fixable by code change — skip:**
- `infra` — CI infrastructure failures (runner unavailable, network timeout, quota exceeded)
- `flaky` — non-deterministic failures with varying errors across runs

### Step 3 — Write the fix plan

Write `.forge/fix-plan.md`:

```
# CI Fix Plan

## Summary
[1-2 sentences: what failed and what the fix involves]

## Fixable Failures

### {check-name}
**Category**: {compile | lint | format | codegen-outdated | unit-test | e2e-code-bug | commit-message}
**Root Cause**: {exact error or description}
**Affected Files**: {list}
**Fix**:
1. {exact command or edit}
2. {verification command}

When category is `commit-message`, also include:

### Amended Commit Message
```
{full corrected commit message including required trailers}
```

## Skipped Failures

### {check-name}
**Reason**: {infra | flaky} — {brief explanation}
```

## Important

- Fetch the actual logs — do not guess based on the check name
- Be specific: include exact file paths and error messages
- If a log is unavailable, mark the failure as skipped with reason "log unavailable"
- If a test was previously fixed and is still failing, do not repeat the same approach — re-read the code and find a different root cause

## Documentation ripple

For every fixable failure that changes a constant, threshold, or behavior, search for stale references:

```bash
grep -r "<old value>" . --include="*.go" --include="*.py" --include="*.md" -l
```

Include stale documentation files in **Affected Files** alongside the implementation files.
