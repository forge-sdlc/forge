"""Prometheus metrics endpoint for observability."""

from fastapi import APIRouter, Response
from prometheus_client import (
    CONTENT_TYPE_LATEST,
    Counter,
    Gauge,
    Histogram,
    generate_latest,
)

router = APIRouter(tags=["metrics"])

# Webhook counters
WEBHOOKS_RECEIVED = Counter(
    "forge_webhooks_received_total",
    "Total number of webhook events received",
    ["source", "event_type"],
)

WEBHOOKS_PROCESSED = Counter(
    "forge_webhooks_processed_total",
    "Total number of webhook events successfully processed",
    ["source", "event_type"],
)

WEBHOOKS_FAILED = Counter(
    "forge_webhooks_failed_total",
    "Total number of webhook events that failed processing",
    ["source", "event_type", "error_type"],
)

# Workflow counters
WORKFLOWS_STARTED = Counter(
    "forge_workflows_started_total",
    "Total number of workflows started",
    ["ticket_type"],
)

WORKFLOWS_COMPLETED = Counter(
    "forge_workflows_completed_total",
    "Total number of workflows completed",
    ["ticket_type", "final_node"],
)

WORKFLOWS_FAILED = Counter(
    "forge_workflows_failed_total",
    "Total number of workflows that failed",
    ["ticket_type", "error_type"],
)

# Review/Approval metrics
APPROVALS = Counter(
    "forge_approvals_total",
    "Total number of approvals by stage",
    ["stage"],  # prd, spec, plan
)

REVISIONS_REQUESTED = Counter(
    "forge_revisions_requested_total",
    "Total number of revision requests by stage",
    ["stage"],  # prd, spec, plan
)

PROPOSAL_REVIEW_DECISIONS = Counter(
    "forge_proposal_review_decisions_total",
    "Proposal review thread decisions by artifact type and disposition",
    ["artifact_type", "disposition"],
)

# CI/CD metrics
CI_FIX_ATTEMPTS = Counter(
    "forge_ci_fix_attempts_total",
    "Total number of CI fix attempts",
    ["repo", "result"],
)

# Agent metrics
AGENT_INVOCATIONS = Counter(
    "forge_agent_invocations_total",
    "Total number of agent invocations",
    ["task_type"],
)

AGENT_DURATION = Histogram(
    "forge_agent_duration_seconds",
    "Duration of agent invocations",
    ["task_type"],
    buckets=[1, 5, 10, 30, 60, 120, 300, 600],
)

# Queue metrics
QUEUE_DEPTH = Gauge(
    "forge_queue_depth",
    "Current depth of event queues",
    ["queue_name"],
)

# MCP metrics
MCP_TOOLS_LOADED = Gauge(
    "forge_mcp_tools_loaded",
    "Number of MCP tools currently loaded",
    ["server"],
)

# Phase duration metrics
PHASE_DURATION = Histogram(
    "forge_phase_duration_seconds",
    "Duration of workflow phases",
    ["phase"],  # prd_generation, spec_generation, epic_decomposition, task_generation, etc.
    buckets=[5, 10, 30, 60, 120, 300, 600, 1200, 1800],
)

# External API latency metrics
EXTERNAL_API_LATENCY = Histogram(
    "forge_external_api_latency_seconds",
    "Latency of external API calls",
    [
        "service",
        "operation",
    ],  # service: jira, github, llm; operation: get_issue, create_pr, etc.
    buckets=[0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0],
)

EXTERNAL_API_ERRORS = Counter(
    "forge_external_api_errors_total",
    "Total external API call errors",
    ["service", "operation", "error_type"],
)

# Review cycle metrics
REVIEW_CYCLES = Counter(
    "forge_review_cycles_total",
    "Total review cycles detected",
    ["skill", "step"],
)

REVIEW_VERDICTS = Counter(
    "forge_review_verdicts_total",
    "Review verdicts by outcome",
    ["skill", "step", "verdict"],  # verdict: approved, rejected
)

REVIEW_DURATION = Histogram(
    "forge_review_duration_seconds",
    "Review cycle duration",
    ["skill", "step"],
    buckets=[1, 5, 10, 30, 60, 120, 300, 600],  # Same as AGENT_DURATION
)

EFFECT_ATTEMPTS = Counter(
    "forge_effect_attempts_total",
    "Durable external effect attempts",
    ["operation"],
)

EFFECT_RESULTS = Counter(
    "forge_effect_results_total",
    "Durable external effect results",
    ["operation", "status"],
)

EFFECT_REPLAYS = Counter(
    "forge_effect_replays_total",
    "Operator-requested durable effect replays",
    ["operation"],
)

# Execution read-model metrics.  These deliberately use bounded labels (status,
# drift class, and blocking code) so an issue key or arbitrary provider message
# can never create an unbounded Prometheus time series.
READ_MODEL_LATENCY = Histogram(
    "forge_read_model_latency_seconds",
    "Latency of authenticated execution read-model requests",
    ["operation"],
    buckets=[0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5],
)

EXECUTION_WAITING_AGE = Histogram(
    "forge_execution_waiting_age_seconds",
    "Age of executions currently waiting for an external or operator action",
    ["code"],
    buckets=[60, 300, 900, 3600, 21600, 86400, 604800],
)

EXECUTION_RETRIES = Gauge(
    "forge_execution_retry_count",
    "Retry count in the most recently sampled execution read model",
    ["kind"],
)

EXECUTION_DRIFT = Gauge(
    "forge_execution_drift_state",
    "Drift state in the most recently sampled execution read model (0 or 1)",
    ["class"],
)

EXECUTION_BLOCKED = Gauge(
    "forge_execution_blocked_state",
    "Blocked state in the most recently sampled execution read model (0 or 1)",
    ["code"],
)

EXECUTION_MIGRATION_ELIGIBILITY = Gauge(
    "forge_execution_migration_eligibility",
    "Current execution migration eligibility (1 eligible, 0 ineligible, -1 unknown)",
    ["state"],
)

_BLOCKING_CODES = ("blocked", "failed", "gate", "unknown")
_DRIFT_CLASSES = ("operator_required", "stale")
_MIGRATION_STATES = ("eligible", "ineligible", "unknown")


@router.get("/metrics")
async def metrics() -> Response:
    """Expose Prometheus metrics.

    Returns:
        Prometheus-formatted metrics.
    """
    return Response(
        content=generate_latest(),
        media_type=CONTENT_TYPE_LATEST,
    )


# Helper functions to record metrics
def record_webhook_received(source: str, event_type: str) -> None:
    """Record a webhook received event."""
    WEBHOOKS_RECEIVED.labels(source=source, event_type=event_type).inc()


def record_webhook_processed(source: str, event_type: str) -> None:
    """Record a webhook successfully processed."""
    WEBHOOKS_PROCESSED.labels(source=source, event_type=event_type).inc()


def record_webhook_failed(source: str, event_type: str, error_type: str) -> None:
    """Record a webhook processing failure."""
    WEBHOOKS_FAILED.labels(source=source, event_type=event_type, error_type=error_type).inc()


def record_workflow_started(ticket_type: str) -> None:
    """Record a workflow started."""
    WORKFLOWS_STARTED.labels(ticket_type=ticket_type).inc()


def record_workflow_completed(ticket_type: str, final_node: str) -> None:
    """Record a workflow completed."""
    WORKFLOWS_COMPLETED.labels(ticket_type=ticket_type, final_node=final_node).inc()


def record_workflow_failed(ticket_type: str, error_type: str) -> None:
    """Record a workflow failure."""
    WORKFLOWS_FAILED.labels(ticket_type=ticket_type, error_type=error_type).inc()


def record_ci_fix_attempt(repo: str, result: str) -> None:
    """Record a CI fix attempt."""
    CI_FIX_ATTEMPTS.labels(repo=repo, result=result).inc()


def record_agent_invocation(task_type: str) -> None:
    """Record an agent invocation."""
    AGENT_INVOCATIONS.labels(task_type=task_type).inc()


def observe_agent_duration(task_type: str, duration: float) -> None:
    """Record agent invocation duration."""
    AGENT_DURATION.labels(task_type=task_type).observe(duration)


def set_queue_depth(queue_name: str, depth: int) -> None:
    """Set current queue depth."""
    QUEUE_DEPTH.labels(queue_name=queue_name).set(depth)


def set_mcp_tools_loaded(server: str, count: int) -> None:
    """Set number of MCP tools loaded from a server."""
    MCP_TOOLS_LOADED.labels(server=server).set(count)


def record_approval(stage: str) -> None:
    """Record an approval for a stage (prd, spec, plan)."""
    APPROVALS.labels(stage=stage).inc()


def record_revision_requested(stage: str) -> None:
    """Record a revision request for a stage (prd, spec, plan)."""
    REVISIONS_REQUESTED.labels(stage=stage).inc()


def record_effect_attempt(operation: str) -> None:
    EFFECT_ATTEMPTS.labels(operation=operation).inc()


def record_effect_result(operation: str, status: str) -> None:
    EFFECT_RESULTS.labels(operation=operation, status=status).inc()


def record_effect_replay(operation: str) -> None:
    EFFECT_REPLAYS.labels(operation=operation).inc()


def observe_read_model_latency(operation: str, duration: float) -> None:
    """Observe one authenticated read-model request latency."""
    READ_MODEL_LATENCY.labels(operation=operation).observe(max(0.0, duration))


def record_execution_read_model(model: object) -> None:
    """Record bounded operational signals from an execution projection.

    ``model`` is intentionally accepted as an object rather than importing the
    read-model package.  This keeps the metrics module usable by projection and
    API code without introducing an import cycle.
    """
    raw_status = getattr(model, "status", "")
    status = str(getattr(raw_status, "value", raw_status))
    for known_code in _BLOCKING_CODES:
        EXECUTION_BLOCKED.labels(code=known_code).set(0)
    waiting = getattr(model, "waiting", None)
    if waiting is not None:
        raw_code = str(getattr(waiting, "code", "unknown"))
        code = raw_code if raw_code in _BLOCKING_CODES else "unknown"
        EXECUTION_BLOCKED.labels(code=code).set(1 if status == "blocked" else 0)
        since = getattr(waiting, "since", None)
        if since is not None:
            from datetime import UTC, datetime

            if since.tzinfo is None:
                since = since.replace(tzinfo=UTC)
            EXECUTION_WAITING_AGE.labels(code=code).observe(
                max(0.0, (datetime.now(UTC) - since).total_seconds())
            )

    # A projection has attempt numbers for both station and durable-effect
    # work.  Count only additional attempts (attempt 1 is the initial try).
    retry_count = 0
    for attempt in (
        *(getattr(item, "attempt", 1) for item in getattr(model, "station_attempts", ())),
        *(getattr(item, "attempt", 1) for item in getattr(model, "effects", ())),
    ):
        retry_count += max(0, int(attempt) - 1)
    EXECUTION_RETRIES.labels(kind="execution").set(retry_count)

    observations = [getattr(model, "last_observation", None)]
    observations.extend(getattr(model, "stale_observations", ()))
    observations.extend(getattr(model, "conflicting_observations", ()))
    drift_counts = {"operator_required": 0, "stale": 0}
    for observation in observations:
        if observation is not None:
            if getattr(observation, "conflicting", False):
                drift_counts["operator_required"] += 1
            elif getattr(observation, "stale", False):
                drift_counts["stale"] += 1
    for drift_class in _DRIFT_CLASSES:
        count = drift_counts[drift_class]
        EXECUTION_DRIFT.labels(**{"class": drift_class}).set(1 if count else 0)

    migration = getattr(model, "migration", None)
    eligible = getattr(migration, "eligible", None)
    migration_state = (
        "eligible" if eligible is True else "ineligible" if eligible is False else "unknown"
    )
    for state in _MIGRATION_STATES:
        EXECUTION_MIGRATION_ELIGIBILITY.labels(state=state).set(0)
    EXECUTION_MIGRATION_ELIGIBILITY.labels(state=migration_state).set(
        1 if eligible is True else 0 if eligible is False else -1
    )


def record_proposal_review_decision(artifact_type: str, disposition: str) -> None:
    """Record one semantic proposal-review thread decision."""
    PROPOSAL_REVIEW_DECISIONS.labels(
        artifact_type=artifact_type,
        disposition=disposition,
    ).inc()


def observe_phase_duration(phase: str, duration: float) -> None:
    """Record duration of a workflow phase.

    Args:
        phase: Phase name (prd_generation, spec_generation, etc.).
        duration: Duration in seconds.
    """
    PHASE_DURATION.labels(phase=phase).observe(duration)


def observe_external_api_latency(service: str, operation: str, duration: float) -> None:
    """Record latency of an external API call.

    Args:
        service: External service name (jira, github, llm).
        operation: Operation name (get_issue, create_pr, generate, etc.).
        duration: Duration in seconds.
    """
    EXTERNAL_API_LATENCY.labels(service=service, operation=operation).observe(duration)


def record_external_api_error(service: str, operation: str, error_type: str) -> None:
    """Record an external API call error.

    Args:
        service: External service name (jira, github, llm).
        operation: Operation name.
        error_type: Type of error (timeout, rate_limit, auth, etc.).
    """
    EXTERNAL_API_ERRORS.labels(service=service, operation=operation, error_type=error_type).inc()


def record_review_cycle(skill: str, step: str) -> None:
    """Record a review cycle detected.

    Args:
        skill: Skill name (e.g., implement-task, fix-ci).
        step: Workflow step name.
    """
    REVIEW_CYCLES.labels(skill=skill, step=step).inc()


def record_review_verdict(skill: str, step: str, verdict: str) -> None:
    """Record a review verdict.

    Args:
        skill: Skill name (e.g., implement-task, fix-ci).
        step: Workflow step name.
        verdict: Verdict outcome (approved, rejected).
    """
    REVIEW_VERDICTS.labels(skill=skill, step=step, verdict=verdict).inc()


def observe_review_duration(skill: str, step: str, duration: float) -> None:
    """Record review cycle duration.

    Args:
        skill: Skill name (e.g., implement-task, fix-ci).
        step: Workflow step name.
        duration: Duration in seconds.
    """
    REVIEW_DURATION.labels(skill=skill, step=step).observe(duration)
