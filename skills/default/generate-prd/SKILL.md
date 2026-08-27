---
name: generate-prd
description: Generate a structured Product Requirements Document (PRD) from raw requirements. Use when asked to create a PRD, product spec, requirements document, or feature definition.
---

# PRD Generation Skill

Generate a Product Requirements Document using the template and guidelines below.

> **IMPORTANT**: Return ONLY the PRD content. Do not include any planning text, explanations of what you're doing, or meta-commentary. Start directly with the PRD title.

## Instructions

1. Read the template from `skills/default/generate-prd/prd-template.md`
2. **Fetch attachments**: Check whether the feature ticket has any attachments (e.g. mockups, research docs, specs, diagrams). Use `mcp__atlassian__jira_download_attachments` or equivalent Jira tools to retrieve them. For each attachment, attempt to read or fetch its content and incorporate it as additional context. If an attachment cannot be read (e.g. unsupported binary format), note its filename and skip it.
3. **Explore the target repositories**: For every repository identified in the ticket or additional context, use the available GitHub, repository, or filesystem tools to inspect the repository before writing the PRD.
   - Read repository guidance and product context when present, including `README.md`, `AGENTS.md`, `CLAUDE.md`, `CONTRIBUTING.md`, relevant files under `docs/`, and nearby code or tests that establish current user-visible behavior.
   - Focus exploration on understanding the existing product behavior, terminology, supported workflows, integrations, and constraints relevant to the requested feature.
   - Use repository findings to make requirements accurate and to identify genuine assumptions or open questions. Do not turn the PRD into an implementation plan or prescribe technical solutions.
   - Do not invent repository details. If a target repository cannot be accessed, record the missing access or unresolved repository-dependent facts as assumptions or open questions in the PRD.
4. Analyze the raw requirements together with attachment content and relevant repository findings
5. Fill in all sections of the template
6. Ensure every requirement is testable and specific
7. Validate against the quality checklist

## Size Calibration

Match PRD depth to feature complexity. A single config field or small enhancement does not need the same treatment as a new subsystem.

- **Personas**: Define only the distinct user types who are actually affected. If one engineer role covers all use cases, write one persona. Do not invent additional personas to fill the template.
- **User stories**: Write one story per distinct user goal. Three similar stories for the same user and goal should be one story with multiple acceptance criteria.
- **Glossary**: Only include terms that are non-obvious to the target audience (engineers in this domain). Do not define "API", "CI", or other industry-standard terms.
- **Timeline**: If no dates or sprint info is provided, the timeline section adds no value. Either omit it or keep it to a single sentence noting dates are TBD.
- **Risks**: 2-4 specific, realistic risks are better than 6 generic ones. Each must name a concrete failure mode, not a vague category.

## Generation Rules

1. **Be Specific**: Avoid vague language. Every requirement must be testable.
2. **Prioritize**: Use MVP (must-have), non-MVP (should-have), nice-to-have.
3. **User-Centric**: Frame everything from the user's perspective.
4. **Measurable**: Include specific metrics and acceptance criteria where meaningful. Do not invent metrics to fill the table.
5. **No Implementation**: Focus on WHAT, not HOW. No technical solutions.
6. **Honest Constraints**: Only list constraints that are definitively known to apply. Do not invent or speculate about constraints that may not hold — an uncertain constraint is an assumption, not a constraint.
7. **No scope creep**: Only document requirements explicitly stated or strongly implied by the raw requirements. Do not add "nice to have" features that weren't asked for.

## Markdown Formatting

Output must be valid markdown. For tables:
- Every row must start AND end with `|`
- All rows must have the same number of columns
- Include separator row after header: `|---|---|---|`

Example:
```markdown
| ID | Requirement | Priority |
|----|-------------|----------|
| FR-001 | Description | MVP |
```

## Quality Checklist

Before returning the PRD, verify:

- [ ] Executive summary is concise (2-3 sentences)
- [ ] Problem statement clearly articulates the pain point
- [ ] At least 1 user persona defined with goals and pain points
- [ ] All functional requirements have acceptance criteria
- [ ] Success metrics are quantifiable with specific targets
- [ ] Scope boundaries clearly defined (in/out of scope)
- [ ] Risks have mitigation strategies
- [ ] No technical implementation details included
- [ ] Relevant target repositories were inspected, or unavailable access is captured as an assumption or open question

## Output Format

Follow the structure in `skills/default/generate-prd/prd-template.md` exactly.
