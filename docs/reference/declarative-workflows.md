# Declarative workflows

Forge project administrators can compose the nodes and state profiles shipped with Forge into
project-specific workflows. Definitions are selected by Jira label, validated before compilation,
and compiled into LangGraph graphs at runtime. They cannot import Python or define expressions.

This is project-level configuration, not a plugin system. A definition controls flow; trusted
Forge code controls authority. To add a provider adapter, a station, a node, or an effect executor,
use the [core-maintainer extension path](../architecture/control-plane.md#core-maintainers-trusted-capabilities).

## Author and publish

Create a YAML file locally:

```yaml
apiVersion: forge/v1
kind: Workflow
metadata:
  name: prd-only
  revision: 1
  description: Generate a PRD and wait for approval
spec:
  state: feature
  entry: generate_prd
  steps:
    generate_prd:
      next: prd_approval_gate
    prd_approval_gate:
      route: route_prd_approval
      branches:
        generate_spec: __end__
        regenerate_prd: generate_prd
        answer_question: answer_question
        __end__: __end__
    answer_question:
      next: prd_approval_gate
```

Validate and publish it:

```bash
forge workflow validate workflow.yaml
forge workflow publish MYPROJ workflow.yaml
```

Before authoring a definition, inspect the supported catalog and before activating a revision,
inspect the resulting topology and migration impact:

```bash
forge workflow catalog feature
forge workflow validate workflow.yaml --json
forge workflow render workflow.yaml
forge workflow diff previous.yaml workflow.yaml
forge workflow simulate-migration previous.yaml workflow.yaml instances.json
```

Publishing stores canonical JSON in the `forge.workflow.prd-only` Jira project property. Jira
requires the credentials used by the command to have global or project administration permission.
The canonical value must fit Jira's 32,768-byte project-property limit.

Apply `forge:workflow:prd-only` to a ticket to select the workflow. With no such label, Forge uses
its built-in ticket-type routing. Multiple workflow labels, missing definitions, or invalid
definitions block execution instead of silently falling back.

## Format

The checked-in built-in definitions are canonical JSON because that is the exact artifact Forge
pins and stores. They are not intended to be read as raw topology. Render one as Mermaid or as a
compact process manifest instead:

```bash
forge workflow render src/forge/workflow/declarative/definitions/feature.json
forge workflow render src/forge/workflow/declarative/definitions/feature.json --format json
```

Authors may use YAML, as in the example above; publishing converts it to canonical JSON. In either
format, the fields that describe the process are `spec.entry` and `spec.steps`. Each step declares
either a fixed `next` step or a named `route` with possible `branches`.

Repository users can ask a compatible coding agent to use the generic
`.agents/skills/forge-workflow-authoring` skill to create, explain, change, or review a definition.
The skill authors YAML and uses Forge's validator, renderer, diff, and migration simulation rather
than asking users to edit canonical JSON directly.

- `metadata.name` is lowercase and becomes both the property and label suffix.
- `metadata.revision` must increase whenever content changes.
- `spec.state` is `feature`, `bug`, or `task_takeover` and controls the available node catalog.
- Each step name is a canonical, registered Forge node. A step has either `next` or `route` with a
  complete branch map. Use `__end__` to stop the current invocation.
- Node kind, station contract, effect authority, mandatory policies, observation handling, and
  precondition contracts are owned by the trusted state-profile catalog. They are not workflow
  authoring fields. Older pinned definitions containing this metadata remain readable.
- Exceptional commands such as `/forge rebase` execute through the command-operation boundary;
  they are not lifecycle steps and do not add branches to the process graph.
- `retryBound`, `dynamicRoute`, joins, and concurrency remain in the definition because they
  change how the flow executes. A dynamic router's possible targets are capabilities of its
  trusted implementation and are derived from the catalog rather than repeated in the workflow.
- Graphs may contain a cycle only when it crosses an approved human/CI pause boundary.
- A new instance pins the selected definition's name, revision, digest, and canonical artifact.
  Publishing or activating a newer revision does not silently change an active instance.

To move a pinned instance when a newer revision removes or renames its saved node, add an explicit
migration mapping and run compatibility simulation before activation:

```yaml
spec:
  resume:
    fromRevisions:
      "1":
        old_gate: replacement_gate
```

State-profile changes, revision rollback, and content changes without a revision increment are
rejected. Published revisions are immutable and retained for pinned instances. Removing an active
pointer prevents new selection but does not mutate an existing checkpoint.

## Operational safeguards

Definitions are strict and unknown fields are rejected. Runtime reads JSON rather than YAML, all
nodes and routers come from a static allowlist, unreachable nodes and unguarded cycles are rejected,
and executions are limited to 100 LangGraph transitions per invocation and 500 transitions per
checkpoint lifetime. Existing node-level repository restrictions and sandboxing continue to apply.
Run `forge workflow catalog feature` (or `bug`/`task_takeover`) to inspect the registered nodes,
routers, station contracts, mandatory policies, observation behavior, and effective effect
authority. This derived metadata is inspectable but is not copied into workflows.

Allowlisted nodes may also carry built-in precondition contracts. Forge evaluates these before
running a node and records decisions in `precondition_history`. Contracts are shared with built-in
graphs: workspace setup requires a resolved repository, pull-request creation requires a repository
and workspace, and CI evaluation requires an existing pull request. Missing structural inputs block
before the node performs external side effects.

Lifecycle capabilities are tri-state. An absent capability preserves compatibility with older
state; an explicit `true` or `false` value is authoritative. This permits safe optional PR and CI
stages once implementation has durably recorded whether code changes and a PR are expected.

For taskless execution, use the allowlisted `implement_work` node after `setup_workspace`. It
resolves implementation input in descending specificity: the current Jira Task, a pending Task for
the current repository, repository-specific Epic plans, a general plan, specification, RCA, PRD,
then the root ticket. More general artifacts remain supporting context rather than replacing the
selected work unit. The resolution, artifact digests, and internal work-unit identity are persisted
in the checkpoint.

Use these commands to inspect definitions:

```bash
forge workflow catalog feature
forge workflow list MYPROJ
forge workflow show MYPROJ prd-only
forge workflow show-history MYPROJ prd-only
```

## Configuration boundary

Project authors can configure these flow-level choices:

- the built-in state profile (`feature`, `bug`, or `task_takeover`);
- registered steps, fixed edges, router branches, joins, dynamic fan-out, retry
  bounds, and concurrency; and
- revision/resume mappings for explicitly migratable saved positions.

The trusted catalog, not a project definition, owns node kind, station contract,
effect operations, required and mandatory policies, observation policy,
preconditions, provider credentials, and external command handling. A definition
cannot grant a node permission to push a branch, create a Jira issue, bypass an
approval, or make arbitrary network, shell, or Python calls.

## Safe revision checklist

1. Start from a built-in definition with the same state profile.
2. Run `forge workflow catalog STATE` and use only listed nodes and routers.
3. Increment `metadata.revision` for every content change.
4. Validate and render the candidate definition.
5. Diff it against the active revision.
6. When a saved node changes, add `spec.resume.fromRevisions` and run migration
   simulation against representative active instances.
7. Publish only after reviewing the resulting canonical JSON and migration
   result. Existing tickets remain pinned to their prior definition unless a
   declared migration applies.
