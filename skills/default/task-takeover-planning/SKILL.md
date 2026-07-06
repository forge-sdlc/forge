---
name: task-takeover-planning
description: Produce a concrete implementation plan for a standalone Task or Epic takeover ticket. Use when a Jira Task/Epic is approved for Task Takeover planning and needs target files, implementation steps, tests, and repository scope.
---

# Task Takeover Planning Skill

Create a concise, executable implementation plan for the standalone task.

## Repository Selection

1. Use only repositories listed in the prompt's Available Repositories section.
2. Infer the target repository from the ticket summary, description, comments, and any existing `repo:<owner>/<repo>` labels in context.
3. Inspect the selected repository with available repository, GitHub, or filesystem tools before naming files, commands, or test locations.
4. If multiple repositories are genuinely involved, include each one. For ordinary single-repo tasks, choose exactly one.
5. Do not choose the first configured repository as a fallback. If the target repository cannot be determined from the ticket and repository inspection, write a blocking note instead of guessing.

## Repository Grounding

Before writing the plan:

1. Read repository guidance when available, including `AGENTS.md`, `CLAUDE.md`, `.claude/AGENTS.md`, `.claude/CLAUDE.md`, `README.md`, `CONTRIBUTING.md`, `Makefile`, language-specific project files, and docs.
2. Inspect only files relevant to the requested change and nearby tests or validation commands.
3. Confirm target files and validation commands against real repository contents.
4. Follow existing repository conventions for documentation style, tests, generated files, formatting, and local workflow.
5. Prefer the smallest plan that safely implements the ticket.

If repository inspection is unavailable, make that explicit in the plan and do not invent paths or commands.

## Plan Requirements

The plan must include:

1. **Repository Scope** with explicit `repo:<owner>/<repo>` tag(s). These tags drive downstream automation and Jira labels.
2. **Target Files** with specific existing files or clearly justified new files.
3. **Implementation Steps** in execution order.
4. **Validation Plan** with concrete commands or manual checks appropriate for the change.
5. **Risks / Open Questions** only when something remains uncertain.

## Output Format

Return structured Markdown only. Do not include JSON, tool logs, or meta-commentary.
