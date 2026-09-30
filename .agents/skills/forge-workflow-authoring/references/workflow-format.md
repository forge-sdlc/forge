# Forge workflow format

## How to read it

A definition has four important parts:

- `metadata.name` is the stable workflow identity; `metadata.revision` increases with every change.
- `spec.state` chooses the `feature`, `bug`, or `task_takeover` catalog.
- `spec.entry` names the first ordinary step.
- `spec.steps` maps registered node names to their transitions.

Reusable `Subworkflow` definitions use the same registered nodes and may end a transition
at `@exit/name`. A caller binds each exit through `spec.includes`; it may also include a
complete workflow with `returnTo` for normal completion. Use `source: builtin` or
`source: project`. Project references resolve to active definitions in the caller's Jira
project. Nodes remain canonical and cannot collide. New runs pin the fully expanded graph;
active runs keep their existing pin.

Start at `entry`. Follow `next` for a fixed transition. At a step with `route`, follow every target in `branches`. A branch key is a possible router result; its value is the next step. A step may instead use ordered `cases` with predicates over catalog facts and a required `otherwise` target. `__end__` stops the current invocation and is not itself a declared step. In an included full workflow, fixed `next: __end__` returns to the caller's `returnTo` target; routed and conditional `__end__` still stop the invocation. Include a fixed completion step or use a `Subworkflow` with named exits for conditional returns.

Use `forge workflow render FILE` instead of tracing a large definition manually.

## Step shapes

A fixed transition:

```yaml
generate_prd:
  next: prd_approval_gate
```

A routed transition:

```yaml
prd_approval_gate:
  route: route_prd_approval
  branches:
    generate_spec: __end__
    regenerate_prd: generate_prd
    answer_question: answer_question
    __end__: __end__
```

Every possible static router result must be represented in `branches`.

A conditional transition uses the first matching case, then `otherwise`:

```yaml
ci_evaluator:
  cases:
    - when: {fact: ci.status, op: in, value: [failed, blocked, no_prs]}
      next: escalate_blocked
    - when: {fact: ci.status, op: equals, value: fixing}
      next: attempt_ci_fix
  otherwise: human_review_gate
```

Inspect available facts with `forge workflow catalog STATE`. Supported predicates are `equals`,
`in`, `isNull`, and bounded `all`, `any`, and `not` groups. Protected gates require a trusted router.

Dynamic fan-out uses `dynamicRoute: true` and an explicit `maxConcurrency`. Forge derives the router's permitted destinations from the trusted catalog; inspect them with `forge workflow catalog STATE`. A join uses `join: all` or `join: any`. Copy the applicable shape from a validated built-in definition instead of reconstructing advanced routing from memory.

## Separation of concerns

The workflow owns topology and flow-level execution choices. `retryBound`, `maxConcurrency`, and join behavior remain valid because they change how the graph advances.

Do not author step `kind`, `stationContract`, `stationContractVersion`, `requiredPolicies`, `allowedEffects`, `externalEntry`, `observationPolicy`, `mandatoryPolicies`, or `extensionPoints`. The top-level `kind` is `Workflow` or `Subworkflow`. Forge derives node identity, authority, reconciliation, and mandatory governance from the selected state profile. Exceptional commands such as PR rebasing execute through the command-operation boundary and do not appear as workflow steps. Older pinned definitions containing catalog metadata remain readable for compatibility.

Run `forge workflow catalog STATE` when you need to inspect the derived node metadata; do not copy that metadata into the workflow.

## Revision compatibility

Running instances pin a definition, so publishing a revision does not silently move them. If a saved position was renamed or removed, map it explicitly:

```yaml
spec:
  resume:
    fromRevisions:
      1:
        old_gate: replacement_gate
```

Do not reuse a revision with changed content or assume a valid new graph can resume old checkpoints.

## Commands and outputs

```bash
forge workflow validate workflow.yaml
forge workflow catalog feature
forge workflow validate workflow.yaml --json
forge workflow render workflow.yaml
forge workflow render workflow.yaml --format json
forge workflow diff previous.yaml workflow.yaml
forge workflow simulate-migration previous.yaml workflow.yaml instances.json
```

Use `--project-key PROJECT` when a source file includes an active project definition. For a
historical diff or migration check, use the exact expanded artifacts pinned by the instances;
resolving old source files against today's active dependencies can change the graph being compared.

For a workflow, `validate --json` emits the resolved canonical runtime JSON and
`render --format json` emits a process manifest. For a subworkflow, render JSON emits the
resolved subworkflow definition with its exit targets.
