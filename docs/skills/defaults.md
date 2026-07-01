# Default Skills

The `skills/default/` directory contains stack-agnostic skills used by all Forge projects. These are the baseline that project-specific skills override.

## Skill Reference

### `generate-prd`

Generates a Product Requirements Document from a Jira feature ticket.

**Inputs:** Ticket summary, description, reporter, and any existing comments.

**Output format:** Structured PRD including problem statement, goals, non-goals, user stories, constraints, and open questions.

**Template:** `generate-prd/prd-template.md` defines the document structure.

---

### `generate-spec`

Generates a behavioral specification from the approved PRD.

**Output format:** Acceptance criteria using Given/When/Then format, edge cases, and out-of-scope items.

**Template:** `generate-spec/spec-template.md`

---

### `decompose-epics`

Breaks a feature into logical epics — high-level work areas that map to implementation phases.

**Output format:** Numbered epic list with summary, scope, and implementation notes per epic.

**Template:** `decompose-epics/epic-template.md`

---

### `generate-tasks`

Generates granular implementation tasks from approved epics, scoped to individual repositories.

**Output format:** Task list with title, repository target, description, and acceptance criteria. Each task is sized to fit a single container execution.

---

### `implement-task`

Drives the code implementation agent running inside an ephemeral container.

**Scope:** Given a task file at `.forge/task.json`, implement the code, run tests, and commit. No external network access.

**Auto-review:** Includes `review.md` that checks test coverage, error handling, documentation, and debug code before PR creation. See [Auto-Review Guide](../guide/auto-review.md).

---

### `local-code-review`

Reviews the implementation diff against `main` before PR creation.

**Focus:** Breaking changes, test failures, security issues, and spec mismatches. Up to 2 fix passes.

**Auto-review:** Includes `review.md` that validates test suite execution and checks for breaking changes, security issues, and spec alignment. See [Auto-Review Guide](../guide/auto-review.md).

---

### `analyze-ci`

Categorizes CI failures into actionable categories.

**Output:** `.forge/fix-plan.md` with failure category, root cause, and proposed fix approach. This file is consumed by `fix-ci`.

**Categories (default):**
- Infrastructure failure (recommend skip)
- Test code failure (fix required)
- Build failure (fix required)
- Dependency issue (fix required)

---

### `fix-ci`

Implements a CI fix based on the plan from `analyze-ci`.

**Input:** `.forge/fix-plan.md` written by `analyze-ci`.

**Scope:** Targeted fix only — does not refactor or expand scope.

---

### `implement-review`

Handles post-PR-review fix passes when a human reviewer requests changes.

**Input:** GitHub PR review comments.

**Scope:** Addresses each review comment in a focused fix pass.

---

### `review-code`

Reviews the completed PR against the original spec after CI passes.

**Output:** Pass/fail assessment with specific findings. Only flags issues that contradict the spec.

---

### `analyze-bug`

Generates a root cause analysis for a bug ticket.

**Output:** Probable root cause, affected code areas, and proposed fix approach. Posted to the Jira ticket for human approval.

## Overriding a Default

To override any of these for your project, create `skills/{your-project-key}/{skill-name}/SKILL.md`. See the [Authoring Guide](authoring.md).
