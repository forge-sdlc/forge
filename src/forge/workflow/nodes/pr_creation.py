"""PR creation node for opening pull requests."""

import contextlib
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from forge.config import get_settings
from forge.integrations.agents import ForgeAgent
from forge.integrations.github.client import GitHubClient
from forge.integrations.jira.client import JiraClient
from forge.models.workflow import ForgeLabel, TicketType
from forge.orchestrator.checkpointer import set_pr_ticket_index
from forge.prompts import load_prompt
from forge.workflow.nodes.code_review import sync_pr_description
from forge.workflow.nodes.post_merge_summary import _extract_impact
from forge.workflow.pr_state import save_active_pull_request
from forge.workflow.utils import update_state_timestamp
from forge.workflow.utils.jira_status import post_status_comment
from forge.workspace.git_ops import GitOperations
from forge.workspace.manager import Workspace

WorkflowState = dict[str, Any]

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class PullRequestTarget:
    """Resolved upstream and fork repository for PR creation."""

    owner: str
    repo: str
    fork_owner: str
    fork_repo: str


async def prepare_pull_request_target(
    github: GitHubClient,
    git: GitOperations,
    current_repo: str,
) -> PullRequestTarget:
    """Prepare a fork remote for opening a pull request from the current workspace."""
    if not current_repo or "/" not in current_repo:
        raise ValueError(
            f"Invalid repository format '{current_repo}': must be in owner/repo format"
        )

    owner, repo = current_repo.split("/", 1)

    logger.info(f"Getting or creating fork for {current_repo}")
    fork_data = await github.get_or_create_fork(owner, repo)
    fork_owner = fork_data["owner"]["login"]
    fork_repo = fork_data["name"]

    await github.sync_fork_with_upstream(fork_owner, fork_repo)
    git.add_fork_remote(fork_owner, fork_repo)

    return PullRequestTarget(
        owner=owner,
        repo=repo,
        fork_owner=fork_owner,
        fork_repo=fork_repo,
    )


async def open_pull_request_from_fork(
    github: GitHubClient,
    target: PullRequestTarget,
    *,
    branch_name: str,
    title: str,
    body: str,
    base: str = "main",
    draft: bool = False,
) -> dict:
    """Open a pull request from the prepared fork branch to upstream."""
    return await github.create_pull_request(
        owner=target.owner,
        repo=target.repo,
        title=title,
        body=body,
        head=f"{target.fork_owner}:{branch_name}",
        base=base,
        draft=draft,
    )


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

    if not implemented_tasks:
        implemented_tasks = [state.get("current_task_key") or ticket_key]

    if not workspace_path:
        logger.error(f"No workspace for PR creation on {ticket_key}")
        return {
            **state,
            "last_error": "Workspace not available",
            "current_node": "create_pr",
        }

    logger.info(f"Creating PR for {ticket_key} ({len(implemented_tasks)} tasks)")

    github = GitHubClient()
    jira = JiraClient()

    try:
        # Set up workspace reference
        context = state.get("context", {})
        branch_name = context.get("branch_name", "")
        default_branch = context.get("default_branch", "main")
        workspace = Workspace(
            path=Path(workspace_path),
            repo_name=current_repo,
            branch_name=branch_name,
            ticket_key=ticket_key,
        )
        git = GitOperations(workspace)

        pr_target = await prepare_pull_request_target(github, git, current_repo)

        # Check for merge conflicts before pushing
        has_conflicts, conflicting_files = await check_merge_conflicts(git, default_branch)

        if has_conflicts:
            logger.warning(f"Merge conflicts detected for {ticket_key}: {conflicting_files}")

            # Transition to blocked status
            await jira.set_workflow_label(ticket_key, ForgeLabel.BLOCKED)
            await post_status_comment(
                jira,
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

        project_key = ticket_key.split("-")[0] if "-" in ticket_key else ticket_key
        is_draft = await jira.is_repo_draft(project_key, current_repo)

        pr_data = await open_pull_request_from_fork(
            github,
            pr_target,
            branch_name=branch_name,
            title=pr_title,
            body=pr_body,
            base=default_branch,
            draft=is_draft,
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
        await post_status_comment(
            jira,
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

        # Transition Jira ticket to "In Review"
        with contextlib.suppress(Exception):
            await jira.transition_issue(ticket_key, "In Review")

        # Sync description to catch any inaccuracies from local_review commits
        await sync_pr_description(
            state,
            git,
            owner=pr_target.owner,
            repo=pr_target.repo,
            pr_number=pr_number,
            attempt=0,
        )

        # Append auto-review exhaustion section AFTER sync_pr_description
        # (sync rewrites the body with an AI agent that would drop this section)
        if pr_number is not None:
            exhaustion_section = _format_review_exhaustion_section(
                state.get("review_exhaustion_report", {})
            )
            if exhaustion_section:
                try:
                    pr_data = await github.get_pull_request(
                        pr_target.owner, pr_target.repo, pr_number
                    )
                    current_body = pr_data.get("body", "") or ""
                    if "## Auto-Review Notes" in current_body:
                        logger.debug("PR body already contains Auto-Review Notes — skipping append")
                    else:
                        await github.update_pull_request(
                            pr_target.owner,
                            pr_target.repo,
                            pr_number,
                            body=current_body + "\n\n" + exhaustion_section,
                        )
                        logger.info("Appended auto-review exhaustion section to PR body")
                except Exception as e:
                    logger.warning(f"Failed to append review exhaustion section: {e}")

        return update_state_timestamp(
            save_active_pull_request(
                {
                    **state,
                    "pr_urls": pr_urls,
                    "current_pr_url": pr_url,
                    "current_pr_number": pr_number,
                    "fork_owner": pr_target.fork_owner,
                    "fork_repo": pr_target.fork_repo,
                    "current_node": "teardown_workspace",
                    "last_error": None,
                }
            )
        )

    except Exception as e:
        logger.error(f"PR creation failed for {ticket_key}: {e}")
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


def _format_review_exhaustion_section(report: dict[str, dict]) -> str:
    """Format review exhaustion data as a markdown section for the PR body."""
    if not report:
        return ""

    lines = [
        "## Auto-Review Notes",
        "",
        "The following review criteria could not be resolved after all retry attempts.",
        "Human reviewers should pay particular attention to these areas.",
        "",
    ]

    for entry in report.values():
        step = entry.get("step_name", "unknown")
        task = entry.get("task_key", "unknown")
        skill = entry.get("skill", "unknown")
        max_retries = entry.get("max_retries", "?")
        feedback = entry.get("final_feedback", "")

        lines.append(f"### {step} — {task}")
        lines.append(f"**Skill:** {skill} | **Retries:** {max_retries}/{max_retries} exhausted")
        lines.append("")
        if feedback:
            for feedback_line in feedback.split("\n"):
                lines.append(f"> {feedback_line}")
            lines.append("")

    return "\n".join(lines)


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
        default_branch = state.get("context", {}).get("default_branch", "main")
        commit_log = git._run_git(
            "log",
            f"origin/{default_branch}..HEAD",
            "--pretty=format:%h %s%n%b",
            "--no-merges",
            check=False,
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
            policy_key="generate_pr_description",
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
        # Move to next repo — reset per-repo state
        return update_state_timestamp(
            {
                **state,
                "repos_completed": repos_completed,
                "current_repo": remaining[0],
                "implemented_tasks": [],
                "current_task_key": None,
                "fork_owner": None,
                "fork_repo": None,
                "current_pr_url": None,
                "current_pr_number": None,
                "review_verdict": None,
                "review_feedback": None,
                "qualitative_review_retry_count": 0,
                "qualitative_review_failed": False,
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
