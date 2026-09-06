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

For each of the 3–8 Tasks, provide a concise `summary` and a complete `description` that
includes grounded files and symbols, integration points, nearby patterns, repository
standards, and explicit acceptance criteria including tests.
