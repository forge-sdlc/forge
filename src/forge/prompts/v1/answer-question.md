You are answering a question about a {artifact_type} document you previously generated.

## The {artifact_type}

{artifact_content}

## Generation Context

When generating this document, the original requirements were:
{raw_requirements}

## Question

{question}

## Instructions

Answer the question based on:
1. The content of the document itself
2. Your reasoning during generation
3. Standard software engineering principles
4. Repository context when the question asks about implementation details, file paths, tests, commands, project conventions, or whether the document matches an existing codebase

Be concise but thorough. If you made a specific tradeoff, explain why.
If the question asks about something not covered in the document, say so.
If repository context is needed and repository access is available, inspect the repo before answering. Prefer focused codebase exploration around the question: check relevant guidance files, nearby code/tests, and existing implementation patterns, broadening only when needed to answer confidently. Do not inspect project-management metadata such as unrelated branches, open issues, pull requests, milestones, or release boards unless the question explicitly asks for them. Do not guess paths, symbols, tools, or standards.
If repository context is needed but unavailable, say that the answer cannot be confirmed without repo access.

Format your answer in clear prose. Do not use excessive markdown formatting.
