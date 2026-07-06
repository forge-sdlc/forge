"""PR creation node for Task Takeover workflow."""

import asyncio
import contextlib
import logging
from pathlib import Path
from typing import cast

from forge.integrations.github.client import GitHubClient
from forge.integrations.jira.client import JiraClient
from forge.workflow.nodes.pr_creation import _build_pr_body, _generate_pr_body_with_agent
from forge.workflow.nodes.workspace_setup import teardown_workspace
from forge.workflow.task_takeover.state import TaskTakeoverState as WorkflowState
from forge.workflow.utils import update_state_timestamp
from forge.workspace.git_ops import GitOperations
from forge.workspace.manager import Workspace

logger = logging.getLogger(__name__)


async def cleanup_podman_containers(ticket_key: str) -> None:
    """Stop and remove any running or stopped podman containers related to the ticket.

    Args:
        ticket_key: Jira ticket key to match container names.
    """
    try:
        # Find containers with name matching forge-{ticket_key}-*
        proc = await asyncio.create_subprocess_exec(
            "podman",
            "ps",
            "-a",
            "--filter",
            f"name=forge-{ticket_key}-",
            "--format",
            "{{.Names}}",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await proc.communicate()
        container_names = [name.strip() for name in stdout.decode().splitlines() if name.strip()]

        for name in container_names:
            logger.info(f"Stopping container: {name}")
            stop_proc = await asyncio.create_subprocess_exec(
                "podman",
                "stop",
                "-t",
                "5",
                name,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await stop_proc.wait()

            logger.info(f"Removing container: {name}")
            rm_proc = await asyncio.create_subprocess_exec(
                "podman",
                "rm",
                "-f",
                name,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await rm_proc.wait()
    except Exception as e:
        logger.warning(f"Error during podman container teardown for {ticket_key}: {e}")


async def create_task_takeover_pr(state: WorkflowState) -> WorkflowState:
    """Create a pull request from workspace changes for the task takeover workflow.

    This node:
    1. Synchronizes local changes with repository fork.
    2. Pushes local changes using GitOperations.
    3. Opens a Pull Request using GitHubClient.
    4. Posts the PR markdown link as a comment on Jira.
    5. Transitions Jira ticket status to "In Review".
    6. Teardown the workspace and container runner, freeing all resources.

    Args:
        state: Current task takeover workflow state.

    Returns:
        Updated state with PR details, workspace cleared.
    """
    ticket_key = state["ticket_key"]
    workspace_path = state.get("workspace_path")
    current_repo = state.get("current_repo", "")
    branch_name = state.get("context", {}).get("branch_name") or f"forge/{ticket_key.lower()}"

    if not workspace_path:
        logger.error(f"No workspace for PR creation on {ticket_key}")
        return cast(
            WorkflowState,
            update_state_timestamp(
                {
                    **state,
                    "last_error": "Workspace not set up",
                    "current_node": "create_task_takeover_pr",
                }
            ),
        )

    github = GitHubClient()
    jira = JiraClient()

    try:
        # Step 1: Set up GitOperations
        workspace = Workspace(
            path=Path(workspace_path),
            repo_name=current_repo,
            branch_name=branch_name,
            ticket_key=ticket_key,
        )
        git = GitOperations(workspace)

        # Step 2: Push changes to fork
        if not current_repo or "/" not in current_repo:
            raise ValueError(
                f"Invalid repository format '{current_repo}': must be in owner/repo format"
            )

        owner, repo = current_repo.split("/")
        logger.info(f"Getting or creating fork for {current_repo}")
        fork_data = await github.get_or_create_fork(owner, repo)
        fork_owner = fork_data["owner"]["login"]
        fork_repo = fork_data["name"]

        # Sync fork with upstream main branch
        await github.sync_fork_with_upstream(fork_owner, fork_repo)

        # Add fork remote and push
        git.add_fork_remote(fork_owner, fork_repo)
        git.push_to_fork()

        # Step 3: Fetch Jira issue details to construct the PR title
        ticket_summary = ""
        try:
            ticket_issue = await jira.get_issue(ticket_key)
            ticket_summary = ticket_issue.summary or ""
        except Exception as e:
            logger.warning(f"Could not fetch ticket details for PR: {e}")

        pr_title = f"[{ticket_key}] {ticket_summary or 'Task Takeover Implementation'}"
        implemented_tasks = state.get("implemented_tasks") or [
            state.get("current_task_key") or ticket_key
        ]
        pr_body = await _generate_pr_body_with_agent(
            cast(WorkflowState, state),
            git,
            jira,
            implemented_tasks,
        )
        if not pr_body:
            pr_body = _build_pr_body(cast(WorkflowState, state), implemented_tasks)

        # Step 4: Open a Pull Request from fork to upstream
        pr_data = await github.create_pull_request(
            owner=owner,
            repo=repo,
            title=pr_title,
            body=pr_body,
            head=f"{fork_owner}:{branch_name}",
            base="main",
        )
        pr_url = pr_data.get("html_url", "")
        pr_number = pr_data.get("number")

        # Step 5: Post the PR markdown link as a comment on Jira
        pr_label = f"PR #{pr_number}" if pr_number is not None else "Pull Request"
        pr_markdown_link = f"[{pr_label}]({pr_url})"
        comment_text = (
            f"🚀 Task takeover implementation complete. Pull Request created:\n\n{pr_markdown_link}"
        )
        await jira.add_comment(ticket_key, comment_text)

        # Create remote link in Jira as well for better integration
        with contextlib.suppress(Exception):
            await jira.create_remote_link(ticket_key, pr_url, pr_label)

        # Step 6: Transition the Jira ticket status to "In Review"
        await jira.transition_issue(ticket_key, "In Review")

        # Update PR URL lists
        pr_urls = state.get("pr_urls", [])
        if pr_url and pr_url not in pr_urls:
            pr_urls.append(pr_url)

        # Update state with PR information before teardown
        state_with_pr = {
            **state,
            "pr_urls": pr_urls,
            "current_pr_url": pr_url,
            "current_pr_number": pr_number,
            "fork_owner": fork_owner,
            "fork_repo": fork_repo,
        }

        # Step 7: Teardown workspace and container runner resources
        # Clean up any lingering container runners
        await cleanup_podman_containers(ticket_key)

        # Clean up files and delete workspace
        teardown_state = await teardown_workspace(cast(WorkflowState, state_with_pr))

        return cast(
            WorkflowState,
            update_state_timestamp(
                {
                    **teardown_state,
                    "current_node": "complete",
                    "last_error": None,
                }
            ),
        )

    except Exception as e:
        logger.error(f"Task takeover PR creation node failed for {ticket_key}: {e}")
        return cast(
            WorkflowState,
            update_state_timestamp(
                {
                    **state,
                    "last_error": str(e),
                    "current_node": "create_task_takeover_pr",
                    "retry_count": state.get("retry_count", 0) + 1,
                }
            ),
        )
    finally:
        await github.close()
        await jira.close()
