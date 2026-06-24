"""Unit tests for plan_bug_fix, plan_approval_gate, route_plan_approval,
regenerate_plan, and decompose_plan nodes."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from langgraph.graph import END

from forge.models.workflow import ForgeLabel, TicketType
from forge.workflow.nodes.plan_bug_fix import (
    decompose_plan,
    plan_approval_gate,
    plan_bug_fix,
    regenerate_plan,
    route_plan_approval,
)


@pytest.fixture
def base_bug_state():
    """Minimal BugState at the point plan_bug_fix is entered."""
    return {
        "ticket_key": "BUG-42",
        "ticket_type": TicketType.BUG,
        "current_node": "plan_bug_fix",
        "is_paused": False,
        "retry_count": 0,
        "last_error": None,
        "rca_content": "Password validator rejects valid special characters.",
        "rca_options": [
            {"title": "Fix regex", "description": "Update pattern.", "tradeoffs": "Low risk."},
            {
                "title": "Escape chars",
                "description": "Escape before validate.",
                "tradeoffs": "Higher complexity.",
            },
        ],
        "selected_fix_option": 1,
        "selected_fix_approach": {
            "title": "Fix regex",
            "description": "Update pattern.",
            "tradeoffs": "Low risk.",
        },
        "plan_content": None,
        "linked_task_keys": [],
        "feedback_comment": None,
        "revision_requested": False,
        "is_question": False,
        "context": {"summary": "Login fails with special char passwords"},
        "messages": [],
        "qa_history": [],
        "generation_context": {},
        "current_repo": "acme/backend",
    }


def _make_mock_jira(summary="Login fails with special char passwords", project_key="BUG"):
    jira = AsyncMock()
    issue = MagicMock()
    issue.summary = summary
    issue.description = "Bug description"
    issue.project_key = project_key
    jira.get_issue = AsyncMock(return_value=issue)
    jira.add_comment = AsyncMock()
    jira.set_workflow_label = AsyncMock()
    jira.create_task = AsyncMock(return_value="BUG-50")
    jira.create_issue_link = AsyncMock()
    jira.get_issue_links = AsyncMock(return_value=[])
    jira.get_labels = AsyncMock(return_value=[])
    jira.close = AsyncMock()
    return jira


def _make_mock_runner_success(plan_content="## Plan\n\nFix the regex.\n\nrepo:acme/backend"):
    class _FakeRunner:
        async def run(self, workspace_path, **_kwargs):
            forge_dir = workspace_path / ".forge"
            forge_dir.mkdir(exist_ok=True)
            (forge_dir / "plan.md").write_text(plan_content)
            result = MagicMock()
            result.success = True
            result.exit_code = 0
            result.stdout = "Done"
            result.stderr = ""
            return result

    return _FakeRunner()


def _make_mock_runner_failure():
    runner = MagicMock()
    result = MagicMock()
    result.success = False
    result.exit_code = 1
    result.stdout = ""
    result.stderr = "Container failed"
    runner.run = AsyncMock(return_value=result)
    return runner


def _make_mock_runner_no_file():
    class _FakeRunner:
        async def run(self, workspace_path, **_kwargs):
            forge_dir = workspace_path / ".forge"
            forge_dir.mkdir(exist_ok=True)
            result = MagicMock()
            result.success = True
            result.exit_code = 0
            result.stdout = "Done"
            result.stderr = ""
            return result

    return _FakeRunner()


class TestPlanBugFix:
    """Tests for the plan_bug_fix node."""

    @pytest.mark.asyncio
    async def test_container_invoked_with_rca_and_fix_approach(self, base_bug_state):
        """Container receives full RCA content and the selected fix approach."""
        mock_jira = _make_mock_jira()
        captured_desc = []

        class _CapturingRunner:
            async def run(self, workspace_path, task_description="", **_kwargs):
                captured_desc.append(task_description)
                forge_dir = workspace_path / ".forge"
                forge_dir.mkdir(exist_ok=True)
                (forge_dir / "plan.md").write_text("## Plan\n\nFix it.")
                result = MagicMock()
                result.success = True
                result.exit_code = 0
                result.stdout = "Done"
                result.stderr = ""
                return result

        with (
            patch("forge.workflow.nodes.plan_bug_fix.JiraClient", return_value=mock_jira),
            patch(
                "forge.workflow.nodes.plan_bug_fix.ContainerRunner", return_value=_CapturingRunner()
            ),
        ):
            await plan_bug_fix(base_bug_state)

        assert captured_desc, "runner.run was not called"
        desc = captured_desc[0]
        assert "Password validator" in desc or "Fix regex" in desc

    @pytest.mark.asyncio
    async def test_plan_content_stored_in_state(self, base_bug_state):
        """plan_content is populated from container output on success."""
        mock_jira = _make_mock_jira()
        runner = _make_mock_runner_success("## Plan\n\nUpdate the regex pattern.")

        with (
            patch("forge.workflow.nodes.plan_bug_fix.JiraClient", return_value=mock_jira),
            patch("forge.workflow.nodes.plan_bug_fix.ContainerRunner", return_value=runner),
        ):
            result = await plan_bug_fix(base_bug_state)

        assert result["plan_content"] == "## Plan\n\nUpdate the regex pattern."

    @pytest.mark.asyncio
    async def test_jira_comment_posted_with_plan(self, base_bug_state):
        """Plan content is posted as a Jira comment (plus an ack comment at the start)."""
        mock_jira = _make_mock_jira()
        runner = _make_mock_runner_success()

        with (
            patch("forge.workflow.nodes.plan_bug_fix.JiraClient", return_value=mock_jira),
            patch("forge.workflow.nodes.plan_bug_fix.ContainerRunner", return_value=runner),
        ):
            await plan_bug_fix(base_bug_state)

        # Two comments: ack at start, plan at end
        assert mock_jira.add_comment.call_count == 2
        ack_comment = mock_jira.add_comment.call_args_list[0][0][1]
        plan_comment = mock_jira.add_comment.call_args_list[1][0][1]
        assert "working on" in ack_comment.lower() or "plan" in ack_comment.lower()
        assert "Plan" in plan_comment or "regex" in plan_comment.lower()

    @pytest.mark.asyncio
    async def test_plan_pending_label_set(self, base_bug_state):
        """forge:plan-pending label is set on the ticket."""
        mock_jira = _make_mock_jira()
        runner = _make_mock_runner_success()

        with (
            patch("forge.workflow.nodes.plan_bug_fix.JiraClient", return_value=mock_jira),
            patch("forge.workflow.nodes.plan_bug_fix.ContainerRunner", return_value=runner),
        ):
            await plan_bug_fix(base_bug_state)

        mock_jira.set_workflow_label.assert_called_once_with("BUG-42", ForgeLabel.PLAN_PENDING)

    @pytest.mark.asyncio
    async def test_routes_to_plan_approval_gate_on_success(self, base_bug_state):
        """On success, current_node is plan_approval_gate."""
        mock_jira = _make_mock_jira()
        runner = _make_mock_runner_success()

        with (
            patch("forge.workflow.nodes.plan_bug_fix.JiraClient", return_value=mock_jira),
            patch("forge.workflow.nodes.plan_bug_fix.ContainerRunner", return_value=runner),
        ):
            result = await plan_bug_fix(base_bug_state)

        assert result["current_node"] == "plan_approval_gate"

    @pytest.mark.asyncio
    async def test_comment_truncated_at_25k_chars(self, base_bug_state):
        """Plan comment is truncated at 25k characters with a truncation note appended."""
        long_plan = "A" * 30_000
        mock_jira = _make_mock_jira()
        runner = _make_mock_runner_success(long_plan)

        with (
            patch("forge.workflow.nodes.plan_bug_fix.JiraClient", return_value=mock_jira),
            patch("forge.workflow.nodes.plan_bug_fix.ContainerRunner", return_value=runner),
        ):
            await plan_bug_fix(base_bug_state)

        comment = mock_jira.add_comment.call_args[0][1]
        assert len(comment) <= 25_500
        assert "truncated" in comment.lower()

    @pytest.mark.asyncio
    async def test_container_failure_triggers_retry(self, base_bug_state):
        """Container failure increments retry_count and keeps current_node as plan_bug_fix."""
        mock_jira = _make_mock_jira()
        runner = _make_mock_runner_failure()

        with (
            patch("forge.workflow.nodes.plan_bug_fix.JiraClient", return_value=mock_jira),
            patch("forge.workflow.nodes.plan_bug_fix.ContainerRunner", return_value=runner),
        ):
            result = await plan_bug_fix(base_bug_state)

        assert result["retry_count"] == 1
        assert result["last_error"] is not None
        assert result["current_node"] == "plan_bug_fix"

    @pytest.mark.asyncio
    async def test_three_failures_escalate_blocked(self, base_bug_state):
        """After 3 container failures, preserve retry node for graph escalation."""
        base_bug_state["retry_count"] = 2
        mock_jira = _make_mock_jira()
        runner = _make_mock_runner_failure()

        with (
            patch("forge.workflow.nodes.plan_bug_fix.JiraClient", return_value=mock_jira),
            patch("forge.workflow.nodes.plan_bug_fix.ContainerRunner", return_value=runner),
        ):
            result = await plan_bug_fix(base_bug_state)

        assert result["current_node"] == "plan_bug_fix"
        assert result["retry_count"] == 3

    @pytest.mark.asyncio
    async def test_failure_retries_to_plan_bug_fix(self, base_bug_state):
        """Container failure (before limit) routes back to plan_bug_fix, not regenerate_plan."""
        mock_jira = _make_mock_jira()
        runner = _make_mock_runner_failure()

        with (
            patch("forge.workflow.nodes.plan_bug_fix.JiraClient", return_value=mock_jira),
            patch("forge.workflow.nodes.plan_bug_fix.ContainerRunner", return_value=runner),
        ):
            result = await plan_bug_fix(base_bug_state)

        assert result["current_node"] == "plan_bug_fix"

    @pytest.mark.asyncio
    async def test_missing_plan_file_triggers_retry(self, base_bug_state):
        """If plan.md is not written by the container, error is logged and retry triggered."""
        mock_jira = _make_mock_jira()
        runner = _make_mock_runner_no_file()

        with (
            patch("forge.workflow.nodes.plan_bug_fix.JiraClient", return_value=mock_jira),
            patch("forge.workflow.nodes.plan_bug_fix.ContainerRunner", return_value=runner),
        ):
            result = await plan_bug_fix(base_bug_state)

        assert result["retry_count"] == 1
        assert result["last_error"] is not None


class TestPlanApprovalGate:
    """Tests for the plan_approval_gate pause gate."""

    def test_set_paused_with_correct_node_name(self, base_bug_state):
        """plan_approval_gate sets is_paused=True and current_node='plan_approval_gate'."""
        result = plan_approval_gate(base_bug_state)

        assert result["is_paused"] is True
        assert result["current_node"] == "plan_approval_gate"

    def test_state_otherwise_unchanged(self, base_bug_state):
        """Other state fields are preserved."""
        result = plan_approval_gate(base_bug_state)

        assert result["ticket_key"] == "BUG-42"
        assert result["rca_content"] == base_bug_state["rca_content"]


class TestRoutePlanApproval:
    """Tests for the route_plan_approval routing function."""

    def test_routing_is_question_routes_to_answer_question(self, base_bug_state):
        """is_question=True → routes to answer_question."""
        base_bug_state["is_question"] = True
        assert route_plan_approval(base_bug_state) == "answer_question"

    def test_routing_paused_returns_end(self, base_bug_state):
        """is_paused=True with no other flags → returns END."""
        base_bug_state["is_paused"] = True
        assert route_plan_approval(base_bug_state) == END

    def test_routing_revision_requested_routes_to_regenerate(self, base_bug_state):
        """revision_requested=True → routes to regenerate_plan."""
        base_bug_state["revision_requested"] = True
        base_bug_state["feedback_comment"] = "Needs more detail."
        assert route_plan_approval(base_bug_state) == "regenerate_plan"

    def test_routing_plan_approved_routes_to_decompose(self, base_bug_state):
        """When not paused and no other flags → routes to decompose_plan."""
        base_bug_state["is_paused"] = False
        assert route_plan_approval(base_bug_state) == "decompose_plan"

    def test_is_question_takes_priority_over_paused(self, base_bug_state):
        """is_question=True takes priority over is_paused=True."""
        base_bug_state["is_question"] = True
        base_bug_state["is_paused"] = True
        assert route_plan_approval(base_bug_state) == "answer_question"

    def test_is_paused_takes_priority_over_revision_requested(self, base_bug_state):
        """is_paused=True takes priority over revision_requested=True."""
        base_bug_state["is_paused"] = True
        base_bug_state["revision_requested"] = True
        assert route_plan_approval(base_bug_state) == END


class TestRegeneratePlan:
    """Tests for the regenerate_plan node."""

    @pytest.fixture
    def regen_state(self, base_bug_state):
        return {
            **base_bug_state,
            "current_node": "regenerate_plan",
            "plan_content": "## Old Plan\n\nOriginal fix approach.",
            "feedback_comment": "The plan needs to address migration scripts.",
            "revision_requested": True,
        }

    @pytest.mark.asyncio
    async def test_feedback_passed_to_container(self, regen_state):
        """User feedback from feedback_comment is passed as context when regenerating."""
        mock_jira = _make_mock_jira()
        captured_desc = []

        class _CapturingRunner:
            async def run(self, workspace_path, task_description="", **_kwargs):
                captured_desc.append(task_description)
                forge_dir = workspace_path / ".forge"
                forge_dir.mkdir(exist_ok=True)
                (forge_dir / "plan.md").write_text("## Revised Plan\n\nNow includes migration.")
                result = MagicMock()
                result.success = True
                result.exit_code = 0
                result.stdout = "Done"
                result.stderr = ""
                return result

        with (
            patch("forge.workflow.nodes.plan_bug_fix.JiraClient", return_value=mock_jira),
            patch(
                "forge.workflow.nodes.plan_bug_fix.ContainerRunner", return_value=_CapturingRunner()
            ),
        ):
            await regenerate_plan(regen_state)

        assert captured_desc, "runner.run was not called"
        assert "migration" in captured_desc[0].lower() or "feedback" in captured_desc[0].lower()

    @pytest.mark.asyncio
    async def test_routes_to_plan_approval_gate(self, regen_state):
        """After plan regeneration, current_node is plan_approval_gate."""
        mock_jira = _make_mock_jira()
        runner = _make_mock_runner_success("## Revised Plan\n\nNow includes migration.")

        with (
            patch("forge.workflow.nodes.plan_bug_fix.JiraClient", return_value=mock_jira),
            patch("forge.workflow.nodes.plan_bug_fix.ContainerRunner", return_value=runner),
        ):
            result = await regenerate_plan(regen_state)

        assert result["current_node"] == "plan_approval_gate"

    @pytest.mark.asyncio
    async def test_clears_revision_flags(self, regen_state):
        """regenerate_plan clears revision_requested and feedback_comment on success."""
        mock_jira = _make_mock_jira()
        runner = _make_mock_runner_success("## Revised Plan")

        with (
            patch("forge.workflow.nodes.plan_bug_fix.JiraClient", return_value=mock_jira),
            patch("forge.workflow.nodes.plan_bug_fix.ContainerRunner", return_value=runner),
        ):
            result = await regenerate_plan(regen_state)

        assert result["revision_requested"] is False
        assert result["feedback_comment"] is None

    @pytest.mark.asyncio
    async def test_failure_retries_to_regenerate_plan(self, regen_state):
        """Container failure (before limit) routes back to regenerate_plan, not plan_bug_fix."""
        mock_jira = _make_mock_jira()
        runner = _make_mock_runner_failure()

        with (
            patch("forge.workflow.nodes.plan_bug_fix.JiraClient", return_value=mock_jira),
            patch("forge.workflow.nodes.plan_bug_fix.ContainerRunner", return_value=runner),
        ):
            result = await regenerate_plan(regen_state)

        assert result["current_node"] == "regenerate_plan"


class TestDecomposePlan:
    """Tests for the decompose_plan node (no container)."""

    @pytest.fixture
    def plan_state(self, base_bug_state):
        return {
            **base_bug_state,
            "current_node": "decompose_plan",
            "plan_content": (
                "## Bug Fix Plan\n\n"
                "Change the validator in repo:acme/backend.\n"
                "Update tests in repo:acme/backend."
            ),
        }

    @pytest.mark.asyncio
    async def test_repos_parsed_from_plan_content(self, plan_state):
        """repos are extracted from plan_content via 'repo:<name>' pattern."""
        mock_jira = _make_mock_jira()

        with patch("forge.workflow.nodes.plan_bug_fix.JiraClient", return_value=mock_jira):
            await decompose_plan(plan_state)

        # Only one unique repo (acme/backend appears twice but deduplicated)
        mock_jira.create_task.assert_called_once()
        call_kwargs = mock_jira.create_task.call_args
        summary = call_kwargs[1].get("summary") or call_kwargs[0][1]
        assert "acme/backend" in summary

    @pytest.mark.asyncio
    async def test_one_task_created_per_repo(self, plan_state):
        """One Jira task is created for each unique identified repo."""
        plan_state["plan_content"] = (
            "Fix auth in repo:acme/backend.\nUpdate docs in repo:acme/frontend.\n"
        )
        mock_jira = _make_mock_jira()
        mock_jira.create_task = AsyncMock(side_effect=["BUG-50", "BUG-51"])

        with patch("forge.workflow.nodes.plan_bug_fix.JiraClient", return_value=mock_jira):
            result = await decompose_plan(plan_state)

        assert mock_jira.create_task.call_count == 2
        assert set(result["linked_task_keys"]) == {"BUG-50", "BUG-51"}

    @pytest.mark.asyncio
    async def test_task_summary_format(self, plan_state):
        """Task summary is 'Fix: {bug_summary} ({repo_name})'."""
        mock_jira = _make_mock_jira(summary="Login fails with special char passwords")

        with patch("forge.workflow.nodes.plan_bug_fix.JiraClient", return_value=mock_jira):
            await decompose_plan(plan_state)

        call_kwargs = mock_jira.create_task.call_args
        summary = call_kwargs[1].get("summary") or call_kwargs[0][1]
        assert summary == "Fix: Login fails with special char passwords (acme/backend)"

    @pytest.mark.asyncio
    async def test_task_has_correct_labels(self, plan_state):
        """Each task has 'repo:<name>', 'forge:managed', and 'forge:parent:<key>' labels."""
        mock_jira = _make_mock_jira()

        with patch("forge.workflow.nodes.plan_bug_fix.JiraClient", return_value=mock_jira):
            await decompose_plan(plan_state)

        call_kwargs = mock_jira.create_task.call_args
        labels = call_kwargs[1].get("labels") or call_kwargs[0][4]
        assert "repo:acme/backend" in labels
        assert ForgeLabel.FORGE_MANAGED.value in labels
        assert "forge:parent:BUG-42" in labels

    @pytest.mark.asyncio
    async def test_task_linked_to_bug_via_implements(self, plan_state):
        """create_issue_link('implements', task_key, bug_key) called for each task."""
        mock_jira = _make_mock_jira()
        mock_jira.create_task = AsyncMock(return_value="BUG-50")

        with patch("forge.workflow.nodes.plan_bug_fix.JiraClient", return_value=mock_jira):
            await decompose_plan(plan_state)

        mock_jira.create_issue_link.assert_called_once_with("Related", "BUG-50", "BUG-42")

    @pytest.mark.asyncio
    async def test_all_task_keys_stored_in_linked_task_keys(self, plan_state):
        """linked_task_keys and task_keys both contain all created task keys."""
        mock_jira = _make_mock_jira()
        mock_jira.create_task = AsyncMock(return_value="BUG-50")

        with patch("forge.workflow.nodes.plan_bug_fix.JiraClient", return_value=mock_jira):
            result = await decompose_plan(plan_state)

        assert "BUG-50" in result["linked_task_keys"]
        assert "BUG-50" in result["task_keys"]

    @pytest.mark.asyncio
    async def test_no_repos_found_defaults_to_project_repos(self, plan_state):
        """If plan_content has no repo: tags, falls back to Jira project repos."""
        plan_state["plan_content"] = "## Plan\n\nFix the validator."
        mock_jira = _make_mock_jira()
        mock_jira.get_project_repos = AsyncMock(return_value=["acme/backend"])

        with patch("forge.workflow.nodes.plan_bug_fix.JiraClient", return_value=mock_jira):
            await decompose_plan(plan_state)

        mock_jira.create_task.assert_called_once()
        call_kwargs = mock_jira.create_task.call_args
        summary = call_kwargs[1].get("summary") or call_kwargs[0][1]
        assert "acme/backend" in summary

    @pytest.mark.asyncio
    async def test_idempotency_existing_task_reused(self, plan_state):
        """If a task with matching repo: label already exists, reuse it."""
        mock_jira = _make_mock_jira()
        mock_jira.get_issue_links = AsyncMock(
            return_value=[
                {"type": "Related", "inward_key": "BUG-50", "outward_key": None},
            ]
        )
        mock_jira.get_labels = AsyncMock(return_value=["repo:acme/backend", "forge:managed"])

        with patch("forge.workflow.nodes.plan_bug_fix.JiraClient", return_value=mock_jira):
            result = await decompose_plan(plan_state)

        # No new task should be created since acme/backend is already covered
        mock_jira.create_task.assert_not_called()
        assert "BUG-50" in result["linked_task_keys"]

    @pytest.mark.asyncio
    async def test_idempotency_partial_coverage(self, plan_state):
        """Creates only missing repo tasks; reuses existing ones for already-covered repos."""
        plan_state["plan_content"] = (
            "Fix auth in repo:acme/backend.\nUpdate docs in repo:acme/frontend.\n"
        )
        mock_jira = _make_mock_jira()
        mock_jira.get_issue_links = AsyncMock(
            return_value=[
                {"type": "Related", "inward_key": "BUG-50", "outward_key": None},
            ]
        )
        mock_jira.get_labels = AsyncMock(return_value=["repo:acme/backend", "forge:managed"])
        mock_jira.create_task = AsyncMock(return_value="BUG-51")

        with patch("forge.workflow.nodes.plan_bug_fix.JiraClient", return_value=mock_jira):
            result = await decompose_plan(plan_state)

        # Only acme/frontend should be created (acme/backend already exists)
        mock_jira.create_task.assert_called_once()
        call_kwargs = mock_jira.create_task.call_args
        summary = call_kwargs[1].get("summary") or call_kwargs[0][1]
        assert "acme/frontend" in summary
        assert set(result["linked_task_keys"]) == {"BUG-50", "BUG-51"}

    @pytest.mark.asyncio
    async def test_partial_failure_escalates(self, plan_state):
        """If any new task creation fails, escalate immediately."""
        mock_jira = _make_mock_jira()
        mock_jira.create_task = AsyncMock(side_effect=RuntimeError("Jira unavailable"))

        with patch("forge.workflow.nodes.plan_bug_fix.JiraClient", return_value=mock_jira):
            result = await decompose_plan(plan_state)

        assert result["current_node"] == "decompose_plan"
        assert result["last_error"] is not None

    @pytest.mark.asyncio
    async def test_no_repo_tags_and_no_project_repos_escalates(self, plan_state):
        """Missing repo information blocks before workspace setup."""
        plan_state["plan_content"] = "## Plan\n\nFix the bug without explicit repo tags."
        mock_jira = _make_mock_jira()
        mock_jira.get_project_repos = AsyncMock(return_value=[])

        with patch("forge.workflow.nodes.plan_bug_fix.JiraClient", return_value=mock_jira):
            result = await decompose_plan(plan_state)

        assert result["current_node"] == "decompose_plan"
        assert "No repositories found" in result["last_error"]
        mock_jira.create_task.assert_not_called()

    @pytest.mark.asyncio
    async def test_success_routes_to_setup_workspace(self, plan_state):
        """On success, current_node is setup_workspace."""
        mock_jira = _make_mock_jira()

        with patch("forge.workflow.nodes.plan_bug_fix.JiraClient", return_value=mock_jira):
            result = await decompose_plan(plan_state)

        assert result["current_node"] == "setup_workspace"

    @pytest.mark.asyncio
    async def test_success_resets_retry_count(self, plan_state):
        """decompose_plan resets retry_count to 0 on success."""
        plan_state["retry_count"] = 2
        mock_jira = _make_mock_jira()

        with patch("forge.workflow.nodes.plan_bug_fix.JiraClient", return_value=mock_jira):
            result = await decompose_plan(plan_state)

        assert result["retry_count"] == 0

    @pytest.mark.asyncio
    async def test_get_labels_failure_skips_that_link(self, plan_state):
        """If get_labels fails for an existing linked task, it is skipped (not escalated)."""
        mock_jira = _make_mock_jira()
        mock_jira.get_issue_links = AsyncMock(
            return_value=[
                {"type": "implements", "inward_key": "BUG-DELETED", "outward_key": None},
            ]
        )
        mock_jira.get_labels = AsyncMock(side_effect=RuntimeError("Issue not found"))
        mock_jira.create_task = AsyncMock(return_value="BUG-50")

        with patch("forge.workflow.nodes.plan_bug_fix.JiraClient", return_value=mock_jira):
            result = await decompose_plan(plan_state)

        # Should still succeed — skipped the bad link, created a new task
        assert result["current_node"] == "setup_workspace"
        mock_jira.create_task.assert_called_once()
