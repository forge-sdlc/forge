---
name: plan-bug-fix
description: Produce a concrete implementation plan for a bug fix from an approved RCA and selected fix approach. Use when the team has selected a fix option and needs a detailed plan before implementation.
---

# Bug Fix Planning Skill

Produce a concrete implementation plan that an engineer can execute without re-reading the RCA. You have access to the codebase — use it to confirm file paths, function names, test locations, and repository standards before writing the plan.

## Repository Grounding

Before writing the plan:

1. Inspect the relevant repository paths named by the RCA and selected fix approach.
2. Read repository guidance when present, including `AGENTS.md`, `CLAUDE.md`, `.claude/AGENTS.md`, `.claude/CLAUDE.md`, `README.md`, `CONTRIBUTING.md`, `Makefile`, language-specific project files, docs, and repo-local skills or agent instructions.
3. Identify existing standards for architecture, naming, error handling, tests, generated files, documentation, and local agent workflow.
4. Confirm every planned file, function, class, test location, and command against the codebase or repository guidance.
5. Prefer existing abstractions and helpers over new patterns.

Prefer codebase exploration focused on the failing behavior, selected fix approach, and nearby tests. Broaden the search when needed to understand the fix safely. Do not inspect project-management metadata such as unrelated branches, open issues, pull requests, milestones, or release boards unless the RCA or selected fix approach explicitly asks for them.

If you cannot inspect the relevant repository, do not guess file paths, symbols, test commands, or implementation standards. Make the access problem explicit in the plan so implementation does not proceed from invented details.

## What the Plan Must Cover

1. **Files to change** — for each file: what to change, where, and why. Confirm the file and function exist before listing them.

2. **New tests required** — include test function names and what each test verifies. If the RCA includes a reproduction test, reference it.

3. **Order of operations** — if changes must be sequenced (e.g. schema before logic, test before implementation), specify the order.

4. **Repository scope** — tag each involved repository explicitly as `repo:<owner>/<name>` (e.g. `repo:acme/backend`). This is required for downstream automation that creates per-repo tasks.

5. **Precision** — the plan must be specific enough that an implementer can execute it without reading the RCA again.

6. **Repository standards** — the plan must follow standards discovered from repository guidance and existing implementation patterns. If a deviation is required, explain why.

7. **Existing patterns** — the plan should use nearby code and test patterns instead of guessing from path names alone. New tests should follow adjacent test layout when the repo establishes one.

8. **Ordering-sensitive operations** — after drafting all code changes, scan each function and block in the plan for pairs of operations where order matters because of non-obvious side effects. For each, write an entry under a top-level `## Ordering Invariants` heading with three fields:
   - **What**: the two operations and their required order (e.g. `` `A()` → `B()` ``)
   - **Why**: the side effect or dependency that makes the order matter
   - **Breaks if reversed**: the concrete failure mode

   This section is required even when there are none — write "None identified." so the implementer knows it was not omitted by accident.

   The trigger: *would a reasonable engineer, seeing only these two calls, be able to reverse their order without knowing the system?* If yes, document the invariant.

## Format

Write the plan as structured Markdown. Use headers for each section. Do not include JSON.

When done, write the plan to `.forge/plan.md`. Do not write any other files. Do not commit.
