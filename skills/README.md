# Forge Skills

Agent skills for the Forge SDLC orchestrator.

## Directory Layout

```
skills/
├── default/          # Stack-agnostic defaults used by all projects
│   ├── analyze-ci/
│   ├── generate-prd/
│   └── ...
├── openshift/        # OpenShift team overrides (example)
│   └── analyze-ci/
└── {project}/        # Per-project overrides (Jira project key, lowercase)
    └── {skill-name}/
        └── SKILL.md
```

## How Overrides Work

Skills are resolved per-ticket by Jira project key:
- `AISOS-123` → looks for `skills/aisos/` first, falls back to `skills/default/`
- A team only needs to provide the skills they want to customize
- All other skills are served automatically from `skills/default/`

## Writing a Skill Override

Create `skills/{project-key-lowercase}/{skill-name}/SKILL.md`.

The skill name must match the directory name and an existing skill name in `skills/default/`.

### What belongs in a skill (Domain content)

- Document content and structure within the runtime output schema
- Stage-specific decision criteria and evidence requirements
- Quality checklists and acceptance criteria
- Technology-specific conventions (CI tooling, test frameworks, language idioms)
- Failure categorizations relevant to your stack
- References to `.forge/` inter-skill interface files (e.g., `.forge/fix-plan.md` written by `analyze-ci` and consumed by `fix-ci`)

### What does NOT belong in a skill (Plumbing)

The following belong in Forge system prompts, not skills. Do not duplicate them:

- Execution permissions, structured response schemas, git commit rules or git hygiene
- `.forge/handoff.md` update instructions
- Workspace setup or task context loading
- Label management or workflow state transitions (handled programmatically)

### Skill file format

```markdown
---
name: {skill-name}
description: What this skill does and when to use it.
---

# Skill Title

...skill content...
```

The `name` field must match the directory name exactly.

## Default Skill Quality Bar

Skills in `skills/default/` must be useful to any software project regardless of stack.
Before adding stack-specific content to a default skill, ask:
"Would this make sense for a Java microservices project? A Rust CLI? A Python pipeline?"

If the answer is no, put it in a project-specific override instead.

## Stage contracts and resources

The runtime owns execution mode, repository scope, output envelope, and side-effect
permissions. Overrides may specialize content and checks, but cannot change those
contracts. Analysis writes only its requested artifacts; qualitative reviewers have
file/search access without shell or mutation tools. Implementation and conflict
resolution have distinct commit rules. The latter never commits automatically.

Resolve supporting files relative to the effective SKILL.md. Container mount paths
and installed project override locations differ from the source checkout. Do not
hard-code `skills/default/` into resource references.

Keep each skill focused on its trigger, required inputs, decision procedure,
completion evidence, and missing-input behavior. Use existing requirement IDs to
trace coverage. Apply risk-specific checks only when relevant; unknown facts stay
unknown. One Epic/Task is valid when it covers the scope. Tests and affected docs
belong with each behavior change; documentation-only work does not require an
artificial test file.

Record implementation validation in `.forge/validation.md` with actual commands,
results, tested revision/state, and limitations. Reviewers consume that evidence;
a successful container exit or commit-message marker is not proof that tests ran.
