---
name: decompose-epics
description: Decompose a Technical Specification into implementable Epics with technical plans. Use when asked to break down features, create epics, or plan implementation.
---

# Epic Decomposition Skill

Decompose a specification into 2-5 implementable Epics using the template and guidelines below.

## Instructions

1. Read the template from `skills/default/decompose-epics/epic-template.md`
2. Analyze the specification content
3. Inspect each target repository listed in the prompt before proposing Epics
4. Identify 2-5 cohesive capability areas
5. Create a detailed implementation plan for each
6. Map dependencies between Epics
7. Validate against the quality checklist

## Repository Grounding

You must ground the Epic plan in the actual target repository before naming implementation files or components.

For each target repository listed in the prompt:

1. Inspect the repository structure using the available repository, GitHub, or filesystem tools.
2. Determine the language, framework, build system, and test layout from real files.
3. Read repository guidance when present, including `AGENTS.md`, `CLAUDE.md`, `.claude/AGENTS.md`, `.claude/CLAUDE.md`, `README.md`, `CONTRIBUTING.md`, `Makefile`, `pyproject.toml`, `package.json`, `go.mod`, `docs/`, and repo-local skills or agent instructions.
4. Search for existing modules, workflow nodes, routers, clients, tests, and helpers related to the requested feature.
5. Identify repository standards for architecture, naming, testing, packaging, documentation, and local agent workflow from guidance files and existing implementation patterns.
6. Use existing paths, naming conventions, abstractions, and repository standards whenever possible.

Prefer targeted codebase exploration. Start with repository-level guidance and structure files, then look at code and tests directly related to the requested feature. Broaden the search when needed to understand the change safely. Avoid exhaustively reading unrelated packages or repeating the same repository survey for each Epic. Do not inspect project-management metadata such as unrelated branches, open issues, pull requests, milestones, or release boards unless the specification explicitly asks for them.

Implementation plans must reference real repository paths discovered during inspection. If a file is new, place it in an existing directory that matches the repository's conventions. Do not invent generic paths such as `pkg/...`, `src/...`, or framework-specific directories unless the repository inspection shows that those paths exist.

Implementation plans must also follow repository standards discovered during inspection. Do not propose a new framework, test runner, directory layout, service boundary, agent convention, or documentation style when the repository already establishes a relevant standard. If the requested change requires deviating from a repository standard, call out the reason explicitly in the plan.

Use existing paths together with nearby code patterns to avoid guessing where the change belongs. When a new file is needed, prefer the nearest established source and test layout. If the right location is unclear after checking the relevant area, note the uncertainty instead of broadening into unrelated repo areas.

If repository inspection is unavailable or fails, do not guess implementation paths. Surface the repository grounding failure clearly so downstream planning does not proceed from invented architecture.

## Decomposition Rules

1. **Cohesive**: Each Epic represents a single, deployable capability.
2. **Independent**: Minimize dependencies between Epics.
3. **Vertical Slices**: Prefer end-to-end slices over horizontal layers.
4. **Sized Right**: Each Epic should be 1-3 sprints of work.
5. **Clear Boundaries**: No overlap between Epics.

## Epic Naming Convention

Use format: `[Verb] [Noun] [Qualifier]`

Examples:
- "Implement User Authentication System"
- "Create Dashboard Analytics Module"
- "Build Notification Delivery Pipeline"

## Ordering Principles

1. **Foundation First**: Infrastructure/setup Epics before feature Epics
2. **Dependencies**: Dependent Epics come after their dependencies
3. **Value Delivery**: Higher-value Epics earlier when possible
4. **Risk Reduction**: Technical risk Epics early to fail fast

## Quality Checklist

Before returning the Epic breakdown:

- [ ] 2-5 Epics total (not more, not fewer)
- [ ] Each Epic has clear, non-overlapping scope
- [ ] Dependencies between Epics documented
- [ ] Technical approach is specific and grounded in inspected repository files
- [ ] Key files/components use real repository paths, or new paths are explicitly marked and justified
- [ ] Repository guidance files were considered when available
- [ ] Proposed architecture, tests, docs, and workflow conventions follow discovered repository standards
- [ ] Technical approach follows relevant existing code patterns
- [ ] Repository inspection focused on relevant guidance, code, and nearby tests
- [ ] Complexity estimates provided
- [ ] Acceptance criteria are verifiable
- [ ] Implementation is phased logically
- [ ] All specification scenarios covered across Epics

## Output Format

For each Epic, use this format:

```
---
EPIC: [Epic Title]
PLAN:
[Full epic content following skills/default/decompose-epics/epic-template.md]
---
```

Repeat for each Epic (2-5 total).

IMPORTANT: Return ONLY the Epic content. Do not include any planning text, explanations of what you're doing, or meta-commentary. Start directly with the first Epic.
