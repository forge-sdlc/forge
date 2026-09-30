# User-defined workflow nodes

A workflow step can embed a template or refer to an active node published for the same Jira project. Each step has its own instance name. Project references are resolved to the active immutable node revision when the workflow starts; the resolved template and dependency digest are stored in the pinned workflow definition.

For example, add this step to a feature workflow after specification generation:

```yaml
review_artifact:
  node:
    source: project
    name: artifact-review
  cases:
    - when: {fact: node.review_artifact.outcome, op: equals, value: ready}
      next: generate_tasks
  otherwise: generate_spec
```

The example [artifact-review-node.yaml](artifact-review-node.yaml) selects only the ticket key and specification content. `agent-assessment-v1` makes one structured model call without agent tools or repository access. Its response must contain one declared outcome and a summary of at most 1,000 characters. Missing required input, model failure, or an invalid response blocks the run. `decision-v1` evaluates ordered workflow predicates locally and requires a declared fallback outcome.

Assessment nodes use the `user_node_assessment` model-policy key, which requires
`structured_output` and does not require tools. A failed node remains the resume
position so `forge:retry` reruns that assessment.

```sh
forge node catalog
forge node validate docs/examples/workflow-nodes/artifact-review-node.yaml
forge node publish DEMO docs/examples/workflow-nodes/artifact-review-node.yaml
forge node activate DEMO artifact-review 1
forge node show DEMO artifact-review
```

Use `forge node history DEMO artifact-review`, `forge node list DEMO`, and `forge node rollback DEMO artifact-review 1 --expected-active-digest <digest>` to inspect and manage revisions. Activation is separate from publication. The `--expected-active-digest` argument is required when replacing an active revision.

Activation and rollback validate active workflow and subworkflow consumers against
the candidate node revision. A revision that removes an outcome used by a consumer
is rejected before the active node changes.

Outcomes are available to conditional edges as `node.<step>.outcome`. In a parallel branch the default scope reads that branch's result. A step with `join: all` can use `scope: any` or `scope: all` in its predicate to test all recorded branch outcomes. A missing branch result blocks evaluation. Template outcomes, instructions, selected inputs, and resolved revision are part of workflow change detection.
