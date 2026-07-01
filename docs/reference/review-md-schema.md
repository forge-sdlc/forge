# review.md Schema

This document specifies the file format and validation rules for `review.md` files used to configure skill-specific review loops.

## Overview

A `review.md` file configures how an AI reviewer evaluates the output of a skill before proceeding. It specifies retry limits and provides review instructions that guide the reviewer's analysis.

## File Location

### Naming Convention

The file must be named exactly `review.md` (lowercase) and placed alongside `SKILL.md` in a skill directory:

```
skills/
├── default/
│   └── local-code-review/
│       ├── SKILL.md          # Skill definition
│       └── review.md         # Review configuration
└── myproject/
    └── local-code-review/
        ├── SKILL.md          # Project-specific skill override
        └── review.md         # Project-specific review override
```

### Override Precedence

Review configuration follows the same precedence rules as skills:

| Priority | Path | Description |
|----------|------|-------------|
| 1 (highest) | `skills/{project}/{skill-name}/review.md` | Project-specific override |
| 2 | `skills/default/{skill-name}/review.md` | Default configuration |

The project key is extracted from the Jira ticket key and lowercased. For ticket `MYPROJECT-123`, Forge checks `skills/myproject/` first.

**Examples:**

- Ticket `AISOS-456` with skill `local-code-review`:
  1. Check `skills/aisos/local-code-review/review.md`
  2. Fall back to `skills/default/local-code-review/review.md`

- If neither path exists, the skill runs **without a reviewer**.

## File Format

The `review.md` file uses YAML frontmatter followed by Markdown prose, consistent with `SKILL.md` files:

```markdown
---
max_retries: 5
---

# Review Instructions

Verify that the implementation meets all acceptance criteria...
```

### YAML Frontmatter Schema

The frontmatter section is delimited by `---` markers and contains YAML key-value pairs:

| Field | Type | Required | Default | Description |
|-------|------|----------|---------|-------------|
| `max_retries` | `int` | No | `3` | Maximum retry attempts after a REJECTED verdict |

**Frontmatter rules:**

- The opening `---` must be the first line of the file (no leading whitespace or blank lines)
- The closing `---` must appear on its own line
- Unknown fields are silently ignored (forward compatibility)
- Field values must match the expected type; invalid types trigger a warning and use defaults

**Priority for `max_retries`:**

1. Frontmatter value (if valid integer)
2. `AUTO_REVIEW_MAX_RETRIES` environment variable (if set and valid)
3. Built-in default: `3`

### Prose Body

Everything after the closing `---` delimiter is treated as reviewer instructions. This Markdown content is passed to the reviewer agent as its system prompt.

**Best practices for instructions:**

- Define what constitutes approval vs. rejection
- Specify quality criteria and acceptance thresholds
- Reference any artifacts the reviewer should examine (e.g., `.forge/` files, test output)
- Keep instructions focused on review criteria, not implementation details

**Example:**

```markdown
---
max_retries: 3
---

# Code Review Checklist

Evaluate the implementation against these criteria:

## Required (reject if missing)
- [ ] All acceptance criteria from the task are addressed
- [ ] No obvious security vulnerabilities introduced
- [ ] Tests cover the new functionality

## Recommended (note but don't reject)
- [ ] Code follows existing patterns
- [ ] Documentation updated if needed

## Output Format

End your review with exactly one of:
- `APPROVED` — all required criteria pass
- `REJECTED` — followed by specific feedback for the implementer
```

## Edge Cases

### Empty File

A `review.md` file with zero bytes or only whitespace:

- Reviewer spawns with **no instructions** (empty prompt)
- Uses default `max_retries: 3` (or env var if set)

### Frontmatter-Only File

A file with frontmatter but no prose body:

```markdown
---
max_retries: 5
---
```

- Reviewer spawns with **no instructions** (empty prompt)
- Uses the specified `max_retries` value

### Missing Frontmatter

A file without `---` delimiters:

```markdown
Review the code for bugs and style issues.
```

- Entire file content becomes reviewer instructions
- Uses default `max_retries: 3` (or env var if set)

### Malformed YAML

If the frontmatter contains invalid YAML:

```markdown
---
max_retries: "not a number"
---

Instructions here...
```

- A warning is logged
- Default `max_retries` is used
- Prose body is still extracted and used as instructions

### No review.md Found

If no `review.md` exists in either the project or default skill directory:

- **No reviewer is spawned** — the skill output is accepted without review
- This is the expected behavior for skills that don't require review

## Review Cycle Output

Each review cycle writes its results to:

```
.forge/{step-name}/review_cycle_N.json
```

Where:
- `{step-name}` is the workflow step (e.g., `implement_task`, `local_code_review`)
- `N` is the 1-indexed cycle number

**Example:** `.forge/implement_task/review_cycle_1.json`

**JSON schema:**

```json
{
  "cycle": 1,
  "max_cycles": 3,
  "verdict": "rejected",
  "feedback": "Missing test coverage for edge case...",
  "skill": "local-code-review",
  "elapsed_seconds": 45.2,
  "timestamp": "2024-01-15T10:30:00Z"
}
```

| Field | Type | Description |
|-------|------|-------------|
| `cycle` | `int` | Current cycle number (1-indexed) |
| `max_cycles` | `int` | Maximum cycles allowed (from `max_retries + 1`) |
| `verdict` | `string` | `"approved"` or `"rejected"` |
| `feedback` | `string` | Reviewer feedback (empty string if approved) |
| `skill` | `string` | Name of the skill that performed the review |
| `elapsed_seconds` | `float` | Time taken for this review cycle |
| `timestamp` | `string` | ISO 8601 UTC timestamp of cycle completion |

## Validation Summary

| Condition | Behavior |
|-----------|----------|
| File missing | No reviewer spawned |
| File empty | Reviewer spawns with no instructions, default retries |
| No frontmatter | Entire content used as instructions, default retries |
| Empty frontmatter | Prose used as instructions, default retries |
| Malformed YAML | Warning logged, prose used, default retries |
| Invalid `max_retries` type | Warning logged, default used |
| Unknown frontmatter fields | Silently ignored |

## See Also

- [Authoring Skills](../skills/authoring.md) — how to create and customize skills
- [Configuration](config.md) — environment variables including `AUTO_REVIEW_MAX_RETRIES`
