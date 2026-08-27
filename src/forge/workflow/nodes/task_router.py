"""Task router node for grouping Tasks by repository.

Supports both sequential and parallel execution modes using
LangGraph's Send API for concurrent workspace spawning.
"""

import logging
from typing import Literal

from langgraph.types import Send

from forge.workflow.feature.state import FeatureState as WorkflowState
from forge.workflow.projections.task_routing import (
    project_repository_aggregation,
    project_task_routing,
)
from forge.workflow.reducers.task_routing import (
    reduce_repository_aggregation,
    reduce_task_routing,
)
from forge.workflow.stations.runner import invoke_builtin_station_sync
from forge.workflow.utils import update_state_timestamp

logger = logging.getLogger(__name__)


# Configuration for parallel execution
MAX_CONCURRENT_REPOS = 5  # Maximum concurrent workspace spawns


async def route_tasks_by_repo(state: WorkflowState) -> WorkflowState:
    """Route Tasks by grouping them by target repository.

    This node analyzes the tasks_by_repo mapping and prepares
    for sequential or parallel execution based on repository count.

    Args:
        state: Current workflow state with tasks_by_repo.

    Returns:
        Updated state ready for workspace setup.
    """
    request = project_task_routing(state)
    outcome = invoke_builtin_station_sync(request)
    update = reduce_task_routing(state, request, outcome)
    if outcome.output is not None:
        logger.info(
            "Routing %s tasks across %s repos for %s",
            outcome.output.task_count,
            len(outcome.output.repositories),
            state["ticket_key"],
        )
    return update_state_timestamp({**state, **update})


def route_after_pr(
    state: WorkflowState,
) -> Literal["setup_workspace", "complete", "task_router"]:
    """Route after PR creation to handle next repository or complete.

    Args:
        state: Current workflow state.

    Returns:
        Next node name.
    """
    repos_to_process = state.get("repos_to_process", [])
    repos_completed = state.get("repos_completed", [])
    current_repo = state.get("current_repo")

    # Mark current repo as completed
    if current_repo and current_repo not in repos_completed:
        repos_completed.append(current_repo)

    # Find next unprocessed repo
    remaining = [r for r in repos_to_process if r not in repos_completed]

    if remaining:
        logger.info(f"Moving to next repository: {remaining[0]}")
        return "setup_workspace"

    logger.info(f"All repositories processed for {state['ticket_key']}")
    return "complete"


def get_repo_execution_plan(state: WorkflowState) -> list[dict]:
    """Get the execution plan for all repositories.

    Args:
        state: Current workflow state.

    Returns:
        List of execution plans per repo.
    """
    tasks_by_repo = state.get("tasks_by_repo", {})
    repos_completed = state.get("repos_completed", [])

    plan = []
    for repo, tasks in tasks_by_repo.items():
        plan.append(
            {
                "repo": repo,
                "task_count": len(tasks),
                "tasks": tasks,
                "status": "completed" if repo in repos_completed else "pending",
            }
        )

    return plan


def route_tasks_parallel(
    state: WorkflowState,
) -> str | list[Send]:
    """Route tasks to parallel workspace execution using LangGraph Send API.

    This function implements fan-out pattern for concurrent repository
    processing. Each repository gets its own execution branch.

    Args:
        state: Current workflow state with tasks_by_repo.

    Returns:
        Either "setup_workspace" for sequential, or list[Send] for parallel.
    """
    ticket_key = state["ticket_key"]
    tasks_by_repo = state.get("tasks_by_repo", {})
    parallel_enabled = state.get("parallel_execution_enabled", True)

    if not tasks_by_repo:
        logger.warning(f"No tasks to route for {ticket_key}")
        return "setup_workspace"

    repos = list(tasks_by_repo.keys())
    repo_count = len(repos)

    if not parallel_enabled or repo_count == 1:
        # Fall back to sequential for single repo or disabled parallel
        logger.info(f"Using sequential execution for {ticket_key}")
        return "setup_workspace"

    # Limit concurrent repos
    batch_size = min(repo_count, MAX_CONCURRENT_REPOS)
    logger.info(f"Spawning parallel execution for {batch_size}/{repo_count} repos on {ticket_key}")

    sends = []
    for i, repo in enumerate(repos[:batch_size]):
        # Create isolated state for each parallel branch
        branch_state = {
            **state,
            "current_repo": repo,
            "parallel_branch_id": i,
            "parallel_total_branches": batch_size,
            "repos_to_process": repos,
            "repos_completed": [],
            "implemented_tasks": [],
        }
        sends.append(Send("setup_workspace", branch_state))

    return sends


def aggregate_parallel_results(states: list[WorkflowState]) -> WorkflowState:
    """Aggregate results from parallel execution branches.

    This function implements fan-in pattern to collect results from
    concurrent repository processing.

    Args:
        states: List of states from parallel branches.

    Returns:
        Aggregated workflow state.
    """
    if not states:
        return {}

    base_state = states[0]
    request = project_repository_aggregation(states)
    outcome = invoke_builtin_station_sync(request)
    update = reduce_repository_aggregation(base_state, request, outcome)

    logger.info(
        "Aggregated %s PRs from %s repos for %s",
        len(update["pr_urls"]),
        len(update["repos_completed"]),
        base_state["ticket_key"],
    )
    return update_state_timestamp({**base_state, **update})


def should_use_parallel_execution(state: WorkflowState) -> bool:
    """Determine if parallel execution should be used.

    Args:
        state: Current workflow state.

    Returns:
        True if parallel execution is appropriate.
    """
    tasks_by_repo = state.get("tasks_by_repo", {})
    parallel_enabled = state.get("parallel_execution_enabled", True)

    if not parallel_enabled:
        return False

    repo_count = len(tasks_by_repo)
    return repo_count > 1


class ParallelExecutionTracker:
    """Track parallel execution state across branches."""

    def __init__(self, total_branches: int):
        """Initialize tracker.

        Args:
            total_branches: Total number of parallel branches.
        """
        self.total_branches = total_branches
        self.completed_branches: list[int] = []
        self.branch_results: dict[int, WorkflowState] = {}

    def mark_complete(
        self,
        branch_id: int,
        state: WorkflowState,
    ) -> None:
        """Mark a branch as complete.

        Args:
            branch_id: Branch identifier.
            state: Final state from the branch.
        """
        self.completed_branches.append(branch_id)
        self.branch_results[branch_id] = state

    def is_all_complete(self) -> bool:
        """Check if all branches are complete.

        Returns:
            True if all branches finished.
        """
        return len(self.completed_branches) >= self.total_branches

    def get_aggregated_state(self) -> WorkflowState:
        """Get aggregated state from all branches.

        Returns:
            Aggregated workflow state.
        """
        states = list(self.branch_results.values())
        return aggregate_parallel_results(states)
