---
name: generate-tasks
description: Break down Epic implementation plans into concrete, actionable Tasks. Use when decomposing Epics into implementation units.
---

# Task Generation Skill

Generate implementation Tasks from Epic plans following the guidelines below.

## Instructions

1. Analyze the Epic implementation plan
2. Identify discrete, testable units of work (2-8 hours each)
3. Define clear acceptance criteria for each Task
4. Identify which repository each Task belongs to
5. Order Tasks by dependency (foundation first)

## Repository Grounding

Tasks must preserve the repository grounding established by the Epic plan.

Before decomposing Tasks:

1. Check whether the Epic plan names real repository paths, existing components, tests, and repo guidance.
2. Use those grounded paths and standards when splitting work into Tasks.
3. If the Epic plan lacks enough repository context for a concrete Task, do not invent files, packages, frameworks, test runners, or directory layouts.
4. When repository tools are available, inspect the target repository to fill in missing implementation details before naming files or functions.
5. Follow discovered repository standards for architecture, naming, testing, packaging, documentation, and local agent workflow.

Prefer additional codebase exploration only for details missing from the Epic plan. Reuse grounded Epic context when it remains applicable, and broaden the search when needed to understand the change safely. Look at nearby source and test patterns before splitting implementation work. Do not inspect project-management metadata such as unrelated branches, open issues, pull requests, milestones, or release boards unless the Epic explicitly asks for them.

If repository grounding is unavailable, keep the Task scoped to the investigation or access needed to ground the work instead of turning assumptions into implementation steps.

## Task Sizing Rules

1. **Atomic**: Each Task should be completable in a single PR
2. **Testable**: Clear acceptance criteria that can be verified
3. **Independent**: Minimize dependencies between Tasks where possible
4. **Sized Right**: 2-8 hours of work per Task

## Output Format

For each Task, use this format:

```
---
TASK: [Task Title]
REPO: [repository-name or "unknown"]
DESCRIPTION:
[Detailed implementation steps]

ACCEPTANCE_CRITERIA:
- [Criterion 1]
- [Criterion 2]
---
```

Repeat for each Task (typically 3-10 Tasks per Epic).

## Quality Checklist

Before returning the Task breakdown:

- [ ] Each Task is atomic and completable in one PR
- [ ] Acceptance criteria are specific and testable
- [ ] Dependencies are identified and ordered correctly
- [ ] Repository assignments are clear
- [ ] File paths, test targets, and implementation conventions are grounded in the Epic plan or inspected repository
- [ ] Tasks follow discovered repository standards
- [ ] Tasks follow nearby source and test patterns when the repo establishes them
- [ ] No gaps - full Epic coverage across all Tasks
