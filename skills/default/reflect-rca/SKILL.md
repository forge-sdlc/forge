---
name: reflect-rca
description: Validate an RCA for correctness and evidence quality before presenting it to the engineering team. Use after analyze-bug produces an rca.json.
---

# RCA Reflection Skill

Validate the root cause analysis against seven criteria. You have access to the codebase — use it to verify claims.

## Validation Criteria

Check each criterion. For any failure, write a specific, actionable finding that names the file, function, commit, or option that is wrong.

1. **File and function exist** — Verify the named file and function actually exist at the stated location using `find` and `grep`. If the code location is wrong, flag it.

2. **Mechanism is plausible** — Given the code, is the stated failure mechanism actually possible? Does the code do what the RCA claims?

3. **Options are distinct** — Are the fix options genuinely different approaches, or paraphrases of each other? Two options that differ only in wording are not distinct.

4. **No unexplained gaps** — Is there a complete, unbroken chain from the stated trigger to the observed symptom? Flag any missing links.

5. **Multiple hypotheses were considered** — The `hypothesis_log` should include at least one `"rejected"` candidate unless the bug is trivially isolated. If no candidates were rejected, verify the bug is genuinely trivial.

6. **Git history was consulted** — Does `introduced_in` contain a real commit hash? Verify using `git show <commit>` that it exists and is plausible. If the hash looks fabricated, flag it.

7. **Confidence level is justified** — If confidence is `Low` or `Medium`, the rationale must name specific missing evidence. Vague rationales are insufficient.

## Output

The output format is a forge protocol constraint — do not change it:
- All criteria pass: output only the word `VALID`
- Any criterion fails: output a numbered list of gaps, one item per failure

No other text. Do not explain that you are outputting VALID.
