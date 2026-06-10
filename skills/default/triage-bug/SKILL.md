---
name: triage-bug
description: Evaluate a bug ticket for completeness before starting analysis. Use when triaging bug reports to determine if enough information is present to begin investigation.
---

# Bug Triage Skill

Determine whether a bug ticket contains enough information for a developer to begin investigating. You are **not** deciding whether the bug is fully understood — that is what analysis is for. Triage only blocks tickets where the absence of information makes it literally impossible to start looking.

## Evaluation Philosophy

**Default to passing.** Block a ticket only when a field is completely absent AND that absence genuinely prevents starting an investigation. When in doubt, pass.

The reporter may not have access to every piece of information — they might not know the affected version, might not have logs, or might not be able to reproduce the issue reliably. **That is normal.** Triage should never block a ticket just because the reporter lacks information they reasonably cannot provide. The purpose of the investigation stage is to fill in those gaps.

A field is satisfied if **any** of the following are true:
- The information is present, even if brief or informal
- The information is clearly inferable from context
- An experienced engineer could construct it from what is provided
- The reporter states they don't have this information, or it's unclear — this counts as satisfied because the investigation can proceed without it
- The nature of the bug makes the field inapplicable
- The reporter describes the problem well enough that an engineer could start looking, even without this specific field

## Fields to Evaluate

1. **steps_to_reproduce** — How to trigger the bug. Satisfied if the mechanism or conditions are described even vaguely. Also satisfied if the reporter says they can't reproduce it reliably or it happens intermittently — that is useful information, not a gap.

2. **expected_vs_actual** — What happened vs. what should have happened. Satisfied if the reporter describes either side — "X is broken" implies the expected behavior is "X works."

3. **environment** — Runtime or infrastructure context. Almost always satisfied. Satisfied for any internal codebase bug, for bugs where the environment is obvious from context, or when the reporter doesn't know the environment. Never block on this alone.

4. **affected_versions** — Which version exhibits the bug. Almost always satisfied. Satisfied for internal projects, unreleased software, or when the reporter simply doesn't know the version. Never block on this alone.

5. **error_output** — Logs, stack traces, or error messages. Satisfied if the reporter states no error output exists, doesn't mention logs, or says they don't have access to logs. The absence of error output is itself a data point.

6. **affected_component** — Which part of the system is involved. Satisfied if the reporter names any layer, service, area, or even just a user-facing feature. Also satisfied if the bug description makes the component obvious. Never require file-level or class-level specificity.

7. **disambiguating_context** — Only flag this if the description is so generic that completely different bugs could plausibly match it. Almost never needs to be flagged.

## Decision Rule

Ask yourself: **"Could an engineer start investigating this bug with what's here?"** If yes, output `sufficient`. The investigation itself will uncover details the reporter couldn't provide.

Only block when the ticket is so bare that an engineer would have zero starting direction — no description of the problem, no indication of what's wrong, nothing to search for in the codebase.

## Output

The output format is a forge protocol constraint — do not change it:
- If the ticket is sufficient to start an investigation: output only the bare string `sufficient`
- If one or more fields are genuinely missing and block investigation: output a bare JSON array of field names, e.g. `["steps_to_reproduce"]`

No markdown, no code fences, no explanation.
