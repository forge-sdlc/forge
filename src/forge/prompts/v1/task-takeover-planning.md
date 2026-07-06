## Task Ticket

**Key:** {ticket_key}
**Summary:** {summary}

**Description:**
{description}

**Comments:**
{comments}

## Available Repositories

Use only these exact repository names when tagging `repo:<owner>/<name>` in the plan:

{known_repos}

## Planning Context

No local repository clone is provided during planning. Use available repository, GitHub, or filesystem tools to identify and inspect the correct repository from the configured list before naming target files or validation commands.

{file_metadata}

## Repository Grounding Requirements

Before returning the plan, inspect the relevant repository using available repository, GitHub, or filesystem tools.

- Read repo guidance when present: `AGENTS.md`, `CLAUDE.md`, `.claude/AGENTS.md`, `.claude/CLAUDE.md`, `README.md`, `CONTRIBUTING.md`, `Makefile`, language-specific project files, docs, and repo-local skills or agent instructions.
- Confirm planned files, functions/classes, test locations, generated-file requirements, and validation commands against real repository contents.
- Follow discovered repository standards for architecture, naming, error handling, testing, packaging, documentation, and local agent workflow.
- Prefer codebase exploration focused on the ticket description, proposed solution/approach, nearby code, and validation commands. Broaden the search when needed to understand the context safely. Do not inspect project-management metadata such as unrelated branches, open issues, pull requests, milestones, or release boards unless explicitly required.
- Use nearby code and test patterns instead of guessing from path names alone.
- Do not invent generic paths, symbols, frameworks, test runners, or directory layouts. If repository inspection is unavailable, write the plan with an explicit blocking note explaining what repo access or configuration is required.
- All target files and file references MUST be repository-relative paths such as `README.md` or `src/forge/workflow/nodes/task_takeover_planning.py`. Never include absolute local, container, or host paths such as `/home/...`, `/tmp/...`, or `/workspace/...`.

## Formulate Implementation Plan

Formulate a concrete implementation plan mapping the proposed solution to specific target files and test plans.

Your plan MUST include:
1. **Target Files**: List the specific, existing repository files to be modified, or new files to be created, using repository-relative paths only.
2. **Implementation Steps**: Clear, sequential steps for implementing the proposed solution/approach.
3. **Test Plans**: A detailed validation plan describing how the changes will be tested. Map the proposed solutions to concrete unit or integration tests, naming specific test commands and test files (existing or new) to run.
4. **Repository Tags**: Include at least one `repo:<owner>/<repo>` tag using only names from the Available Repositories section. For single-repository tasks, include exactly one repo tag.

---

Produce a detailed implementation plan.
Return the plan as Markdown only. Do not write files.
