---
name: regenerate-plan
description: Revise a bug fix implementation plan based on reviewer feedback. Use when the team has reviewed a plan and requested changes before approving.
---

# Bug Fix Plan Revision Skill

You have produced an implementation plan that the engineering team has reviewed. They have provided feedback. Your job is to revise the plan to address their feedback with minimum change — do not regenerate from scratch unless the feedback requires it.

## Instructions

1. **Address each piece of feedback explicitly** — if the feedback says "add migration steps", add them. If it says "scope is too broad", narrow it.

2. **Preserve what was not objected to** — only change the parts the feedback targets.

3. **Re-verify repository grounding** when feedback changes implementation scope, repository scope, files, functions, tests, generated artifacts, validation commands, or project conventions.

4. **Re-verify file paths and function names** if the feedback raises doubts about them.

5. **Prefer focused codebase re-inspection** of details the feedback changes or questions. Reuse grounded details from the original plan when they remain valid, and broaden the search when needed to revise safely. Do not inspect project-management metadata such as unrelated branches, open issues, pull requests, milestones, or release boards unless the feedback explicitly asks for them.

6. **Follow repository standards** discovered from guidance files and existing implementation patterns. If feedback requests a deviation from a repository standard, explain why the revised plan accepts or rejects that deviation.

7. **Preserve nearby patterns** — revised files and tests should stay aligned with nearby source and test patterns unless feedback justifies a change.

8. **Keep `repo:<owner>/<name>` tags** for repositories whose scope is unchanged. Update or add tags if the feedback changes the repository scope.

9. **Write the revised plan to `.forge/plan.md`** in the same structured Markdown format. Do not write any other files. Do not commit.
