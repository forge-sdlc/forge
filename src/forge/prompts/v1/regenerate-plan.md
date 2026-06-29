## Bug Ticket

**Key:** {ticket_key}
**Summary:** {bug_summary}

## Root Cause Analysis

{rca_content}

## Selected Fix Approach

**Title:** {fix_approach_title}
**Description:** {fix_approach_description}
**Tradeoffs:** {fix_approach_tradeoffs}

## Original Plan

{original_plan}

## Feedback to Address

{feedback_comment}

## Available Repositories

Use only these exact repository names when tagging `repo:<owner>/<name>`:

{known_repos}

## Repository Re-check Requirements

If the feedback changes or questions implementation scope, repository scope, files, functions/classes, tests, generated artifacts, validation commands, or project conventions, re-inspect the relevant repository before revising `.forge/plan.md`.

- Read repo guidance when present: `AGENTS.md`, `CLAUDE.md`, `.claude/AGENTS.md`, `.claude/CLAUDE.md`, `README.md`, `CONTRIBUTING.md`, `Makefile`, language-specific project files, docs, and repo-local skills or agent instructions.
- Confirm revised files, symbols, tests, commands, and generated-file requirements against real repository contents.
- Preserve valid grounded details from the original plan when feedback does not challenge them.
- Follow discovered repository standards for architecture, naming, error handling, testing, packaging, documentation, and local agent workflow.
- Prefer focused codebase re-inspection of details the feedback changes or questions; reuse grounded details from the original plan when they remain valid, and broaden the search when needed to revise safely. Do not inspect project-management metadata such as unrelated branches, open issues, pull requests, milestones, or release boards unless the feedback explicitly asks for them.
- Keep nearby source and test patterns unless the feedback explicitly requires a justified change.
- Do not invent paths, symbols, frameworks, test runners, or directory layouts. If repository inspection is required but unavailable, write the revised plan with an explicit blocking note explaining what repo access or configuration is required.

---

Revise the plan using the regenerate-plan skill, addressing all feedback.
Write the revised plan to `.forge/plan.md`.
