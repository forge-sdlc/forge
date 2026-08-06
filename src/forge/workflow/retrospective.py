"""Bounded, non-mutating retrospective reporting for terminal workflows."""

from __future__ import annotations

import hashlib
import logging
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from forge.config import Settings, get_settings
from forge.integrations.jira.client import JiraClient

logger = logging.getLogger(__name__)

_SECRET = re.compile(r"(?i)(api[_-]?key|token|secret|password|authorization)\s*[:=]\s*[^\s,;]+")
_MAX_TEXT = 240


def _safe_text(value: Any) -> str:
    text = str("" if value is None else value)[:_MAX_TEXT]
    return _SECRET.sub(r"\1=[REDACTED]", text)


@dataclass(frozen=True)
class Evidence:
    stage: str
    metric: str
    value: str


@dataclass(frozen=True)
class RetrospectiveInput:
    ticket_key: str
    outcome: str
    started_at: str | None
    completed_at: str
    evidence: tuple[Evidence, ...]
    historical_runs: int = 0


@dataclass(frozen=True)
class Recommendation:
    title: str
    detail: str
    evidence: tuple[str, ...]
    scope: str = "single incident"


@dataclass(frozen=True)
class RetrospectiveReport:
    input: RetrospectiveInput
    recommendations: tuple[Recommendation, ...] = field(default_factory=tuple)
    model: str = "deterministic-v1"
    input_tokens: int = 0
    output_tokens: int = 0
    estimated_cost_usd: float = 0.0


def build_input(state: Mapping[str, Any], max_items: int = 20) -> RetrospectiveInput:
    """Project unrestricted workflow state into a small, redacted typed summary."""
    evidence: list[Evidence] = []

    def add(stage: str, metric: str, value: Any) -> None:
        if value is not None and len(evidence) < max_items:
            evidence.append(Evidence(stage, metric, _safe_text(value)))

    blocked = bool(state.get("is_blocked"))
    outcome = "blocked" if blocked else "successful"
    if state.get("last_error") and not blocked:
        outcome = "partially_completed"
    add("workflow", "terminal_node", state.get("current_node", "unknown"))
    add("workflow", "retry_count", state.get("retry_count", 0))
    add("workflow", "blocked", blocked)
    add("workflow", "error", state.get("last_error"))
    add("ci", "status", state.get("ci_status"))
    add("ci", "fix_attempts", state.get("ci_fix_attempt", 0))
    add("ci", "failed_checks", len(state.get("ci_failed_checks") or []))
    add("review", "local_attempts", state.get("local_review_attempts", 0))
    add("review", "ai_status", state.get("ai_review_status"))
    add("review", "human_status", state.get("human_review_status"))
    add("delivery", "repositories_completed", len(state.get("repos_completed") or []))
    add("delivery", "pull_requests", len(state.get("pr_urls") or []))
    return RetrospectiveInput(
        ticket_key=_safe_text(state.get("ticket_key", "unknown")),
        outcome=outcome,
        started_at=state.get("created_at"),
        completed_at=datetime.now(UTC).isoformat(),
        evidence=tuple(evidence),
    )


def analyze(data: RetrospectiveInput) -> RetrospectiveReport:
    """Create conservative recommendations directly supported by supplied facts."""
    values = {(item.stage, item.metric): item.value for item in data.evidence}
    recs: list[Recommendation] = []
    scope = "recurring pattern" if data.historical_runs > 1 else "single incident"
    if int(values.get(("ci", "fix_attempts"), "0")) > 0:
        recs.append(
            Recommendation(
                "Move CI validation earlier",
                "At least one autonomous CI repair was needed; reproduce the failing checks locally.",
                ("ci.fix_attempts", "ci.failed_checks"),
                scope,
            )
        )
    if int(values.get(("review", "local_attempts"), "0")) > 1:
        recs.append(
            Recommendation(
                "Strengthen pre-review validation",
                "Multiple local review passes indicate an opportunity for an earlier validation gate.",
                ("review.local_attempts",),
                scope,
            )
        )
    if data.outcome != "successful":
        recs.append(
            Recommendation(
                "Review the terminal failure",
                "The workflow did not complete successfully; inspect the bounded terminal evidence.",
                ("workflow.blocked", "workflow.error", "workflow.terminal_node"),
                scope,
            )
        )
    return RetrospectiveReport(input=data, recommendations=tuple(recs))


def format_report(report: RetrospectiveReport) -> str:
    lines = [
        "## Forge retrospective",
        "",
        f"Outcome: **{report.input.outcome}**",
        f"Evidence window: {len(report.input.evidence)} bounded facts; historical runs: "
        f"{report.input.historical_runs}.",
        "",
    ]
    if report.recommendations:
        for rec in report.recommendations:
            refs = ", ".join(f"`{ref}`" for ref in rec.evidence)
            lines.extend([f"- **{rec.title}** ({rec.scope}): {rec.detail} Evidence: {refs}."])
    else:
        lines.append("No actionable pattern was identified from the available evidence.")
    lines.extend(
        [
            "",
            f"Analysis usage: {report.model}; input tokens={report.input_tokens}; "
            f"output tokens={report.output_tokens}; estimated cost=${report.estimated_cost_usd:.4f}.",
        ]
    )
    return "\n".join(lines)


async def run_retrospective(
    state: Mapping[str, Any], settings: Settings | None = None
) -> RetrospectiveReport | None:
    """Publish a terminal report without changing state or terminal outcome."""
    settings = settings or get_settings()
    if not settings.retrospective_enabled or state.get("retrospective_completed", False):
        return None
    data = build_input(state, settings.retrospective_max_items)
    report = analyze(data)
    jira = JiraClient()
    await jira.add_comment(data.ticket_key, format_report(report))
    if settings.retrospective_create_issues:
        await _create_recommendation_tasks(jira, report)
    return report


async def _create_recommendation_tasks(jira: JiraClient, report: RetrospectiveReport) -> None:
    """Create stable-keyed tasks; Jira labels provide idempotent deduplication."""
    project = report.input.ticket_key.split("-", 1)[0]
    for rec in report.recommendations:
        digest = hashlib.sha256(rec.title.encode()).hexdigest()[:12]
        label = f"forge-retro-{digest}"
        # Search is deliberately bounded to the target project and stable label.
        matches = await jira.search_issues(
            f'project = "{project}" AND labels = "{label}"', max_results=1
        )
        if matches:
            continue
        await jira.create_task(
            project,
            f"Forge retrospective: {rec.title}"[:255],
            f"{rec.detail}\n\nEvidence: {', '.join(rec.evidence)}",
            labels=["forge-retrospective", label],
        )
