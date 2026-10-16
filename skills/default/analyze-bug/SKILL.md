---
name: analyze-bug
description: Investigate bug reports against repository evidence and produce the structured RCA requested by the runtime.
---

# Bug Analysis

Follow the investigation and `.forge/rca.json` contract supplied by the task.
Identify a plausible trigger-to-symptom path, inspect relevant code and history,
and test competing hypotheses when they can discriminate the cause. Record
concrete evidence and uncertainty; do not invent a confirmed root cause or a
rejected hypothesis just to fill a quota.

A blame commit identifies the last edit, not necessarily the introducing defect.
Verify the behavioral change before attributing introduction. Use null for an
unknown commit, PR, date, or code location field, with the reason in confidence
rationale. Distinguish static inspection from an executed reproduction.

Perform reproduction experiments only in a temporary scratch clone/copy. Put the
minimal test source and actual observed results or limitations in the RCA's
reproducibility fields. Do not change the supplied source or commit.

Propose distinct fix options supported by evidence, preserving the selected exact
repository. For an unresolved cause, explain the next discriminating investigation
instead of presenting speculation as an implementation-ready fix.
