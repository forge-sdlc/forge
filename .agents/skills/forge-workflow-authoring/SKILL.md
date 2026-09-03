---
name: forge-workflow-authoring
description: Create, explain, change, or review Forge declarative workflow definitions. Use for Forge workflow YAML/JSON, topology, step permissions, validation failures, revisions, and migration planning.
---

# Forge Workflow Authoring

Help the user express a Forge process as readable YAML. Treat canonical JSON as generated publication storage, not as the human authoring format.

## Start here

1. Read [references/workflow-format.md](references/workflow-format.md).
2. For a new workflow, copy [assets/workflow.yaml](assets/workflow.yaml). For a change, start from the active definition or the closest built-in workflow and convert it to YAML if needed.
3. Establish the intended stages, decisions, loops, human pauses, and external commands before editing fields.
4. Run `forge workflow catalog STATE` and use only the nodes and routers it reports. Never invent catalog names.
5. Keep the definition flow-only. Do not add node kinds, station contracts, effect capabilities, required or mandatory policies, extension declarations, observation policies, or external-entry flags. Forge derives and enforces those concerns from its trusted catalog and publication policy.
6. Validate and render before presenting the result:

   ```bash
   forge workflow validate WORKFLOW.yaml
   forge workflow render WORKFLOW.yaml
   ```

Explain the rendered process in plain language when the user is trying to understand an existing definition.

## Changing an existing workflow

Increment `metadata.revision`, preserve the workflow name, and compare revisions:

```bash
forge workflow diff PREVIOUS.yaml CURRENT.yaml
```

If saved nodes were renamed or removed, add explicit `spec.resume.fromRevisions` mappings. When checkpoint snapshots are available, verify them:

```bash
forge workflow simulate-migration PREVIOUS.yaml CURRENT.yaml INSTANCES.json
```

Do not claim migration safety based only on successful validation.

## Review expectations

Before publication, verify:

- all transitions and router outcomes resolve to existing steps or `__end__`;
- expected human and CI pause points remain present;
- cycles cross an approved pause boundary;
- exceptional commands such as PR rebasing are absent from graph topology;
- revision and resume mappings protect in-flight instances.

Report review problems with the affected step and a concrete correction. Distinguish topology, execution-policy, and migration findings; catalog and governance concerns are Forge implementation findings, not fields to add to the workflow.

Do not publish, activate, roll back, or delete a workflow unless the user explicitly requests that external change. If asked to publish, validate and review first, then use a meaningful actor and reason.
