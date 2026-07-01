# Authoring Skills

This guide covers how to write a skill override for your team's Jira project.

## Quick Start

Create `skills/{your-project-key-lowercase}/{skill-name}/SKILL.md`:

```
skills/
└── myteam/
    ├── analyze-ci/
    │   └── SKILL.md       ← your CI failure categories and tooling
    └── generate-prd/
        ├── SKILL.md       ← your PRD process
        └── prd-template.md
```

The project key is the prefix of your Jira issue keys, lowercased. For `MYTEAM-123`, the directory is `skills/myteam/`.

You only need to include the skills you've customized. Forge falls back to `skills/default/` for everything else.

## Skill File Format

Every `SKILL.md` starts with YAML front matter:

```markdown
---
name: analyze-ci
description: Categorizes CI failures for the MYTEAM project's OpenShift-based CI.
---

# Analyze CI

...skill content...
```

The `name` field must match the directory name exactly (e.g., `analyze-ci`).

## What Belongs in a Skill

Skills are for **domain content** — the reasoning, formats, and conventions specific to your project:

- Output format and document structure
- Process steps and analysis frameworks
- Quality checklists and acceptance criteria
- Technology-specific conventions (CI tooling, test frameworks, language idioms)
- Failure categorizations relevant to your stack
- References to `.forge/` inter-skill interface files (e.g., `.forge/fix-plan.md` written by `analyze-ci` and consumed by `fix-ci`)

## What Does NOT Belong in a Skill

These belong in Forge system prompts and should not be duplicated in skills:

- Git commit rules or git hygiene
- `.forge/handoff.md` update instructions
- Workspace setup or task context loading
- Label management or workflow state transitions (handled programmatically)

Duplicating plumbing in skills causes conflicts when Forge's system prompts are updated.

## Quality Bar for Default Skills

Skills in `skills/default/` must work for **any** software project regardless of stack.

Before adding content to a default skill, ask:
> "Would this make sense for a Java microservices project? A Rust CLI? A Python pipeline?"

If the answer is no, put it in a project-specific override instead.

## Example: Overriding `analyze-ci`

```markdown
---
name: analyze-ci
description: CI failure analysis for OpenShift e2e tests using Prow and Sippy.
---

# Analyze CI — OpenShift

## Failure Categories

### Infrastructure Failures (skip with /forge skip-gate)
- Cloud quota exhaustion
- OVN-Kubernetes flaky setup
- Node not-ready on cluster bootstrap

### Test Code Failures (fix required)
- e2e test assertion failures
- Import errors or compilation failures
- Missing test fixtures

## Sippy Integration

Check https://sippy.dptools.openshift.io/ to determine if a failing test has a known flake rate above 5%. If so, categorize as infrastructure.

## Fix Plan Format

Write `.forge/fix-plan.md` with:
1. Failure category
2. Root cause (specific line and file if applicable)
3. Proposed fix approach
```

## Auto-Review

Skills can include a `review.md` file alongside `SKILL.md` to enable automatic quality gates. When present, Forge spawns a reviewer agent after the skill completes, evaluating the output against criteria you define.

```
skills/
└── myteam/
    └── implement-task/
        ├── SKILL.md
        └── review.md      ← reviewer instructions
```

The reviewer issues APPROVED or REJECTED verdicts. On rejection, the skill re-runs with feedback injected — up to `max_retries` attempts (default: 3).

See the [Auto-Review Guide](../guide/auto-review.md) for configuration options and writing effective review instructions.

## Using your skills with Forge

See [Customize Forge for your project](../dev/contributing.md#customize-forge-for-your-project) for how to point a Jira project at your skills repo using `forge project-setup` and the skill installer.
