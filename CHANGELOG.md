# Changelog

All notable changes to the Forge SDLC Orchestrator will be documented in this file.

## [1.0.0] - 2026-08-24

This is the initial stable release of **Forge**, the AI-Integrated SDLC Orchestrator. This release marks the transition from pre-release development to a production-ready system with a robust set of capabilities, integrations, and observability.

### Key Capabilities
* **AI-Driven SDLC Graph Workflows**: Powered by LangGraph, Forge orchestrates structured software engineering activities from bug triage and root-cause analysis (RCA) to writing product requirements (PRDs), designing technical specs, creating task plans, and executing code implementation.
* **Multi-Model Support**: Integrated with multiple language model providers (direct model providers and Google Vertex AI), supporting connection configurations with fallback behaviors and per-stage policies.
* **Sandbox Isolation**: Execution of autonomous tasks inside ephemeral, rootless Podman or Kubernetes containers. Full guardrail and constraint injection via local workspace policies (`CLAUDE.md`, `AGENTS.md`, etc.).

### Key Integrations
* **Jira & GitHub Adapters**: Complete bi-directional integrations supporting webhook events, ticket labels, ticket comments, pull request comments, and automatic code submissions (commits and PRs).
* **MCP Endpoints**: Built-in Model Context Protocol (MCP) server endpoints for rich developer environment tools.
* **LangGraph Checkpointing with Redis**: Production-grade distributed state checkpointing and event-queue management using Redis Streams.

### Observability & Infrastructure
* **Prometheus Metrics**: Detailed Prometheus instrumentation for API route performance, agent execution time, queue processing latency, and model token usage.
* **Grafana Dashboards**: Pre-configured Grafana dashboards for visualizing performance and monitoring system health.
* **OTLP Distributed Tracing**: Full OpenTelemetry tracing configuration supporting Jaeger and other OTLP-compliant endpoints.
* **Langfuse Tracing Integration**: Comprehensive tracking of prompt templates, model responses, request latency, API cost, and exact token counts.
