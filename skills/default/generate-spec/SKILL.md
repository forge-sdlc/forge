---
name: generate-spec
description: Generate a Technical Specification with behavioral acceptance criteria from a PRD. Use when asked to create a spec, technical specification, or detailed requirements.
---

# Specification Generation Skill

Generate a Technical Specification using the template and guidelines below.

> Return the enforced runtime response schema. Put only the Markdown document in `content`; preserve the required repository selection.

## Core Purpose

A spec translates PRD requirements into **testable behavioral contracts**. It answers "exactly how does this behave?" — not "why are we building it?" (that's the PRD).

**Do not repeat PRD content.** The spec assumes the reader has read the PRD. Skip business context, personas, and rationale — go straight to behavior.

## Size Calibration

Match spec depth to feature complexity. Only include a section if it adds information specific to this feature — not because the template has a slot for it. An empty or generic section is noise.

Ask for each section: "Does this feature have something concrete and specific to say here?" If the answer is no, omit the section.

## Scenario Rules

1. **Ground every scenario in the PRD.** Only write scenarios for functionality explicitly described in the PRD's user stories and functional requirements. Do not add scenarios for functionality not mentioned in the PRD.
2. **Cover all priority levels present in the PRD.** If the PRD contains non-MVP or enhancement requirements, write P2/P3 scenarios for them — do not drop them to keep the spec short. If the PRD has no non-MVP requirements, omit P2/P3 sections entirely rather than inventing scenarios to fill the template.
3. **One scenario per distinct behavior.** Don't create separate scenarios for minor variations — use edge cases within a scenario instead.
4. **For simple features: 2-4 scenarios total** is usually right. More than 6 scenarios for a single config field suggests scope creep.

## Generation Rules

1. **Gherkin Format**: All acceptance criteria must use Given/When/Then.
2. **Testable**: Every requirement must be verifiable through testing.
3. **No Ambiguity**: Use precise language. Avoid "should", "might", "could".
4. **Trace to PRD**: Reference FR/US IDs from the parent PRD where relevant.
5. **Error format matches the stack**: Go/CLI tools use field validation errors, not HTTP error codes. Web APIs use HTTP status codes. Match the error style to the actual implementation.
6. **Conditional coverage**: Address security/trust boundaries for authentication, privileged actions, untrusted input, command execution, file ingestion, or dependency changes. Address retries/idempotency/concurrency for asynchronous work, compatibility/migration/rollback for API or data changes, and accessibility for UI changes when applicable. Ground these behaviors in the requested scope and repository constraints; do not invent product features.
7. **Coverage trace**: Map every in-scope PRD requirement to a scenario or an explicit deferred/blocked decision. Preserve IDs for downstream planning and validation.

## Markdown Formatting

Output must be valid markdown. For tables:
- Every row must start AND end with `|`
- All rows must have the same number of columns
- Include separator row after header: `|---|---|---|`

## Quality Checklist

Before returning the specification:

- [ ] Every scenario is grounded in a PRD requirement (FR/US ID cited or clearly traceable)
- [ ] All P1 scenarios have complete Gherkin acceptance criteria
- [ ] Edge cases are covered within scenarios (not as separate scenarios)
- [ ] Sections not applicable to this feature type are omitted
- [ ] Error messages match the error style of the target language/framework
- [ ] Test scenarios cover all acceptance criteria
- [ ] Open questions are only listed if genuinely unresolved (don't list questions already answered in the PRD)

## Output Format

Resolve the following template relative to this SKILL.md. Follow the structure in `spec-template.md`.
Omit any section that does not apply to this feature type.
