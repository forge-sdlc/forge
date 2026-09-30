Please create a Product Requirements Document from the following raw requirements:

{raw_requirements}

Additional context:
{context}

## Repository selection

Select every repository affected by this PRD from `available_repos`. Your structured response must return these exact names in `repositories`; do not invent a repository or leave the list empty.

Before writing the PRD, inspect every target repository identified in the raw requirements or additional context using available GitHub, repository, or filesystem tools. Ground the PRD in relevant repository guidance, documentation, existing user-visible behavior, terminology, tests, integrations, and confirmed constraints. Keep this exploration focused on product requirements; do not prescribe an implementation or turn the PRD into a technical plan. If a target repository is inaccessible, capture repository-dependent uncertainty as an assumption or open question instead of inventing details.

Generate a comprehensive, well-structured PRD following the instructions provided.

Return the enforced structured response with `content` containing the complete
Markdown document and `repositories` containing the selected exact repository
names. No narration belongs inside the document.
