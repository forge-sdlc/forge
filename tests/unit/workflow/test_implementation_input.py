from unittest.mock import AsyncMock

import pytest

from forge.integrations.jira.models import JiraIssue
from forge.workflow.implementation_input import (
    NoPendingImplementationWork,
    resolve_implementation_input,
)


def issue(key: str, description: str, *, labels: list[str], issue_type: str = "Task") -> JiraIssue:
    return JiraIssue(
        key=key,
        id=key,
        summary=f"Summary {key}",
        description=description,
        status="Open",
        issue_type=issue_type,
        labels=labels,
    )


def jira_with(*issues: JiraIssue) -> AsyncMock:
    by_key = {item.key: item for item in issues}
    jira = AsyncMock()
    jira.get_issue.side_effect = by_key.__getitem__
    return jira


@pytest.mark.asyncio
async def test_current_task_wins_and_all_lower_artifacts_are_context():
    jira = jira_with(
        issue("TASK-2", "specific task", labels=["repo:acme/api"]),
        issue("EPIC-1", "repository plan", labels=["repo:acme/api"], issue_type="Epic"),
        issue("FEAT-1", "root request", labels=["repo:acme/api"], issue_type="Feature"),
    )
    result = await resolve_implementation_input(
        {
            "ticket_key": "FEAT-1",
            "current_repo": "acme/api",
            "current_task_key": "TASK-2",
            "tasks_by_repo": {"acme/api": ["TASK-2"]},
            "epic_keys": ["EPIC-1"],
            "plan_content": "general plan",
            "spec_content": "spec",
            "rca_content": "rca",
            "prd_content": "prd",
        },
        jira,
    )

    assert result.work_unit["kind"] == "task"
    assert result.work_unit["key"] == "TASK-2"
    assert [item["kind"] for item in result.context_artifacts] == [
        "task",
        "epic_plan",
        "plan",
        "spec",
        "rca",
        "prd",
        "ticket",
    ]
    assert result.context_artifacts[0]["digest"].startswith("sha256:")
    assert result.state_update()["current_work_unit_id"] == "TASK-2"


@pytest.mark.asyncio
async def test_first_pending_repository_task_wins_deterministically():
    jira = jira_with(
        issue("TASK-2", "second pending", labels=["repo:acme/api"]),
        issue("ROOT-1", "root", labels=["repo:acme/api"]),
    )
    result = await resolve_implementation_input(
        {
            "ticket_key": "ROOT-1",
            "current_repo": "acme/api",
            "tasks_by_repo": {"acme/api": ["TASK-1", "TASK-2"]},
            "implemented_tasks": ["TASK-1"],
        },
        jira,
    )
    assert result.work_unit["key"] == "TASK-2"


@pytest.mark.asyncio
async def test_completed_normalized_work_unit_advances_to_next_repository_task():
    jira = jira_with(
        issue("TASK-2", "second pending", labels=["repo:acme/api"]),
        issue("ROOT-1", "root", labels=["repo:acme/api"]),
    )
    result = await resolve_implementation_input(
        {
            "ticket_key": "ROOT-1",
            "current_repo": "acme/api",
            "tasks_by_repo": {"acme/api": ["TASK-1", "TASK-2"]},
            "work_units": [{"id": "TASK-1", "status": "completed"}],
        },
        jira,
    )

    assert result.work_unit["key"] == "TASK-2"


@pytest.mark.asyncio
async def test_state_update_preserves_resolution_history():
    jira = jira_with(
        issue("TASK-2", "pending", labels=["repo:acme/api"]),
        issue("ROOT-1", "root", labels=["repo:acme/api"]),
    )
    state = {
        "ticket_key": "ROOT-1",
        "current_repo": "acme/api",
        "tasks_by_repo": {"acme/api": ["TASK-1", "TASK-2"]},
        "work_units": [{"id": "TASK-1", "status": "completed"}],
        "artifacts": [{"id": "jira:TASK-1:task", "kind": "task"}],
    }
    result = await resolve_implementation_input(state, jira)
    update = result.state_update(state)

    assert [unit["id"] for unit in update["work_units"]] == ["TASK-1", "TASK-2"]
    assert [artifact["id"] for artifact in update["artifacts"]] == [
        "jira:TASK-1:task",
        "jira:TASK-2:task",
        "jira:ROOT-1:ticket",
    ]


@pytest.mark.asyncio
async def test_task_takeover_root_is_the_primary_work_unit():
    jira = jira_with(issue("TASK-9", "", labels=["repo:acme/api"]))
    result = await resolve_implementation_input(
        {
            "ticket_key": "TASK-9",
            "ticket_type": "Task",
            "current_repo": "acme/api",
            "plan_content": "approved plan",
        },
        jira,
    )

    assert result.work_unit["kind"] == "task"
    assert result.work_unit["key"] == "TASK-9"
    assert result.instructions == "Summary TASK-9"
    assert [artifact["kind"] for artifact in result.context_artifacts] == ["task", "plan"]


@pytest.mark.asyncio
async def test_only_repository_matching_epic_is_eligible():
    jira = jira_with(
        issue("EPIC-WEB", "web plan", labels=["repo:acme/web"], issue_type="Epic"),
        issue("EPIC-API", "api plan", labels=["repo:acme/api"], issue_type="Epic"),
        issue("ROOT-1", "root", labels=["repo:acme/api"]),
    )
    result = await resolve_implementation_input(
        {
            "ticket_key": "ROOT-1",
            "current_repo": "acme/api",
            "epic_keys": ["EPIC-WEB", "EPIC-API"],
        },
        jira,
    )
    assert result.work_unit["kind"] == "epic_plan"
    assert result.work_unit["key"] == "EPIC-API"
    assert [a["source"] for a in result.context_artifacts] == ["EPIC-API", "ROOT-1"]


@pytest.mark.asyncio
async def test_plan_falls_back_through_spec_rca_prd_and_ticket():
    jira = jira_with(issue("BUG-1", "root", labels=["repo:acme/api"], issue_type="Bug"))
    result = await resolve_implementation_input(
        {
            "ticket_key": "BUG-1",
            "current_repo": "acme/api",
            "spec_content": "spec",
            "rca_content": "rca",
            "prd_content": "prd",
        },
        jira,
    )
    assert result.work_unit["kind"] == "spec"
    assert [a["kind"] for a in result.context_artifacts] == ["spec", "rca", "prd", "ticket"]


@pytest.mark.asyncio
async def test_mismatched_current_task_mapping_fails_before_fetch():
    jira = AsyncMock()
    with pytest.raises(ValueError, match="belongs to repository acme/web"):
        await resolve_implementation_input(
            {
                "ticket_key": "ROOT-1",
                "current_repo": "acme/api",
                "current_task_key": "TASK-1",
                "tasks_by_repo": {"acme/web": ["TASK-1"]},
            },
            jira,
        )
    jira.get_issue.assert_not_awaited()


@pytest.mark.asyncio
async def test_missing_repository_or_artifacts_fails_fast():
    with pytest.raises(ValueError, match="current_repo"):
        await resolve_implementation_input({"ticket_key": "ROOT-1"}, AsyncMock())

    jira = jira_with(issue("ROOT-1", "", labels=["repo:acme/api"]))
    with pytest.raises(ValueError, match="No implementation artifact"):
        await resolve_implementation_input(
            {"ticket_key": "ROOT-1", "current_repo": "acme/api"}, jira
        )


@pytest.mark.asyncio
async def test_completed_tasks_do_not_fall_back_to_coarser_artifact():
    with pytest.raises(NoPendingImplementationWork, match="All Jira tasks"):
        await resolve_implementation_input(
            {
                "ticket_key": "ROOT-1",
                "current_repo": "acme/api",
                "tasks_by_repo": {"acme/api": ["TASK-1"]},
                "implemented_tasks": ["TASK-1"],
                "spec_content": "must not run again",
            },
            AsyncMock(),
        )


@pytest.mark.asyncio
async def test_completed_internal_work_unit_is_not_rerun():
    jira = jira_with(issue("ROOT-1", "root", labels=["repo:acme/api"]))
    first = await resolve_implementation_input(
        {"ticket_key": "ROOT-1", "current_repo": "acme/api", "spec_content": "spec"}, jira
    )
    with pytest.raises(NoPendingImplementationWork, match="already complete"):
        await resolve_implementation_input(
            {
                "ticket_key": "ROOT-1",
                "current_repo": "acme/api",
                "spec_content": "spec",
                "work_units": [{**first.work_unit, "status": "completed"}],
            },
            jira,
        )


@pytest.mark.asyncio
async def test_normalized_approved_plan_is_selected_and_ancestors_are_context():
    jira = jira_with(issue("ROOT-1", "root", labels=["repo:acme/api"]))
    result = await resolve_implementation_input(
        {
            "ticket_key": "ROOT-1",
            "current_repository": "acme/api",
            "artifacts": [
                {
                    "id": "prd:1",
                    "kind": "prd",
                    "content": "requirements",
                    "digest": "sha256:prd",
                    "approved_digest": "sha256:prd",
                    "status": "approved",
                },
                {
                    "id": "spec:1",
                    "kind": "spec",
                    "content": "design",
                    "digest": "sha256:spec",
                    "approved_digest": "sha256:spec",
                    "status": "approved",
                },
                {
                    "id": "plan:1",
                    "kind": "plan",
                    "content": "implementation steps",
                    "digest": "sha256:plan",
                    "approved_digest": "sha256:plan",
                    "status": "approved",
                },
            ],
        },
        jira,
    )

    assert result.work_unit["kind"] == "plan"
    assert result.work_unit["source_artifact_ids"] == ["plan:1"]
    assert result.work_unit["context_artifact_ids"] == [
        "spec:1",
        "prd:1",
        "jira:ROOT-1:ticket",
    ]


@pytest.mark.asyncio
async def test_unapproved_and_stale_artifacts_are_not_implementation_input():
    jira = jira_with(issue("ROOT-1", "root", labels=["repo:acme/api"]))
    result = await resolve_implementation_input(
        {
            "ticket_key": "ROOT-1",
            "current_repo": "acme/api",
            "artifacts": [
                {
                    "id": "plan:1",
                    "kind": "plan",
                    "content": "changed plan",
                    "digest": "sha256:new",
                    "approved_digest": "sha256:old",
                    "status": "approved",
                },
                {
                    "id": "spec:1",
                    "kind": "spec",
                    "content": "stale design",
                    "digest": "sha256:spec",
                    "approved_digest": "sha256:spec",
                    "status": "stale",
                },
            ],
        },
        jira,
    )

    assert result.work_unit["kind"] == "ticket"


@pytest.mark.asyncio
async def test_stale_task_blocks_broader_artifact_fallback():
    with pytest.raises(ValueError, match="Tasks derived from stale planning"):
        await resolve_implementation_input(
            {
                "ticket_key": "ROOT-1",
                "current_repo": "acme/api",
                "plan_content": "must not be selected",
                "work_units": [
                    {
                        "id": "TASK-1",
                        "kind": "task",
                        "repository": "acme/api",
                        "status": "stale",
                    }
                ],
            },
            AsyncMock(),
        )
