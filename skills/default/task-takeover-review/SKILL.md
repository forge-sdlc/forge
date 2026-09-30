---
name: task-takeover-review
description: Read-only qualitative review for Task Takeover implementations before PR creation. Use after task takeover execution completes.
---

# Task Takeover Review Skill

Review the implemented changes against the task acceptance criteria and the approved plan. This is a review-only stage.

## Read-Only Contract

Do not edit, format, generate, stage, commit, or write files. Do not apply fixes. If the implementation needs changes, report them in `feedback` so the workflow can start a separate implementation container.

If you find a repo-local review skill or instruction that tells you to fix issues, use only its review checklist and validation guidance. Skip all file-editing, formatting, staging, committing, and autofix steps.

## Step 1 - Gather Review Context

1. Inspect repo-local guidance when present: `AGENTS.md`, `.agents/`, `CLAUDE.md`, `.claude/`, `README.md`, `CONTRIBUTING.md`, Makefile targets, and any repo-local skills or agent instructions.
2. Use relevant repo-local review skills and checklists if they exist.
3. Inspect the complete diff supplied by the runtime and the relevant current files. An unavailable diff is a limitation, not an empty change. Do not execute shell commands in this read-only stage.

## Step 2 - Review Checklist

Evaluate these items:

1. Acceptance criteria: every target requirement is fully implemented.
2. Approved plan scope: the diff matches the approved task plan and does not drift into unrelated work.
3. Validation coverage: behavior changes have meaningful regression coverage. Docs-only changes use applicable link/render/lint checks; no artificial test file is required. Inspect `.forge/validation.md` and report missing evidence honestly.
4. Test relevance: tests actually verify the requested behavior and would catch regressions.
5. Repo-specific review guidance: relevant local review instructions or skills were considered.
6. Breaking issues: no obvious build, runtime, security, or contract problems were introduced.

## Output

The verdict format is a forge protocol constraint. Use it exactly:

```
verdict: adequate
```

or

```
verdict: tests_incomplete
```

Followed by:

```
feedback: <specific, actionable description of what needs to change, or "All checks passed." if adequate>
```

Only these two verdict values are valid. Do not use any other string.
