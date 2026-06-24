"""Unit tests for task generation revision state."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from forge.integrations.jira.models import JiraIssue
from forge.workflow.nodes.task_generation import (
    _generate_tasks_for_epic,
    generate_tasks,
    regenerate_all_tasks,
    regenerate_epic_tasks,
)


@pytest.fixture
def base_state():
    return {
        "ticket_key": "MYPROJ-1",
        "ticket_type": "Feature",
        "spec_content": "Build a backend service.",
        "epic_keys": ["MYPROJ-10"],
        "task_keys": [],
        "tasks_by_repo": {},
        "retry_count": 0,
    }


@pytest.fixture
def mock_parent_issue():
    issue = MagicMock()
    issue.project_key = "MYPROJ"
    issue.summary = "Feature summary"
    return issue


@pytest.fixture
def mock_epic_issue():
    issue = MagicMock()
    issue.summary = "Epic summary"
    issue.description = "Implement the backend pieces."
    return issue


@pytest.fixture
def mock_tasks_data():
    return [
        {
            "summary": "Task One",
            "description": "Do the first thing.",
            "repo": "acme/backend",
        }
    ]


class TestTaskRevisionState:
    """Tests for task revision state cleanup."""

    @pytest.mark.asyncio
    async def test_generate_tasks_clears_revision_flags_on_success(
        self, base_state, mock_parent_issue, mock_epic_issue, mock_tasks_data
    ):
        """Successful task generation must not leave a pending revision at the gate."""
        state = {
            **base_state,
            "feedback_comment": "Split the tasks differently.",
            "revision_requested": True,
            "current_task_key": "MYPROJ-99",
            "current_epic_key": "MYPROJ-10",
        }

        with (
            patch("forge.workflow.nodes.task_generation.JiraClient") as MockJira,
            patch("forge.workflow.nodes.task_generation.ForgeAgent") as MockAgent,
            patch("forge.workflow.nodes.task_generation.post_status_comment"),
            patch(
                "forge.workflow.nodes.task_generation._generate_tasks_for_epic",
                new_callable=AsyncMock,
                return_value=mock_tasks_data,
            ),
        ):
            mock_jira = AsyncMock()
            MockJira.return_value = mock_jira
            mock_jira.get_issue = AsyncMock(side_effect=[mock_parent_issue, mock_epic_issue])
            mock_jira.get_labels = AsyncMock(return_value=["repo:acme/backend"])
            mock_jira.create_task = AsyncMock(return_value="MYPROJ-100")
            mock_jira.set_workflow_label = AsyncMock()
            mock_jira.close = AsyncMock()
            MockAgent.return_value = AsyncMock()

            result = await generate_tasks(state)

        assert result["task_keys"] == ["MYPROJ-100"]
        assert result["current_node"] == "task_approval_gate"
        assert result["revision_requested"] is False
        assert result["feedback_comment"] is None
        assert result["current_task_key"] is None
        assert result["current_epic_key"] is None

    @pytest.mark.asyncio
    async def test_regenerate_all_tasks_clears_revision_flags_after_new_tasks(
        self, base_state, mock_parent_issue, mock_epic_issue, mock_tasks_data
    ):
        """Full task regeneration should return to the gate without looping."""
        state = {
            **base_state,
            "task_keys": ["MYPROJ-20", "MYPROJ-21"],
            "tasks_by_repo": {"acme/backend": ["MYPROJ-20", "MYPROJ-21"]},
            "feedback_comment": "Use smaller implementation tasks.",
            "revision_requested": True,
            "current_epic_key": "MYPROJ-10",
        }

        with (
            patch("forge.workflow.nodes.task_generation.JiraClient") as MockJira,
            patch("forge.workflow.nodes.task_generation.ForgeAgent") as MockAgent,
            patch("forge.workflow.nodes.task_generation.post_status_comment"),
            patch(
                "forge.workflow.nodes.task_generation._generate_tasks_for_epic",
                new_callable=AsyncMock,
                return_value=mock_tasks_data,
            ) as mock_generate,
        ):
            mock_jira = AsyncMock()
            MockJira.return_value = mock_jira
            mock_jira.archive_issue = AsyncMock()
            mock_jira.get_issue = AsyncMock(side_effect=[mock_parent_issue, mock_epic_issue])
            mock_jira.get_labels = AsyncMock(return_value=["repo:acme/backend"])
            mock_jira.create_task = AsyncMock(return_value="MYPROJ-100")
            mock_jira.set_workflow_label = AsyncMock()
            mock_jira.close = AsyncMock()
            MockAgent.return_value = AsyncMock()

            result = await regenerate_all_tasks(state)

        assert mock_jira.archive_issue.call_count == 2
        assert result["task_keys"] == ["MYPROJ-100"]
        assert result["current_node"] == "task_approval_gate"
        assert result["revision_requested"] is False
        assert result["feedback_comment"] is None
        assert result["current_epic_key"] is None
        generated_context = mock_generate.await_args.args[3]
        assert generated_context["feedback"] == "Use smaller implementation tasks."


class TestFeedbackThreading:
    """Feedback in context is appended to the generate-tasks prompt."""

    @pytest.mark.asyncio
    async def test_feedback_appended_to_prompt_when_present(self):
        """When context contains feedback, it appears in the prompt sent to the agent."""
        captured_prompts = []

        async def fake_run_task(task, prompt, context):
            _ = (task, context)
            captured_prompts.append(prompt)
            return ""  # empty → _parse_tasks_response returns []

        mock_agent = MagicMock()
        mock_agent.run_task = fake_run_task

        context = {
            "ticket_key": "TEST-1",
            "project_key": "TEST",
            "epic_key": "TEST-10",
            "epic_summary": "My Epic",
            "feature_key": "TEST-1",
            "epic_repo": "acme/backend",
            "feedback": "Please split the auth task into two separate tasks.",
        }

        await _generate_tasks_for_epic(
            agent=mock_agent,
            epic_plan="Implement authentication.",
            epic_summary="Auth Epic",
            context=context,
        )

        assert captured_prompts, "run_task was never called"
        assert "Revision Feedback" in captured_prompts[0]
        assert "Please split the auth task into two separate tasks." in captured_prompts[0]

    @pytest.mark.asyncio
    async def test_no_feedback_section_when_feedback_absent(self):
        """When context has no feedback, the prompt has no Revision Feedback section."""
        captured_prompts = []

        async def fake_run_task(task, prompt, context):
            _ = (task, context)
            captured_prompts.append(prompt)
            return ""

        mock_agent = MagicMock()
        mock_agent.run_task = fake_run_task

        context = {
            "ticket_key": "TEST-1",
            "project_key": "TEST",
            "epic_key": "TEST-10",
            "epic_summary": "My Epic",
            "feature_key": "TEST-1",
            "epic_repo": "acme/backend",
        }

        await _generate_tasks_for_epic(
            agent=mock_agent,
            epic_plan="Implement authentication.",
            epic_summary="Auth Epic",
            context=context,
        )

        assert "Revision Feedback" not in captured_prompts[0]


def _make_issue(key, summary="S", description="D", parent_key=None, project_key="MYPROJ"):
    """Helper to create a JiraIssue mock."""
    issue = MagicMock(spec=JiraIssue)
    issue.key = key
    issue.summary = summary
    issue.description = description
    issue.parent_key = parent_key
    issue.project_key = project_key
    return issue


class TestRegenerateEpicTasks:
    """Tests for regenerate_epic_tasks node."""

    @pytest.fixture
    def base_state(self):
        return {
            "ticket_key": "FEAT-1",
            "ticket_type": "Feature",
            "spec_content": "Build something.",
            "epic_keys": ["EPIC-10", "EPIC-20"],
            # TASK-100, TASK-101 belong to EPIC-10; TASK-200 belongs to EPIC-20
            "task_keys": ["TASK-100", "TASK-101", "TASK-200"],
            "tasks_by_repo": {"acme/backend": ["TASK-100", "TASK-101", "TASK-200"]},
            "current_epic_key": "EPIC-10",
            "feedback_comment": "Split the task into two.",
            "revision_requested": True,
            "retry_count": 0,
            "context": {},
        }

    @pytest.mark.asyncio
    async def test_archives_only_target_epic_tasks(self, base_state):
        """Only tasks parented to current_epic_key are archived."""
        with (
            patch("forge.workflow.nodes.task_generation.JiraClient") as MockJira,
            patch("forge.workflow.nodes.task_generation.ForgeAgent") as MockAgent,
            patch(
                "forge.workflow.nodes.task_generation._generate_tasks_for_epic",
                new_callable=AsyncMock,
                return_value=[{"summary": "New Task", "description": "D", "repo": "acme/backend"}],
            ),
        ):
            mock_jira = AsyncMock()
            MockJira.return_value = mock_jira

            # get_issue calls: feature, TASK-100 (parent=EPIC-10), TASK-101 (parent=EPIC-10),
            # TASK-200 (parent=EPIC-20), EPIC-10 (epic details), EPIC-20 (sibling)
            mock_jira.get_issue = AsyncMock(
                side_effect=[
                    _make_issue("FEAT-1", project_key="MYPROJ"),  # parent feature
                    _make_issue("TASK-100", parent_key="EPIC-10"),
                    _make_issue("TASK-101", parent_key="EPIC-10"),
                    _make_issue("TASK-200", parent_key="EPIC-20"),
                    _make_issue(
                        "EPIC-10", summary="Epic 10", description="Plan 10"
                    ),  # epic details
                    _make_issue("EPIC-20", summary="Epic 20", description="Plan 20"),  # sibling
                    _make_issue("TASK-200", summary="Old task 200"),  # remaining task ctx
                ]
            )
            mock_jira.get_labels = AsyncMock(return_value=["repo:acme/backend"])
            mock_jira.archive_issue = AsyncMock()
            mock_jira.create_task = AsyncMock(return_value="TASK-102")

            MockAgent.return_value = AsyncMock()

            await regenerate_epic_tasks(base_state)

            archived_keys = [call.args[0] for call in mock_jira.archive_issue.call_args_list]
            assert set(archived_keys) == {"TASK-100", "TASK-101"}
            assert "TASK-200" not in archived_keys

    @pytest.mark.asyncio
    async def test_preserves_other_epic_tasks_in_state(self, base_state):
        """Tasks from other epics remain in task_keys after regeneration."""
        with (
            patch("forge.workflow.nodes.task_generation.JiraClient") as MockJira,
            patch("forge.workflow.nodes.task_generation.ForgeAgent") as MockAgent,
            patch(
                "forge.workflow.nodes.task_generation._generate_tasks_for_epic",
                new_callable=AsyncMock,
                return_value=[{"summary": "New Task", "description": "D", "repo": "acme/backend"}],
            ),
        ):
            mock_jira = AsyncMock()
            MockJira.return_value = mock_jira
            mock_jira.get_issue = AsyncMock(
                side_effect=[
                    _make_issue("FEAT-1", project_key="MYPROJ"),
                    _make_issue("TASK-100", parent_key="EPIC-10"),
                    _make_issue("TASK-101", parent_key="EPIC-10"),
                    _make_issue("TASK-200", parent_key="EPIC-20"),
                    _make_issue("EPIC-10", summary="Epic 10", description="Plan 10"),
                    _make_issue("EPIC-20", summary="Epic 20", description="Plan 20"),
                    _make_issue("TASK-200", summary="Old task 200"),  # for existing_tasks context
                ]
            )
            mock_jira.get_labels = AsyncMock(return_value=["repo:acme/backend"])
            mock_jira.archive_issue = AsyncMock()
            mock_jira.create_task = AsyncMock(return_value="TASK-102")

            MockAgent.return_value = AsyncMock()

            result = await regenerate_epic_tasks(base_state)

            assert "TASK-200" in result["task_keys"]
            assert "TASK-102" in result["task_keys"]
            assert "TASK-100" not in result["task_keys"]
            assert "TASK-101" not in result["task_keys"]

    @pytest.mark.asyncio
    async def test_clears_revision_flags(self, base_state):
        """State flags are cleared after successful regeneration."""
        with (
            patch("forge.workflow.nodes.task_generation.JiraClient") as MockJira,
            patch("forge.workflow.nodes.task_generation.ForgeAgent") as MockAgent,
            patch(
                "forge.workflow.nodes.task_generation._generate_tasks_for_epic",
                new_callable=AsyncMock,
                return_value=[{"summary": "New Task", "description": "D", "repo": "acme/backend"}],
            ),
        ):
            mock_jira = AsyncMock()
            MockJira.return_value = mock_jira
            mock_jira.get_issue = AsyncMock(
                side_effect=[
                    _make_issue("FEAT-1", project_key="MYPROJ"),
                    _make_issue("TASK-100", parent_key="EPIC-10"),
                    _make_issue("TASK-101", parent_key="EPIC-10"),
                    _make_issue("TASK-200", parent_key="EPIC-20"),
                    _make_issue("EPIC-10", summary="Epic 10", description="Plan 10"),
                    _make_issue("EPIC-20", summary="Epic 20", description="Plan 20"),
                    _make_issue("TASK-200", summary="Old task 200"),
                ]
            )
            mock_jira.get_labels = AsyncMock(return_value=["repo:acme/backend"])
            mock_jira.archive_issue = AsyncMock()
            mock_jira.create_task = AsyncMock(return_value="TASK-102")
            MockAgent.return_value = AsyncMock()

            result = await regenerate_epic_tasks(base_state)

            assert result["current_epic_key"] is None
            assert result["feedback_comment"] is None
            assert result["revision_requested"] is False
            assert result["current_node"] == "task_approval_gate"

    @pytest.mark.asyncio
    async def test_feedback_passed_to_generate(self, base_state):
        """feedback_comment is passed as 'feedback' in context to _generate_tasks_for_epic."""
        captured_context = {}

        async def fake_generate(_agent, _epic_plan, _epic_summary, context, **_kwargs):
            captured_context.update(context)
            return []

        with (
            patch("forge.workflow.nodes.task_generation.JiraClient") as MockJira,
            patch("forge.workflow.nodes.task_generation.ForgeAgent") as MockAgent,
            patch(
                "forge.workflow.nodes.task_generation._generate_tasks_for_epic",
                side_effect=fake_generate,
            ),
        ):
            mock_jira = AsyncMock()
            MockJira.return_value = mock_jira
            mock_jira.get_issue = AsyncMock(
                side_effect=[
                    _make_issue("FEAT-1", project_key="MYPROJ"),
                    _make_issue("TASK-100", parent_key="EPIC-10"),
                    _make_issue("TASK-101", parent_key="EPIC-10"),
                    _make_issue("TASK-200", parent_key="EPIC-20"),
                    _make_issue("EPIC-10", summary="Epic 10", description="Plan 10"),
                    _make_issue("EPIC-20", summary="Epic 20", description="Plan 20"),
                    _make_issue("TASK-200", summary="Old task 200"),
                ]
            )
            mock_jira.get_labels = AsyncMock(return_value=[])
            mock_jira.archive_issue = AsyncMock()
            MockAgent.return_value = AsyncMock()

            await regenerate_epic_tasks(base_state)

            assert captured_context.get("feedback") == "Split the task into two."

    @pytest.mark.asyncio
    async def test_no_generated_replacements_does_not_archive_existing_tasks(self, base_state):
        """Empty replacement generation leaves existing epic tasks intact and returns an error state."""
        with (
            patch("forge.workflow.nodes.task_generation.JiraClient") as MockJira,
            patch("forge.workflow.nodes.task_generation.ForgeAgent") as MockAgent,
            patch(
                "forge.workflow.nodes.task_generation._generate_tasks_for_epic",
                new_callable=AsyncMock,
                return_value=[],
            ),
        ):
            mock_jira = AsyncMock()
            MockJira.return_value = mock_jira
            mock_jira.get_issue = AsyncMock(
                side_effect=[
                    _make_issue("FEAT-1", project_key="MYPROJ"),
                    _make_issue("TASK-100", parent_key="EPIC-10"),
                    _make_issue("TASK-101", parent_key="EPIC-10"),
                    _make_issue("TASK-200", parent_key="EPIC-20"),
                    _make_issue("EPIC-10", summary="Epic 10", description="Plan 10"),
                    _make_issue("EPIC-20", summary="Epic 20", description="Plan 20"),
                    _make_issue("TASK-200", summary="Old task 200"),
                ]
            )
            mock_jira.get_labels = AsyncMock(return_value=[])
            mock_jira.archive_issue = AsyncMock()
            MockAgent.return_value = AsyncMock()

            result = await regenerate_epic_tasks(base_state)

        mock_jira.archive_issue.assert_not_awaited()
        assert result["task_keys"] == ["TASK-100", "TASK-101", "TASK-200"]
        assert result["current_node"] == "regenerate_epic_tasks"
        assert result["revision_requested"] is False
        assert result["feedback_comment"] is None
        assert result["current_epic_key"] is None
        assert "No replacement Tasks generated" in result["last_error"]

    @pytest.mark.asyncio
    async def test_partial_replacement_creation_cleans_up_new_tasks_and_keeps_old_tasks(
        self, base_state
    ):
        """Partial replacement creation must not archive existing epic tasks."""
        with (
            patch("forge.workflow.nodes.task_generation.JiraClient") as MockJira,
            patch("forge.workflow.nodes.task_generation.ForgeAgent") as MockAgent,
            patch(
                "forge.workflow.nodes.task_generation._generate_tasks_for_epic",
                new_callable=AsyncMock,
                return_value=[
                    {"summary": "New Task 1", "description": "D1", "repo": "acme/backend"},
                    {"summary": "New Task 2", "description": "D2", "repo": "acme/backend"},
                ],
            ),
        ):
            mock_jira = AsyncMock()
            MockJira.return_value = mock_jira
            mock_jira.get_issue = AsyncMock(
                side_effect=[
                    _make_issue("FEAT-1", project_key="MYPROJ"),
                    _make_issue("TASK-100", parent_key="EPIC-10"),
                    _make_issue("TASK-101", parent_key="EPIC-10"),
                    _make_issue("TASK-200", parent_key="EPIC-20"),
                    _make_issue("EPIC-10", summary="Epic 10", description="Plan 10"),
                    _make_issue("EPIC-20", summary="Epic 20", description="Plan 20"),
                    _make_issue("TASK-200", summary="Old task 200"),
                ]
            )
            mock_jira.get_labels = AsyncMock(return_value=["repo:acme/backend"])
            mock_jira.create_task = AsyncMock(
                side_effect=["TASK-102", RuntimeError("Jira create failed")]
            )
            mock_jira.archive_issue = AsyncMock()
            MockAgent.return_value = AsyncMock()

            result = await regenerate_epic_tasks(base_state)

        archived_keys = [call.args[0] for call in mock_jira.archive_issue.call_args_list]
        assert archived_keys == ["TASK-102"]
        assert "TASK-100" not in archived_keys
        assert "TASK-101" not in archived_keys
        assert result["task_keys"] == ["TASK-100", "TASK-101", "TASK-200"]
        assert result["current_node"] == "regenerate_epic_tasks"
        assert result["revision_requested"] is False
        assert result["feedback_comment"] is None
        assert result["current_epic_key"] is None
        assert "Partial replacement Task creation failed" in result["last_error"]

    @pytest.mark.asyncio
    async def test_error_path_clears_revision_flags_to_prevent_gate_loop(self, base_state):
        """An exception in regenerate_epic_tasks must clear revision flags so task_approval_gate returns END."""
        with (
            patch("forge.workflow.nodes.task_generation.JiraClient") as MockJira,
            patch("forge.workflow.nodes.task_generation.ForgeAgent") as MockAgent,
            patch("forge.workflow.nodes.error_handler.notify_error", new_callable=AsyncMock),
        ):
            mock_jira = AsyncMock()
            MockJira.return_value = mock_jira
            mock_jira.get_issue = AsyncMock(side_effect=RuntimeError("Jira unavailable"))
            mock_jira.close = AsyncMock()
            MockAgent.return_value = AsyncMock()

            result = await regenerate_epic_tasks(base_state)

        assert result["revision_requested"] is False
        assert result["feedback_comment"] is None
        assert result["current_epic_key"] is None

    @pytest.mark.asyncio
    async def test_orphaned_task_with_none_parent_logged_as_warning(self, base_state, caplog):
        """A task whose parent_key is None must log a specific warning, not silently misclassify."""
        import logging

        with (
            patch("forge.workflow.nodes.task_generation.JiraClient") as MockJira,
            patch("forge.workflow.nodes.task_generation.ForgeAgent") as MockAgent,
            patch(
                "forge.workflow.nodes.task_generation._generate_tasks_for_epic",
                new_callable=AsyncMock,
                return_value=[],
            ),
        ):
            mock_jira = AsyncMock()
            MockJira.return_value = mock_jira
            mock_jira.get_issue = AsyncMock(
                side_effect=[
                    _make_issue("FEAT-1", project_key="MYPROJ"),  # parent feature
                    _make_issue("TASK-100", parent_key=None),  # orphaned — no parent
                    _make_issue("TASK-101", parent_key="EPIC-10"),  # belongs to target epic
                    _make_issue("TASK-200", parent_key="EPIC-20"),  # other epic
                    _make_issue("EPIC-10", summary="Epic 10", description="Plan 10"),
                    _make_issue("EPIC-20", summary="Epic 20", description="Plan 20"),
                    _make_issue("TASK-200", summary="Old task 200"),  # remaining task ctx
                ]
            )
            mock_jira.get_labels = AsyncMock(return_value=[])
            mock_jira.archive_issue = AsyncMock()
            MockAgent.return_value = AsyncMock()

            with caplog.at_level(logging.WARNING, logger="forge.workflow.nodes.task_generation"):
                await regenerate_epic_tasks(base_state)

        orphan_warnings = [
            r for r in caplog.records if "TASK-100" in r.message and "parent" in r.message.lower()
        ]
        assert orphan_warnings, "Expected a warning about the orphaned task TASK-100"
