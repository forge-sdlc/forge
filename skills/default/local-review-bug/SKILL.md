---
name: local-review-bug
description: Qualitative code review for bug fixes — verifies root-cause alignment and test coverage before PR creation. Use after implement-task completes for bug tickets.
---

# Bug Fix Local Review Skill

Review the implemented changes against the approved RCA and plan. You have access to the workspace and codebase. Run `git diff origin/main...HEAD` to see what changed.

## Step 1 — Mechanical Checks

Run these first and fix any failures before proceeding to the qualitative review:

1. **Linters** — run the project's linter (e.g. `ruff check .`, `go vet ./...`)
2. **Type checker** — run mypy or equivalent if configured
3. **Test suite** — run all tests and confirm they pass

If any mechanical check fails and cannot be fixed, report it in the feedback.

## Step 2 — Qualitative Checklist

Evaluate the diff against each item:

1. **Root cause alignment** — Does the change address the confirmed root cause, or only a symptom?
2. **Test proof** — Do the new/modified tests actually prove the bug is fixed? Would they fail without the fix?
3. **Fix stability** — Could someone break this fix without a test catching it?
4. **Plan scope adherence** — Does the diff match the approved plan scope, or has it drifted?
5. **Call site completeness** — Are there similar patterns elsewhere in the codebase that need the same treatment?
6. **Backward compatibility** — Is the fix safe to roll back? Does it avoid breaking interfaces?
7. **Bidirectional validation** — Does the commit log show `[bidirectional: PASS]`, confirming the agent verified the test fails without the fix?

8. **Ordering invariants** — Find the `## Ordering Invariants` section in the approved plan above. For each entry, locate the relevant calls in the diff (`git diff origin/main...HEAD`) and verify the stated order is preserved in the implementation. If the section says "None identified.", spot-check the diff for operation pairs with non-obvious side effects (e.g. a call that deletes or resets shared state before a call that depends on it). Flag any reversal as a blocking issue in your feedback.

## Output

The verdict format is a forge protocol constraint — use it exactly:

```
verdict: adequate
```
or
```
verdict: tests_incomplete
```
or
```
verdict: symptom_only
```

Followed by:
```
feedback: <specific, actionable description of what needs to change — or "All checks passed." if adequate>
```

Only these three verdict values are valid. Do not use any other string.
