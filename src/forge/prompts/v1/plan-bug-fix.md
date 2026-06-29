## Bug Ticket

**Key:** {ticket_key}
**Summary:** {bug_summary}

## Root Cause Analysis

{rca_content}

## Selected Fix Approach

**Title:** {fix_approach_title}
**Description:** {fix_approach_description}
**Tradeoffs:** {fix_approach_tradeoffs}

## Available Repositories

Use only these exact repository names when tagging `repo:<owner>/<name>` in the plan:

{known_repos}

## Repository Grounding Requirements

Before writing `.forge/plan.md`, inspect the relevant repository using available repository, GitHub, or filesystem tools.

- Read repo guidance when present: `AGENTS.md`, `CLAUDE.md`, `.claude/AGENTS.md`, `.claude/CLAUDE.md`, `README.md`, `CONTRIBUTING.md`, `Makefile`, language-specific project files, docs, and repo-local skills or agent instructions.
- Confirm planned files, functions/classes, test locations, generated-file requirements, and validation commands against real repository contents.
- Follow discovered repository standards for architecture, naming, error handling, testing, packaging, documentation, and local agent workflow.
- Prefer codebase exploration focused on the failing behavior, selected fix approach, nearby tests, and validation commands. Broaden the search when needed to understand the fix safely. Do not inspect project-management metadata such as unrelated branches, open issues, pull requests, milestones, or release boards unless the RCA or selected fix approach explicitly asks for them.
- Use nearby code and test patterns instead of guessing from path names alone.
- Do not invent generic paths, symbols, frameworks, test runners, or directory layouts. If repository inspection is unavailable, write the plan with an explicit blocking note explaining what repo access or configuration is required.

---

Produce an implementation plan using the plan-bug-fix skill.
Write the plan to `.forge/plan.md`.
