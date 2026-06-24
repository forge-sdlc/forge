"""PR creation node for opening pull requests."""

import logging
from pathlib import Path

from forge.config import get_settings
from forge.integrations.agents import ForgeAgent
from forge.integrations.github.client import GitHubClient
from forge.integrations.jira.client import JiraClient
from forge.models.workflow import ForgeLabel, TicketType
from forge.orchestrator.checkpointer import set_pr_ticket_index
from forge.prompts import load_prompt
from forge.workflow.feature.state import FeatureState as WorkflowState
from forge.workflow.nodes.code_review import sync_pr_description
from forge.workflow.nodes.post_merge_summary import _extract_impact
from forge.workflow.utils import update_state_timestamp
from forge.workspace.git_ops import GitOperations
from forge.workspace.manager import Workspace

logger = logging.getLogger(__name__)


async def check_merge_conflicts(
    git: GitOperations,
    target_branch: str = "main",
) -> tuple[bool, list[str]]:
    """Check if the branch would have merge conflicts with target.

    Simulates a merge to detect conflicts before PR creation.

    Args:
        git: GitOperations instance.
        target_branch: Target branch to merge into.

    Returns:
        Tuple of (has_conflicts, conflicting_files).
    """
    try:
        # Fetch latest target branch
        git._run_git("fetch", "origin", target_branch, check=False)

        # Try merge in dry-run mode
        result = git._run_git(
            "merge-tree",
            f"origin/{target_branch}",
            "HEAD",
            check=False,
        )

        # merge-tree outputs conflict markers if there would be conflicts
        output = result.stdout or ""

        if "CONFLICT" in output or "<<<<<<< " in output:
            # Parse conflicting files from output
            conflicting_files: list[str] = []
            for line in output.split("\n"):
                # Extract filename from "CONFLICT (content): Merge conflict in file.py"
                if line.startswith("CONFLICT") and " in " in line:
                    filename = line.split(" in ")[-1].strip()
                    conflicting_files.append(filename)

            return True, conflicting_files

        return False, []

    except Exception as e:
        logger.warning(f"Could not check merge conflicts: {e}")
        # On error, proceed without blocking
        return False, []


async def create_pull_request(state: WorkflowState) -> WorkflowState:
    """Create a pull request from the workspace changes using fork-based workflow.

    This node:
    1. Gets or creates a fork of the upstream repository
    2. Syncs fork with upstream
    3. Pushes the feature branch to the fork
    4. Creates a PR from fork to upstream
    5. Links PR to Jira tickets
    6. Stores PR URL in state

    Args:
        state: Current workflow state.

    Returns:
        Updated state with PR URL.
    """
    ticket_key = state["ticket_key"]
    workspace_path = state.get("workspace_path")
    current_repo = state.get("current_repo", "")
    implemented_tasks = state.get("implemented_tasks", [])

    if not workspace_path:
        logger.error(f"No workspace for PR creation on {ticket_key}")
        return {
            **state,
            "last_error": "Workspace not available",
            "current_node": "create_pr",
        }

    if not implemented_tasks:
        logger.warning(f"No tasks implemented for {ticket_key}")
        return update_state_timestamp(
            {
                **state,
                "current_node": "teardown_workspace",
                "last_error": None,
            }
        )

    logger.info(f"Creating PR for {ticket_key} ({len(implemented_tasks)} tasks)")

    github = GitHubClient()
    jira = JiraClient()

    try:
        # Set up workspace reference
        branch_name = state.get("context", {}).get("branch_name", "")
        workspace = Workspace(
            path=Path(workspace_path),
            repo_name=current_repo,
            branch_name=branch_name,
            ticket_key=ticket_key,
        )
        git = GitOperations(workspace)

        # Parse owner/repo for upstream
        owner, repo = current_repo.split("/")

        # Get or create fork
        logger.info(f"Getting or creating fork for {current_repo}")
        fork_data = await github.get_or_create_fork(owner, repo)
        fork_owner = fork_data["owner"]["login"]
        fork_repo = fork_data["name"]

        # Sync fork with upstream
        await github.sync_fork_with_upstream(fork_owner, fork_repo)

        # Add fork as remote
        git.add_fork_remote(fork_owner, fork_repo)

        # Check for merge conflicts before pushing
        has_conflicts, conflicting_files = await check_merge_conflicts(git, "main")

        if has_conflicts:
            logger.warning(f"Merge conflicts detected for {ticket_key}: {conflicting_files}")

            # Transition to blocked status
            await jira.set_workflow_label(ticket_key, ForgeLabel.BLOCKED)
            await jira.add_comment(
                ticket_key,
                "**Merge Conflicts Detected**\n\n"
                "Cannot create PR due to merge conflicts with main branch.\n\n"
                "**Conflicting files:**\n"
                + "\n".join(f"- `{f}`" for f in conflicting_files)
                + "\n\n*Human intervention required to resolve conflicts.*",
            )

            return update_state_timestamp(
                {
                    **state,
                    "current_node": "blocked",
                    "last_error": f"Merge conflicts: {conflicting_files}",
                    "merge_conflicts": conflicting_files,
                }
            )

        # Push branch to fork (not origin)
        git.push_to_fork()

        # Build PR title — fetch live summary from Jira as source of truth
        ticket_summary = ""
        try:
            ticket_issue = await jira.get_issue(ticket_key)
            ticket_summary = ticket_issue.summary or ""
        except Exception as e:
            logger.warning(f"Could not fetch ticket summary for PR title: {e}")
        pr_title = f"[{ticket_key}] {_get_pr_title(state, ticket_summary)}"

        # Generate PR body with agent, fall back to template if it fails
        pr_body = await _generate_pr_body_with_agent(state, git, jira, implemented_tasks)
        if not pr_body:
            pr_body = _build_pr_body(state, implemented_tasks)

        # Create PR from fork to upstream
        # Head format: "fork_owner:branch_name"
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

        # Log PR number extraction status
        if pr_number is not None:
            logger.debug(f"Successfully extracted PR number {pr_number} from GitHub API response")
        else:
            logger.warning(
                f"PR number not available in GitHub API response for {ticket_key}. "
                f"PR URL: {pr_url or 'unknown'}"
            )

        # Store PR URL
        pr_urls = state.get("pr_urls", [])
        pr_urls.append(pr_url)

        # Add comment to Jira with PR link
        await jira.add_comment(
            ticket_key,
            f"Pull request created: {pr_url}\n\nImplements {len(implemented_tasks)} tasks.",
        )

        # Add remote link so the poller can discover the PR
        # Use pr_number if available, otherwise use generic label
        pr_label = f"PR #{pr_number}" if pr_number is not None else "Pull Request"
        await jira.create_remote_link(ticket_key, pr_url, pr_label)

        if pr_number is not None:
            logger.info(f"Created PR #{pr_number}: {pr_url}")
        else:
            logger.info(f"Created PR (number unavailable): {pr_url}")

        # Index PR URL → ticket key so the worker can resolve ticket association
        # from GitHub events without relying on PR/branch name parsing.
        try:
            await set_pr_ticket_index(pr_url, ticket_key)
        except Exception:
            logger.warning(
                f"Failed to index PR {pr_url} → {ticket_key}; "
                "GitHub event routing will fall back to name-based extraction",
                exc_info=True,
            )

        # Sync description to catch any inaccuracies from local_review commits
        await sync_pr_description(
            state,
            git,
            owner=owner,
            repo=repo,
            pr_number=pr_number,
            attempt=0,
        )

        return update_state_timestamp(
            {
                **state,
                "pr_urls": pr_urls,
                "current_pr_url": pr_url,
                "current_pr_number": pr_number,
                "fork_owner": fork_owner,
                "fork_repo": fork_repo,
                "current_node": "teardown_workspace",
                "last_error": None,
            }
        )

    except Exception as e:
        logger.error(f"PR creation failed for {ticket_key}: {e}")
        from forge.workflow.nodes.error_handler import notify_error

        await notify_error(state, str(e), "create_pr")
        return {
            **state,
            "last_error": str(e),
            "current_node": "create_pr",
            "retry_count": state.get("retry_count", 0) + 1,
        }
    finally:
        await github.close()
        await jira.close()


def _get_pr_title(state: WorkflowState, ticket_summary: str = "") -> str:
    """Generate PR title from Jira summary, falling back to state context."""
    if ticket_summary:
        return ticket_summary
    context = state.get("context", {})
    return (
        context.get("feature_summary")
        or context.get("summary")
        or f"Implementation for {state.get('ticket_key', 'Unknown')}"
    )


def _build_pr_body(
    state: WorkflowState,
    implemented_tasks: list[str],
) -> str:
    """Build PR body with task list and context.

    Args:
        state: Current workflow state.
        implemented_tasks: List of implemented task keys.

    Returns:
        Formatted PR body.
    """
    ticket_key = state["ticket_key"]
    context = state.get("context", {})
    settings = get_settings()
    jira_base_url = settings.jira_base_url.rstrip("/")

    # Build Jira link if we have the base URL
    ticket_link = (
        f"[{ticket_key}]({jira_base_url}/browse/{ticket_key})" if jira_base_url else ticket_key
    )

    body_parts = [
        "## Summary",
        "",
        f"This PR implements tasks for {ticket_link}.",
        "",
    ]

    # Add feature summary if available
    feature_summary = context.get("feature_summary", "")
    if feature_summary:
        body_parts.extend(
            [
                "### Overview",
                "",
                feature_summary,
                "",
            ]
        )

    body_parts.extend(
        [
            "## Tasks Implemented",
            "",
        ]
    )

    task_fmt = (
        (lambda k: f"- [x] [{k}]({jira_base_url}/browse/{k})")
        if jira_base_url
        else (lambda k: f"- [x] {k}")
    )
    body_parts.extend(task_fmt(k) for k in implemented_tasks)

    body_parts.extend(
        [
            "",
            "## Testing",
            "",
            "- [ ] CI checks pass",
            "- [ ] Code review approved",
        ]
    )

    # Append release note for bug tickets
    if state.get("ticket_type") == TicketType.BUG:
        rca_content = state.get("rca_content") or ""
        fix_approach = state.get("selected_fix_approach") or {}
        current_repo = state.get("current_repo", "")
        impact = _extract_impact(rca_content)
        body_parts.extend(
            [
                "",
                "## Release Note",
                "",
                f"**Component:** {current_repo}",
                f"**Fix:** {fix_approach.get('description', rca_content[:120])}",
                f"**Root cause:** {rca_content[:200]}",
                f"**Impact:** {impact}",
            ]
        )

    body_parts.extend(
        [
            "",
            "---",
            "*Generated by [Forge](https://github.com/forge-sdlc/forge) SDLC Orchestrator*",
        ]
    )

    body = "\n".join(body_parts)

    # Prepend qualitative review warning if review failed
    if state.get("qualitative_review_failed"):
        verdict = state.get("local_review_verdict", "unknown")
        feedback = state.get("qualitative_feedback", "")
        warning = (
            "> **Warning:** Automated qualitative review did not pass within the retry limit.\n"
            f"> Verdict: {verdict}\n"
            f"> Feedback: {feedback}\n"
            "> Manual review of test coverage and root-cause alignment is recommended.\n\n"
        )
        return warning + body

    return body


async def _generate_pr_body_with_agent(
    state: WorkflowState,
    git: GitOperations,
    jira: JiraClient,
    implemented_tasks: list[str],
) -> str | None:
    """Generate PR body using AI agent for better quality descriptions.

    Reads commit history, task descriptions, and handoff summary to generate
    a coherent, detailed PR description.

    Args:
        state: Current workflow state.
        git: GitOperations instance for reading commit log.
        jira: JiraClient for fetching task descriptions.
        implemented_tasks: List of implemented task keys.

    Returns:
        Generated PR body, or None if generation fails.
    """
    ticket_key = state["ticket_key"]
    current_repo = state.get("current_repo", "")
    workspace_path = state.get("workspace_path")
    settings = get_settings()

    try:
        # Get commit log from the branch
        commit_log = git._run_git(
            "log", "origin/main..HEAD", "--pretty=format:%h %s%n%b", "--no-merges", check=False
        ).stdout.strip()

        if not commit_log:
            commit_log = "(No commits on this branch)"

        # Fetch task descriptions from Jira
        task_descriptions = []
        for task_key in implemented_tasks:
            try:
                issue = await jira.get_issue(task_key)
                summary = issue.summary or ""
                description = issue.description or ""
                task_descriptions.append(f"### {task_key}: {summary}\n{description}")
            except Exception as e:
                logger.warning(f"Could not fetch task {task_key}: {e}")
                task_descriptions.append(f"### {task_key}\n(Description unavailable)")

        # Read handoff summary if available
        handoff_summary = "(No handoff summary available)"
        if workspace_path:
            handoff_path = Path(workspace_path) / ".forge" / "handoff.md"
            if handoff_path.exists():
                try:
                    handoff_summary = handoff_path.read_text()[:3000]  # Limit size
                except Exception as e:
                    logger.warning(f"Could not read handoff.md: {e}")

        # Build prompt using template
        prompt = load_prompt(
            "generate-pr-body",
            ticket_key=ticket_key,
            repo_name=current_repo,
            task_descriptions="\n\n".join(task_descriptions),
            commit_log=commit_log,
            handoff_summary=handoff_summary,
            jira_base_url=settings.jira_base_url.rstrip("/"),
        )

        # Run agent to generate PR body
        agent = ForgeAgent(settings)
        result = await agent.run_task(
            task="generate-pr-body",
            prompt=prompt,
            context={
                "ticket_key": ticket_key,
                "task_count": len(implemented_tasks),
            },
            trace_context={
                "ticket_key": ticket_key,
                "ticket_type": state.get("ticket_type", ""),
                "current_node": state.get("current_node", ""),
                "repo": current_repo,
                "pr_number": state.get("current_pr_number", ""),
                "ci_status": state.get("ci_status", ""),
                "event_type": state.get("event_type", ""),
                "event_source": state.get("context", {}).get("source", ""),
                "retry_count": state.get("retry_count", 0),
            },
            include_tools=False,  # No tools needed for text generation
        )

        if result and len(result) > 100:
            result = agent._strip_preamble(result)
            logger.info(f"Generated PR body with agent ({len(result)} chars)")
            return result
        else:
            logger.warning("Agent returned empty or short PR body, falling back to template")
            return None

    except Exception as e:
        logger.warning(f"Agent PR body generation failed, falling back to template: {e}")
        return None


async def teardown_and_route(state: WorkflowState) -> WorkflowState:
    """Teardown workspace and route to next repo or completion.

    Args:
        state: Current workflow state.

    Returns:
        Updated state.
    """
    from forge.workflow.nodes.workspace_setup import teardown_workspace

    # Teardown current workspace
    state = await teardown_workspace(state)

    # Mark current repo as completed
    repos_completed = state.get("repos_completed", [])
    current_repo = state.get("current_repo")

    if current_repo and current_repo not in repos_completed:
        repos_completed.append(current_repo)

    # Check for remaining repos
    repos_to_process = state.get("repos_to_process", [])
    remaining = [r for r in repos_to_process if r not in repos_completed]

    if remaining:
        # Move to next repo
        return update_state_timestamp(
            {
                **state,
                "repos_completed": repos_completed,
                "current_repo": remaining[0],
                "implemented_tasks": [],
                "current_node": "setup_workspace",
            }
        )

    # All repos done — pause until GitHub delivers CI webhook
    return update_state_timestamp(
        {
            **state,
            "repos_completed": repos_completed,
            "current_node": "wait_for_ci_gate",
        }
    )
