Please decompose the following specification into logical Epics with implementation plans:

{spec_content}

Additional context:
- Feature: {feature_summary}
- Project: {project_key}
{repo_instruction}

## Repository Grounding Requirements

Before writing Epics, inspect every target repository listed above using the available repository, GitHub, or filesystem tools.

For each repository:
- Determine the actual language, framework, build system, and test layout from real files.
- Read repository guidance if present: `AGENTS.md`, `CLAUDE.md`, `.claude/AGENTS.md`, `.claude/CLAUDE.md`, `README.md`, `CONTRIBUTING.md`, `Makefile`, `pyproject.toml`, `package.json`, `go.mod`, `docs/`, and repo-local skills or agent instructions.
- Search for existing modules, workflow nodes, routers, clients, tests, and helpers related to the requested change.
- Identify repository standards for architecture, naming, testing, packaging, documentation, and local agent workflow from guidance files and existing implementation patterns.
- Base implementation plans on existing repository conventions, reusable helpers, and discovered repository standards.

Prefer targeted codebase exploration: inspect enough relevant guidance, code, and nearby tests to avoid guessing. Broaden the search when needed to understand the change safely, and avoid repeating the same repository context across every Epic. Do not inspect project-management metadata such as unrelated branches, open issues, pull requests, milestones, or release boards unless the specification explicitly asks for them.

Every file path in an Epic plan must be one of:
- a real existing path discovered during repository inspection, or
- a clearly marked new file path placed in an existing directory that matches local conventions.

Do not invent generic paths such as `pkg/...`, `src/...`, or framework-specific directories unless repository inspection shows they exist. If repository inspection is unavailable or fails, do not guess paths. Instead, return an Epic with a plan that starts with a blocking note explaining that repo grounding failed and what access/configuration is required.

Every Epic plan must follow discovered repository standards. Do not propose a new framework, test runner, directory layout, service boundary, agent convention, or documentation style when the repository already establishes a relevant standard. If the requested change requires deviating from a repository standard, state the reason in the plan.

Every Epic plan should use existing paths together with nearby code patterns to avoid guessing where the change belongs. When a new file is needed, prefer the nearest established source and test layout. If the right location is unclear after checking the relevant area, call out the uncertainty instead of broadening into unrelated repo areas.

## Scope Guidelines

Choose the number of Epics based on feature complexity:
- **Simple features** (single config field, one endpoint, isolated change): 1 Epic
- **Medium features** (2-3 related components, moderate integration): 2-3 Epics
- **Large features** (multiple subsystems, extensive integration): 3-5 Epics

Fewer Epics is better. Only split when work is genuinely independent and parallelizable.
Avoid artificial separation like "Config Epic" + "Validation Epic" + "Tests Epic" -
these belong together in one cohesive Epic.

## Output Format

You MUST use this exact format for each Epic. The parser depends on these exact prefixes:

```
EPIC: [Concise epic title - max 100 chars]
REPO: [owner/repo from the available repositories]
PLAN:
[Detailed implementation plan with:]
- Technical approach and architecture decisions, including relevant existing patterns when clear
- Key components/files to create or modify, using grounded repository paths
- Repository standards followed, including relevant architecture, test, docs, and workflow conventions; keep this concise and do not repeat the same repository context across Epics
- Dependencies and integration points
- Testing strategy
- Estimated complexity (S/M/L)
---
```

Separate each Epic with `---` on its own line.
