# Structured model output

Forge uses schema-enforced final responses for bounded model decisions while leaving the
Deep Agent tool loop unchanged. The runtime first requests the provider-native structured
response strategy. If a provider rejects native schema mode or returns an invalid object,
Forge retries the complete invocation with LangChain's validated tool strategy. It never
silently accepts malformed JSON or falls back to an unvalidated text parser.

## Migrated stages

- Bug and task-takeover triage (`TriageOutput`)
- Epic decomposition (`EpicDecomposition`)
- Task generation (`TaskGeneration`)
- Automated-review triage (`AutomatedReviewTriage`)
- Proposal review-thread classification (`ProposalReviewTriage`)

Narrative PRDs, specifications, implementation plans, PR descriptions, and qualitative
reviews remain Markdown because their primary result is prose rather than a bounded
decision object. CI attribution already crosses a validated file-artifact boundary inside
the sandbox; migrating that separate transport would not remove model-text parsing from
the Forge agent API and is intentionally out of scope here.

## Backend contract

The same `ProviderStrategy`/`ToolStrategy` boundary is used for Vertex AI Gemini, Vertex
AI Anthropic, Google GenAI, and direct Anthropic. A model connection serving any migrated
stage must declare the `structured_output` capability. Legacy implicit connections declare
it automatically; explicit administrator and project connections must opt in after their
chosen model/backend combination has been verified. Missing capability fails during model
policy resolution before inference begins.

Schemas reject unknown fields and report Pydantic validation paths in the terminal error.
Langfuse trace name, stage policy key, model connection, backend, and model attribution are
resolved exactly as for text stages and cover both native and fallback invocations.
