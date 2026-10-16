---
name: fix-ci
description: Apply CI fixes from a pre-analyzed fix plan. Use after analyze-ci has produced a structured plan.
---

# CI Fix Skill

You are running inside a container with the full repository workspace. You have been given a pre-analyzed fix plan. Verify that its assumptions still match the current code and failure evidence before applying it.

## Workflow

For each **Fixable Failure** in the fix plan:

1. Read the affected files listed in the plan
2. Apply the fix as described:
   - Run codegen commands with `execute`
   - Apply code edits with `edit_file`
   - Run formatters with `execute`
3. Run the verification command from the plan to confirm the fix works
4. Move to the next failure

Skip anything listed under **Skipped Failures** — do not attempt to fix them.

## After All Fixes

1. Commit with a clear message referencing what was fixed
2. Do NOT push — the orchestrator handles that

## Guidelines

- Follow the plan — do not invent additional fixes to the logic
- Be surgical: only change files listed in the plan for the core fix
- If a plan step is disproven or cannot apply, stop that fix and write `.forge/fix-blocked.md` with the check, evidence, and required re-analysis. Do not weaken tests, mask errors, or silently claim the failure was fixed. Other independent valid fixes may proceed.
- Do not reformat files not mentioned in the plan

## Ripple updates — required after every fix

After applying each fixable failure, search for stale references to the changed value or behavior and update them. These are not "additional fixes" — they are required completeness for any fix that changes a constant, threshold, algorithm, or behavior.

For each fix applied:
1. Identify the key values or behaviors that changed (e.g., jitter changed from ±10% to [0%, +20%], a flag was renamed, a condition was added).
2. Search the repository for any other files that reference the old value:
   - Inline code comments (`// ±10% jitter`)
   - Documentation files (`docs/`, `website/docs/`, `enhancements/`, `*.md`)
   - User guides and enhancement documents
   - Test comments or test helper descriptions
   Use: `grep -r "±10%" .` or equivalent for the specific value changed.
3. Update every stale reference to match the new behavior. Keep the change minimal — only correct the factual inaccuracy, do not rewrite surrounding context.
4. Include the updated documentation files in the same commit as the fix.

**Example:** If the fix changes jitter from ±10% to [0%, +20%], you must also:
- Update any inline comment in any `.go` file that says "±10%"
- Update any `enhancements/`, `docs/`, or `website/` file that describes the jitter as "±10%"
- Do NOT update files that were already correct or files unrelated to jitter

Preserve historical release notes and superseded design records; update only
statements that describe current behavior.
