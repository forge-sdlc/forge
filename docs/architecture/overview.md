# System & Components

## System Context

Forge sits between project management (Jira), source control (GitHub), and LLM providers, orchestrating work from ticket creation through merged PR.

```mermaid
flowchart LR
    A["Jira / GitHub\n(webhooks)"] --> B["Gateway\n(FastAPI)"]
    B --> C["Redis\n(Streams + State)"]
    C --> D["Workers\n(LangGraph)"]
    D --> E["Podman\nContainers"]
    D <--> F["LLM\n(Claude / Gemini)"]
    E <--> F
    D --> A
```

**External actors:**

- **Jira**: Source of ticket lifecycle events (issue and comment webhooks)
- **GitHub**: Source of PR, CI, and code review events (PR, check suite, and review webhooks)
- **LLM providers**: Anthropic (direct API) and Google Vertex AI (Claude and Gemini models)
- **Langfuse**: Optional observability for LLM call tracing and cost tracking
- **Human reviewers**: Approve or revise artifacts at defined workflow gates

## Component Responsibilities

```mermaid
flowchart TD
    subgraph External["External Systems"]
        Jira
        GitHub
        Langfuse["Langfuse (Observability)"]
    end

    subgraph Gateway["FastAPI Gateway (:8000)"]
        JiraWH["POST /api/v1/webhooks/jira"]
        GitHubWH["POST /api/v1/webhooks/github"]
    end

    subgraph Queue["Redis"]
        Streams["Streams: forge:events:jira\nforge:events:github"]
        State["AsyncRedisSaver\nLangGraph checkpointing"]
    end

    subgraph Workers["Worker Processes (consumer group: forge-workers)"]
        Router{"WorkflowRouter\nroute by issue type"}
        Feature["FeatureWorkflow\n(Feature/Story)"]
        Bug["BugWorkflow\n(Bug)"]
        Task["TaskTakeoverWorkflow\n(Task/Epic)"]
    end

    subgraph Container["Podman Container (ephemeral)"]
        Agent["Deep Agents + MCP\n/workspace (repo mounted)"]
    end

    LLM["LLM Backends\nAnthropic API (Claude)\nVertex AI (Claude/Gemini)"]

    Jira -- webhooks --> JiraWH
    GitHub -- webhooks --> GitHubWH
    JiraWH --> Streams
    GitHubWH --> Streams
    Streams --> Router
    Router --> Feature
    Router --> Bug
    Router --> Task
    Feature --> Container
    Bug --> Container
    Task --> Container
    Workers <--> LLM
    Container <--> LLM
    Workers --> Jira
    Workers --> GitHub
    Workers --> Langfuse
```

**Gateway (FastAPI)**: Accepts webhooks over HTTPS, validates HMAC-SHA256 signatures, and publishes events to Redis Streams. Performs no workflow logic.

**Worker**: Consumes events from Redis Streams via the `forge-workers` consumer group. The `WorkflowRouter` resolves the target LangGraph workflow (Feature, Bug, or Task Takeover) based on Jira issue type and drives execution through planning, implementation, CI repair, and human review stages.

**Redis**: Event bus (Redis Streams), workflow state store (LangGraph `AsyncRedisSaver` checkpoints per ticket), retry queue, dead-letter queue, and supporting indexes (PR-to-ticket mapping, deduplication keys).

**Podman Container**: Ephemeral rootless containers that execute implementation tasks. Each container receives the repo at `/workspace` (read-write), a task file at `/task.json` (read-only), and LLM credentials. Runs Deep Agents with MCP tool access. The orchestrator handles pushing and PR creation after the container exits.

**LLM Backends**: Claude, Gemini, and OpenAI-compatible models called by both orchestrator nodes (planning, review) and container agents (code generation). Supports Anthropic direct API, Google APIs and Vertex AI, and configured OpenAI-compatible Chat Completions endpoints.
