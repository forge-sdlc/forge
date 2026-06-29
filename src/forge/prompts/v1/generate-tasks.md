Please break down the following Epic into implementation Tasks.

## Specification

{spec_content}

## Full Epic Plan

EPIC: {epic_summary}

{epic_plan}

## Other Epics in This Feature

{sibling_epics_section}

## Already Created Tasks (from other Epics)

{existing_tasks_section}

Generate 3-8 concrete Tasks that can be completed in 2-8 hours each.

## Guidance

- Use the Specification above to ensure tasks cover all acceptance criteria relevant to this Epic
- Use the Other Epics section to understand what neighbouring Epics are responsible for — do not duplicate their work
- Cross-cutting concerns (e.g., "add tests", "update docs") should only appear once across all Epics
- If this Epic needs integration with work from another Epic, reference it rather than recreating it
- Preserve the Epic plan's repository grounding: use only file paths, components, test targets, and repo standards supported by the Epic plan or by direct repository inspection
- If the Epic plan lacks enough repository context for concrete implementation Tasks, use available repository tools to inspect the target repo before naming files, functions, frameworks, test runners, or directory layouts
- Do not invent generic paths or introduce new repo standards. If repository grounding remains unavailable, create a Task that clearly scopes the required repo investigation/access before implementation proceeds
- Prefer additional codebase exploration only for missing implementation details. Reuse grounded Epic context when applicable, and broaden the search when needed to understand the change safely. Do not inspect project-management metadata such as unrelated branches, open issues, pull requests, milestones, or release boards unless the Epic explicitly asks for them.
- Each Task should follow nearby source/test patterns when the repo establishes them

## Output Format

You MUST use this exact format for each Task. The parser depends on these exact prefixes:

```
TASK: [Concise task title - max 100 chars]
REPO: [owner/repo - inherit from Epic if not specified]
DESCRIPTION:
[What needs to be implemented, including:]
- Specific grounded files to create/modify
- Functions/classes to implement, using names verified from the Epic plan or repository
- Integration points
- Relevant existing source or test pattern when clear
- Repository standards to follow for architecture, tests, docs, and local workflow
ACCEPTANCE_CRITERIA:
- [ ] Criterion 1
- [ ] Criterion 2
- [ ] Tests pass
---
```

Separate each Task with `---` on its own line. Include 3-8 Tasks total.
