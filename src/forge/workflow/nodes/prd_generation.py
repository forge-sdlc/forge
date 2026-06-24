"""PRD generation node for LangGraph workflow."""

import logging
from datetime import UTC, datetime
from typing import Any

from forge.config import get_settings
from forge.integrations.agents import ForgeAgent
from forge.integrations.github.client import GitHubClient
from forge.integrations.jira.client import JiraClient
from forge.models.workflow import ForgeLabel
from forge.orchestrator.checkpointer import set_pr_ticket_index
from forge.workflow.feature.state import FeatureState as WorkflowState
from forge.workflow.utils import update_state_timestamp
from forge.workflow.utils.jira_status import post_status_comment

logger = logging.getLogger(__name__)


def _normalize_proposals_path(path: str) -> str:
    """Normalize a proposals base path for GitHub content paths."""
    return path.strip("/")


async def _resolve_prd_proposals_repo(project_key: str, jira: JiraClient) -> str | None:
    """Resolve the PRD proposals repo for a project.

    Checks the Jira project property first (forge.prd_proposals_repo),
    then falls back to the global env var when project config is not required.

    Returns:
        Repo string in "owner/repo" format, or None if not configured.
    """
    proposals_repo = await jira.get_prd_proposals_repo(project_key)
    if proposals_repo:
        return proposals_repo

    settings = get_settings()
    if not settings.forge_require_project_config and settings.prd_proposals_repo:
        logger.info(
            f"Project {project_key}: using global fallback PRD proposals repo: "
            f"{settings.prd_proposals_repo}"
        )
        return settings.prd_proposals_repo

    return None


async def _resolve_proposals_path(project_key: str, jira: JiraClient) -> str:
    """Resolve the base directory for enhancement folders in the proposals repo.

    Checks the Jira project property first (forge.prd_proposals_path),
    then falls back to the global config.

    Returns:
        Path string (empty string means repo root).
    """
    proposals_path = await jira.get_proposals_path(project_key)
    if proposals_path is not None:
        return proposals_path

    settings = get_settings()
    return _normalize_proposals_path(settings.prd_proposals_path)


async def _create_prd_proposal_pr(
    ticket_key: str,
    prd_content: str,
    summary: str,
    proposals_repo: str,
    proposals_path: str = "",
) -> dict[str, Any]:
    """Create a PR with the PRD in the enhancement proposals repo."""
    owner, repo = proposals_repo.split("/", 1)
    branch = f"forge/prd/{ticket_key.lower()}"
    proposals_path = _normalize_proposals_path(proposals_path)
    file_path = "/".join(filter(None, [proposals_path, ticket_key, "prd.md"]))

    gh = GitHubClient()
    jira = JiraClient()
    try:
        await gh.create_branch(owner, repo, branch)
        await gh.create_or_update_file(
            owner=owner,
            repo=repo,
            path=file_path,
            content=prd_content,
            message=f"Add PRD for {ticket_key}",
            branch=branch,
        )
        pr_body = (
            f"**PRD for [{ticket_key}](https://redhat.atlassian.net/browse/{ticket_key})**\n\n"
            f"The PRD document is in [`{file_path}`](/{file_path}) on this branch.\n\n"
            "Review the file changes for the latest version. "
            "Leave comments on this PR to provide feedback — "
            "Forge will regenerate the PRD and push updated commits."
        )
        pr_data = await gh.create_pull_request(
            owner=owner,
            repo=repo,
            title=f"[{ticket_key}] PRD: {summary}",
            body=pr_body,
            head=branch,
        )

        pr_url = pr_data["html_url"]
        pr_number = pr_data["number"]

        await set_pr_ticket_index(pr_url, ticket_key)
        await jira.set_workflow_label(ticket_key, ForgeLabel.PRD_PENDING)
        await jira.add_comment(
            ticket_key,
            f"PRD published for review: [GitHub PR]({pr_url})",
        )

        return {
            "prd_pr_url": pr_url,
            "prd_pr_number": pr_number,
            "prd_pr_repo": proposals_repo,
            "prd_pr_branch": branch,
            "prd_pr_file_path": file_path,
        }
    finally:
        await gh.close()
        await jira.close()


async def _update_prd_proposal_pr(
    ticket_key: str,
    prd_content: str,
    state: dict[str, Any],
) -> None:
    """Push updated PRD content to the existing proposal PR branch."""
    owner, repo = state["prd_pr_repo"].split("/", 1)
    branch = state["prd_pr_branch"]
    pr_number = state["prd_pr_number"]
    file_path = state["prd_pr_file_path"]

    gh = GitHubClient()
    try:
        file_meta = await gh.get_file_contents(owner, repo, file_path, branch)
        if not file_meta:
            logger.warning(f"Could not find PRD file {file_path} on branch {branch}")
            return

        await gh.create_or_update_file(
            owner=owner,
            repo=repo,
            path=file_path,
            content=prd_content,
            message=f"Revise PRD for {ticket_key} based on feedback",
            branch=branch,
            sha=file_meta["sha"],
        )
        await gh.create_issue_comment(
            owner,
            repo,
            pr_number,
            "PRD has been revised based on feedback. Please review the updated version.",
        )
    finally:
        await gh.close()


async def generate_prd(state: WorkflowState) -> WorkflowState:
    """Generate a PRD from raw requirements in Jira description.

    This node:
    1. Reads the current Jira issue description
    2. Generates a structured PRD using Claude
    3. Updates the Jira description with the PRD
    4. Transitions the ticket to "Pending PRD Approval"

    Args:
        state: Current workflow state.

    Returns:
        Updated state with prd_content populated.
    """
    ticket_key = state["ticket_key"]
    logger.info(f"Generating PRD for {ticket_key}")

    jira = JiraClient()
    agent = ForgeAgent()
    prd_content = None
    jira_error = None

    try:
        await post_status_comment(
            jira,
            ticket_key,
            "📝 Forge is generating your PRD — this may take a few minutes.",
        )

        # Fetch current issue to get raw requirements
        issue = await jira.get_issue(ticket_key)
        raw_requirements = issue.description or ""

        if not raw_requirements.strip():
            logger.warning(f"No description found for {ticket_key}")
            return {
                **state,
                "last_error": "No requirements found in issue description",
                "current_node": "generate_prd",
            }

        # Build context from issue metadata
        context: dict[str, Any] = {
            "ticket_key": ticket_key,
            "ticket_type": state.get("ticket_type", ""),
            "current_node": state.get("current_node", ""),
            "event_type": state.get("event_type", ""),
            "event_source": state.get("context", {}).get("source", ""),
            "retry_count": state.get("retry_count", 0),
            "summary": issue.summary,
            "project_key": issue.project_key,
        }

        # Generate PRD using Claude - primary operation
        prd_content = await agent.generate_prd(raw_requirements, context)

        # Publish PRD - either as GitHub PR or Jira update
        # Per-project opt-in: check forge.prd_proposals_repo project property
        proposals_repo = await _resolve_prd_proposals_repo(issue.project_key, jira)
        settings = get_settings()
        prd_pr_result = None
        try:
            if proposals_repo:
                proposals_path = await _resolve_proposals_path(issue.project_key, jira)
                prd_pr_result = await _create_prd_proposal_pr(
                    ticket_key=ticket_key,
                    prd_content=prd_content,
                    summary=issue.summary,
                    proposals_repo=proposals_repo,
                    proposals_path=proposals_path,
                )
            else:
                if settings.jira_store_in_comments:
                    await jira.add_structured_comment(
                        ticket_key,
                        "Product Requirements Document (PRD)",
                        prd_content,
                        comment_type="prd",
                    )
                else:
                    await jira.update_description(ticket_key, prd_content)
                await jira.set_workflow_label(ticket_key, ForgeLabel.PRD_PENDING)
        except Exception as e:
            jira_error = str(e)
            logger.warning(f"PRD publish failed for {ticket_key}: {e}")

        logger.info(f"PRD generated for {ticket_key} ({len(prd_content)} chars)")

        # Store generation context for Q&A mode
        generation_context = state.get("generation_context", {})
        generation_context["prd"] = {
            "raw_requirements": raw_requirements,
            "summary": issue.summary,
            "generated_at": datetime.now(UTC).isoformat(),
        }

        # If publish failed, set a warning but still advance (content exists)
        result = update_state_timestamp(
            {
                **state,
                "prd_content": prd_content,
                "generation_context": generation_context,
                "current_node": "prd_approval_gate",
                "last_error": f"PRD publish pending: {jira_error}" if jira_error else None,
            }
        )
        if prd_pr_result:
            result.update(prd_pr_result)
        return result

    except Exception as e:
        logger.error(f"PRD generation failed for {ticket_key}: {e}")
        from forge.workflow.nodes.error_handler import notify_error

        await notify_error(state, str(e), "generate_prd")
        # If we have partial content, save it even on failure
        result_state = {
            **state,
            "last_error": str(e),
            "current_node": "generate_prd",
            "retry_count": state.get("retry_count", 0) + 1,
        }
        if prd_content:
            result_state["prd_content"] = prd_content
        return result_state
    finally:
        await jira.close()
        await agent.close()


async def regenerate_prd_with_feedback(state: WorkflowState) -> WorkflowState:
    """Regenerate PRD incorporating user feedback.

    This node handles the case where a PM rejects the PRD and provides
    feedback via a Jira comment. It regenerates the PRD addressing
    the feedback and updates Jira.

    Args:
        state: Current workflow state with feedback_comment set.

    Returns:
        Updated state with new prd_content.
    """
    ticket_key = state["ticket_key"]
    feedback = state.get("feedback_comment", "")
    original_prd = state.get("prd_content", "")

    if not feedback:
        logger.warning(f"No feedback provided for PRD regeneration on {ticket_key}")
        return state

    logger.info(f"Regenerating PRD for {ticket_key} with feedback")

    jira = JiraClient()
    agent = ForgeAgent()

    try:
        # Regenerate PRD with feedback
        new_prd = await agent.regenerate_with_feedback(
            original_content=original_prd,
            feedback=feedback,
            content_type="prd",
            ticket_key=ticket_key,
            context={
                "ticket_type": state.get("ticket_type", ""),
                "current_node": state.get("current_node", ""),
                "event_type": state.get("event_type", ""),
                "event_source": state.get("context", {}).get("source", ""),
                "retry_count": state.get("retry_count", 0),
            },
        )

        # Publish revised PRD
        if state.get("prd_pr_number"):
            await _update_prd_proposal_pr(ticket_key, new_prd, state)
        else:
            settings = get_settings()
            if settings.jira_store_in_comments:
                await jira.add_structured_comment(
                    ticket_key,
                    "Product Requirements Document (PRD)",
                    new_prd,
                    comment_type="prd",
                )
            else:
                await jira.update_description(ticket_key, new_prd)
            await jira.add_comment(
                ticket_key,
                "📝 PRD has been revised based on feedback. Please review.",
            )

        logger.info(f"PRD regenerated for {ticket_key} ({len(new_prd)} chars)")

        return update_state_timestamp(
            {
                **state,
                "prd_content": new_prd,
                "feedback_comment": None,
                "revision_requested": False,
                "current_node": "prd_approval_gate",
                "last_error": None,
            }
        )

    except Exception as e:
        logger.error(f"PRD regeneration failed for {ticket_key}: {e}")
        from forge.workflow.nodes.error_handler import notify_error

        await notify_error(state, str(e), "regenerate_prd")
        return {
            **state,
            "last_error": str(e),
            "current_node": "regenerate_prd",
            "retry_count": state.get("retry_count", 0) + 1,
        }
    finally:
        await jira.close()
        await agent.close()
