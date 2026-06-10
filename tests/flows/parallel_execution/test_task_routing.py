"""Flow tests for multi-repo task routing and parallel execution."""

from unittest.mock import patch

import pytest

from forge.workflow.nodes.task_router import (
    get_repo_execution_plan,
    route_after_pr,
    route_tasks_parallel,
    should_use_parallel_execution,
)
from tests.fixtures.workflow_states import (
    make_workflow_state,
)


class TestTaskRouterByRepo:
    """route_tasks_by_repo initialises state for sequential repo execution."""

    @pytest.mark.asyncio
    async def test_single_repo_initialises_state(self):
        """Single-repo feature sets repos_to_process and current_repo."""
        state = make_workflow_state(
            current_node="task_router",
            tasks_by_repo={"org/backend": ["TEST-200", "TEST-201"]},
        )

        with patch("forge.workflow.nodes.task_router.update_state_timestamp", side_effect=lambda s: s):
            from forge.workflow.nodes.task_router import route_tasks_by_repo
            result = await route_tasks_by_repo(state)

        assert result["repos_to_process"] == ["org/backend"]
        assert result["current_repo"] == "org/backend"
        assert result["repos_completed"] == []
        assert result["current_node"] == "setup_workspace"

    @pytest.mark.asyncio
    async def test_multi_repo_sets_first_repo_as_current(self):
        """With multiple repos, the first repo becomes current_repo."""
        state = make_workflow_state(
            current_node="task_router",
            tasks_by_repo={
                "org/backend": ["TEST-200"],
                "org/frontend": ["TEST-201"],
            },
        )

        with patch("forge.workflow.nodes.task_router.update_state_timestamp", side_effect=lambda s: s):
            from forge.workflow.nodes.task_router import route_tasks_by_repo
            result = await route_tasks_by_repo(state)

        assert len(result["repos_to_process"]) == 2
        assert result["current_repo"] in ["org/backend", "org/frontend"]
        assert result["repos_completed"] == []

    @pytest.mark.asyncio
    async def test_empty_tasks_by_repo_sets_error(self):
        """No tasks_by_repo is an error — no work to do."""
        state = make_workflow_state(
            current_node="task_router",
            tasks_by_repo={},
        )

        with patch("forge.workflow.nodes.task_router.update_state_timestamp", side_effect=lambda s: s):
            from forge.workflow.nodes.task_router import route_tasks_by_repo
            result = await route_tasks_by_repo(state)

        assert result["last_error"] is not None


class TestRouteAfterPR:
    """route_after_pr advances to the next repo or signals completion."""

    def test_more_repos_remaining_routes_to_setup(self):
        """When repos remain unprocessed, route back to setup_workspace."""
        state = make_workflow_state(
            current_node="teardown_workspace",
            repos_to_process=["org/backend", "org/frontend"],
            repos_completed=["org/backend"],
            current_repo="org/backend",
        )

        result = route_after_pr(state)

        assert result == "setup_workspace"

    def test_last_repo_done_routes_to_complete(self):
        """When all repos are processed, route to complete."""
        state = make_workflow_state(
            current_node="teardown_workspace",
            repos_to_process=["org/backend", "org/frontend"],
            repos_completed=["org/backend"],
            current_repo="org/frontend",
        )

        result = route_after_pr(state)

        assert result == "complete"

    def test_single_repo_routes_to_complete(self):
        """A single-repo feature completes after the one repo is done."""
        state = make_workflow_state(
            current_node="teardown_workspace",
            repos_to_process=["org/backend"],
            repos_completed=[],
            current_repo="org/backend",
        )

        result = route_after_pr(state)

        assert result == "complete"

    def test_three_repos_progresses_sequentially(self):
        """Three-repo feature advances one repo at a time."""
        state = make_workflow_state(
            repos_to_process=["org/a", "org/b", "org/c"],
            repos_completed=["org/a"],
            current_repo="org/b",
        )

        # After org/b PR, one more remains
        result = route_after_pr(state)
        assert result == "setup_workspace"

        # After org/c PR (marking org/b and org/c done), all done
        state["repos_completed"] = ["org/a", "org/b"]
        state["current_repo"] = "org/c"
        result = route_after_pr(state)
        assert result == "complete"


class TestExecutionPlan:
    """get_repo_execution_plan returns the per-repo task breakdown."""

    def test_plan_includes_all_repos(self):
        """Execution plan covers every repo in tasks_by_repo."""
        state = make_workflow_state(
            tasks_by_repo={
                "org/backend": ["TEST-200", "TEST-201"],
                "org/frontend": ["TEST-202"],
            },
        )

        plan = get_repo_execution_plan(state)

        repos = [entry["repo"] for entry in plan]
        assert "org/backend" in repos
        assert "org/frontend" in repos

    def test_plan_shows_correct_task_counts(self):
        """Each plan entry has an accurate task_count."""
        state = make_workflow_state(
            tasks_by_repo={
                "org/backend": ["TEST-200", "TEST-201"],
                "org/frontend": ["TEST-202"],
            },
        )

        plan = get_repo_execution_plan(state)
        by_repo = {entry["repo"]: entry for entry in plan}

        assert by_repo["org/backend"]["task_count"] == 2
        assert by_repo["org/frontend"]["task_count"] == 1

    def test_completed_repos_marked_done(self):
        """Repos in repos_completed are marked 'completed' in the plan."""
        state = make_workflow_state(
            tasks_by_repo={
                "org/backend": ["TEST-200"],
                "org/frontend": ["TEST-201"],
            },
            repos_completed=["org/backend"],
        )

        plan = get_repo_execution_plan(state)
        by_repo = {entry["repo"]: entry for entry in plan}

        assert by_repo["org/backend"]["status"] == "completed"
        assert by_repo["org/frontend"]["status"] == "pending"

    def test_empty_tasks_returns_empty_plan(self):
        """No tasks_by_repo yields an empty plan."""
        state = make_workflow_state(tasks_by_repo={})

        plan = get_repo_execution_plan(state)

        assert plan == []


class TestParallelExecutionDecision:
    """should_use_parallel_execution gates the parallel fan-out."""

    def test_multiple_repos_with_flag_enabled(self):
        """Multiple repos and parallel_execution_enabled=True → use parallel."""
        state = make_workflow_state(
            tasks_by_repo={
                "org/backend": ["TEST-200"],
                "org/frontend": ["TEST-201"],
            },
            parallel_execution_enabled=True,
        )

        assert should_use_parallel_execution(state) is True

    def test_single_repo_does_not_use_parallel(self):
        """Single repo has no benefit from parallel execution."""
        state = make_workflow_state(
            tasks_by_repo={"org/backend": ["TEST-200"]},
            parallel_execution_enabled=True,
        )

        assert should_use_parallel_execution(state) is False

    def test_flag_disabled_does_not_use_parallel(self):
        """Parallel execution flag=False disables fan-out even with multiple repos."""
        state = make_workflow_state(
            tasks_by_repo={
                "org/backend": ["TEST-200"],
                "org/frontend": ["TEST-201"],
            },
            parallel_execution_enabled=False,
        )

        assert should_use_parallel_execution(state) is False


class TestParallelFanOut:
    """route_tasks_parallel returns Send objects for LangGraph fan-out."""

    def test_parallel_returns_send_list(self):
        """Each repo gets a Send object for independent execution."""
        from langgraph.types import Send

        state = make_workflow_state(
            tasks_by_repo={
                "org/backend": ["TEST-200", "TEST-201"],
                "org/frontend": ["TEST-202"],
            },
            parallel_execution_enabled=True,
            task_keys=["TEST-200", "TEST-201", "TEST-202"],
        )

        result = route_tasks_parallel(state)

        assert isinstance(result, list)
        assert len(result) == 2
        assert all(isinstance(item, Send) for item in result)

    def test_parallel_send_targets_setup_workspace(self):
        """Each Send targets the setup_workspace node."""

        state = make_workflow_state(
            tasks_by_repo={
                "org/backend": ["TEST-200"],
                "org/frontend": ["TEST-201"],
            },
            parallel_execution_enabled=True,
            task_keys=["TEST-200", "TEST-201"],
        )

        result = route_tasks_parallel(state)

        assert all(item.node == "setup_workspace" for item in result)

    def test_parallel_each_branch_gets_correct_repo(self):
        """Each branch state has the correct current_repo assigned."""

        state = make_workflow_state(
            tasks_by_repo={
                "org/backend": ["TEST-200"],
                "org/frontend": ["TEST-201"],
            },
            parallel_execution_enabled=True,
            task_keys=["TEST-200", "TEST-201"],
        )

        result = route_tasks_parallel(state)

        repos = {item.arg["current_repo"] for item in result}
        assert repos == {"org/backend", "org/frontend"}

    def test_single_repo_falls_back_to_sequential(self):
        """Single repo returns a string (sequential) rather than Send list."""

        state = make_workflow_state(
            tasks_by_repo={"org/backend": ["TEST-200"]},
            parallel_execution_enabled=True,
            task_keys=["TEST-200"],
        )

        result = route_tasks_parallel(state)

        # Sequential path returns a string node name
        assert isinstance(result, str)


class TestMultiRepoScenarios:
    """End-to-end multi-repo routing scenarios."""

    def test_two_repo_sequential_scenario(self):
        """
        Scenario: Feature with backend + frontend repos, processed sequentially.

        task_router → setup backend → PR → teardown → setup frontend → PR → complete
        """
        # Initial routing sets up both repos
        state = make_workflow_state(
            repos_to_process=["org/backend", "org/frontend"],
            repos_completed=[],
            current_repo="org/backend",
        )

        # After backend PR created, one repo remains
        next_after_backend = route_after_pr(state)
        assert next_after_backend == "setup_workspace"

        # Now frontend is being processed
        state["repos_completed"] = ["org/backend"]
        state["current_repo"] = "org/frontend"

        # After frontend PR, all done
        next_after_frontend = route_after_pr(state)
        assert next_after_frontend == "complete"

    def test_execution_plan_reflects_progress(self):
        """Plan tracks which repos are done mid-workflow."""
        state = make_workflow_state(
            tasks_by_repo={
                "org/backend": ["TEST-200", "TEST-201"],
                "org/frontend": ["TEST-202", "TEST-203"],
            },
            repos_completed=["org/backend"],
        )

        plan = get_repo_execution_plan(state)
        by_repo = {e["repo"]: e for e in plan}

        assert by_repo["org/backend"]["status"] == "completed"
        assert by_repo["org/frontend"]["status"] == "pending"
        assert sum(e["task_count"] for e in plan) == 4
