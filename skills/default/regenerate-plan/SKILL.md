---
name: regenerate-plan
description: Revise a bug fix implementation plan based on reviewer feedback. Use when the team has reviewed a plan and requested changes before approving.
---

# Bug Fix Plan Revision Skill

You have produced an implementation plan that the engineering team has reviewed. They have provided feedback. Your job is to revise the plan to address their feedback with minimum change — do not regenerate from scratch unless the feedback requires it.

## Instructions

1. **Address each piece of feedback explicitly** — if the feedback says "add migration steps", add them. If it says "scope is too broad", narrow it.

2. **Preserve what was not objected to** — only change the parts the feedback targets.

3. **Re-verify file paths and function names** if the feedback raises doubts about them.

4. **Keep `repo:<owner>/<name>` tags** for repositories whose scope is unchanged. Update or add tags if the feedback changes the repository scope.

5. **Write the revised plan to `.forge/plan.md`** in the same structured Markdown format. Do not write any other files. Do not commit.
