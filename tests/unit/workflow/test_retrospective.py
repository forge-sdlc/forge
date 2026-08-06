from unittest.mock import AsyncMock, patch

import pytest

from forge.workflow.retrospective import (
    analyze,
    build_input,
    format_report,
    run_retrospective,
)


def test_build_input_is_bounded_and_redacts_secrets():
    data = build_input(
        {
            "ticket_key": "F-1",
            "current_node": "complete",
            "last_error": "token=supersecret failed",
            "ci_status": "failed",
            "ci_fix_attempt": 2,
            "ci_failed_checks": [{"name": str(i)} for i in range(50)],
        },
        max_items=4,
    )
    assert len(data.evidence) == 4
    assert "supersecret" not in repr(data)
    assert data.outcome == "partially_completed"


@pytest.mark.parametrize(
    ("state", "outcome"),
    [
        ({"current_node": "complete"}, "successful"),
        ({"current_node": "triage", "is_blocked": True}, "blocked"),
        ({"current_node": "complete", "last_error": "partial"}, "partially_completed"),
    ],
)
def test_outcomes(state, outcome):
    assert build_input({"ticket_key": "F-1", **state}).outcome == outcome


def test_missing_observability_produces_valid_report():
    report = analyze(build_input({"ticket_key": "F-1", "current_node": "complete"}))
    text = format_report(report)
    assert "No actionable pattern" in text
    assert "input tokens=0" in text


def test_report_links_recommendations_to_evidence():
    report = analyze(
        build_input(
            {
                "ticket_key": "F-1",
                "current_node": "complete",
                "ci_fix_attempt": 2,
                "ci_failed_checks": [{"name": "unit"}],
                "local_review_attempts": 3,
            }
        )
    )
    text = format_report(report)
    assert "`ci.fix_attempts`" in text
    assert "`review.local_attempts`" in text
    assert all(rec.scope == "single incident" for rec in report.recommendations)


@pytest.mark.asyncio
async def test_disabled_stage_has_no_side_effects():
    settings = AsyncMock(retrospective_enabled=False)
    with patch("forge.workflow.retrospective.JiraClient") as jira:
        assert await run_retrospective({"ticket_key": "F-1"}, settings) is None
    jira.assert_not_called()


@pytest.mark.asyncio
async def test_completed_stage_is_idempotent():
    settings = AsyncMock(retrospective_enabled=True)
    with patch("forge.workflow.retrospective.JiraClient") as jira:
        result = await run_retrospective(
            {"ticket_key": "F-1", "retrospective_completed": True}, settings
        )
    assert result is None
    jira.assert_not_called()


@pytest.mark.asyncio
async def test_enabled_stage_publishes_comment_but_not_issues_by_default():
    settings = AsyncMock(
        retrospective_enabled=True,
        retrospective_create_issues=False,
        retrospective_max_items=20,
    )
    jira = AsyncMock()
    with patch("forge.workflow.retrospective.JiraClient", return_value=jira):
        result = await run_retrospective(
            {"ticket_key": "F-1", "current_node": "complete", "ci_fix_attempt": 1},
            settings,
        )
    assert result is not None
    jira.add_comment.assert_awaited_once()
    jira.create_task.assert_not_awaited()


@pytest.mark.asyncio
async def test_issue_creation_is_deduplicated():
    settings = AsyncMock(
        retrospective_enabled=True,
        retrospective_create_issues=True,
        retrospective_max_items=20,
    )
    jira = AsyncMock()
    jira.search_issues.return_value = [object()]
    with patch("forge.workflow.retrospective.JiraClient", return_value=jira):
        await run_retrospective(
            {"ticket_key": "F-1", "current_node": "complete", "ci_fix_attempt": 1},
            settings,
        )
    jira.search_issues.assert_awaited_once()
    jira.create_task.assert_not_awaited()
