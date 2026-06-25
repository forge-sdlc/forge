"""Specification generation node for LangGraph workflow."""

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
from forge.workflow.nodes.prd_generation import (
    _normalize_proposals_path,
    _resolve_prd_proposals_repo,
    _resolve_proposals_path,
)
from forge.workflow.utils import update_state_timestamp
from forge.workflow.utils.jira_status import post_status_comment
from forge.workflow.utils.qa_summary import post_qa_summary_if_needed

logger = logging.getLogger(__name__)


async def _create_spec_proposal_pr(
    ticket_key: str,
    spec_content: str,
    summary: str,
    proposals_repo: str,
    proposals_path: str = "",
) -> dict[str, Any]:
    """Create a PR with the spec in the enhancement proposals repo."""
    owner, repo = proposals_repo.split("/", 1)
    branch = f"forge/spec/{ticket_key.lower()}"
    proposals_path = _normalize_proposals_path(proposals_path)
    file_path = "/".join(filter(None, [proposals_path, ticket_key, "design.md"]))

    gh = GitHubClient()
    jira = JiraClient()
    try:
        await gh.create_branch(owner, repo, branch)
        await gh.create_or_update_file(
            owner=owner,
            repo=repo,
            path=file_path,
            content=spec_content,
            message=f"Add spec for {ticket_key}",
            branch=branch,
        )
        pr_body = (
            f"**Spec for [{ticket_key}](https://redhat.atlassian.net/browse/{ticket_key})**\n\n"
            f"The specification is in [`{file_path}`](/{file_path}) on this branch.\n\n"
            "Review the file changes for the latest version. "
            "Leave comments on this PR to provide feedback — "
            "Forge will regenerate the spec and push updated commits."
        )
        pr_data = await gh.create_pull_request(
            owner=owner,
            repo=repo,
            title=f"[{ticket_key}] Spec: {summary}",
            body=pr_body,
            head=branch,
        )

        pr_url = pr_data["html_url"]
        pr_number = pr_data["number"]

        await set_pr_ticket_index(pr_url, ticket_key)
        await jira.set_workflow_label(ticket_key, ForgeLabel.SPEC_PENDING)
        await jira.add_comment(
            ticket_key,
            f"Specification published for review: [GitHub PR]({pr_url})",
        )

        return {
            "spec_pr_url": pr_url,
            "spec_pr_number": pr_number,
            "spec_pr_repo": proposals_repo,
            "spec_pr_branch": branch,
            "spec_pr_file_path": file_path,
        }
    finally:
        await gh.close()
        await jira.close()


async def _update_spec_proposal_pr(
    ticket_key: str,
    spec_content: str,
    state: dict[str, Any],
) -> None:
    """Push updated spec content to the existing proposal PR branch."""
    owner, repo = state["spec_pr_repo"].split("/", 1)
    branch = state["spec_pr_branch"]
    pr_number = state["spec_pr_number"]
    file_path = state["spec_pr_file_path"]

    gh = GitHubClient()
    try:
        file_meta = await gh.get_file_contents(owner, repo, file_path, branch)
        if not file_meta:
            logger.warning(f"Could not find spec file {file_path} on branch {branch}")
            return

        await gh.create_or_update_file(
            owner=owner,
            repo=repo,
            path=file_path,
            content=spec_content,
            message=f"Revise spec for {ticket_key} based on feedback",
            branch=branch,
            sha=file_meta["sha"],
        )
        await gh.create_issue_comment(
            owner,
            repo,
            pr_number,
            "Specification has been revised based on feedback. Please review the updated version.",
        )
    finally:
        await gh.close()


async def generate_spec(state: WorkflowState) -> WorkflowState:
    """Generate a behavioral specification from the approved PRD.

    This node:
    1. Reads the PRD content from state (or fetches from Jira)
    2. Generates a specification with Given/When/Then acceptance criteria
    3. Stores spec in Jira custom field
    4. Transitions ticket to "Pending Spec Approval"

    Args:
        state: Current workflow state with prd_content.

    Returns:
        Updated state with spec_content populated.
    """
    ticket_key = state["ticket_key"]
    prd_content = state.get("prd_content", "")

    logger.info(f"Generating specification for {ticket_key}")

    # Post Q&A summary for PRD if any
    qa_history = state.get("qa_history", [])
    if qa_history:
        await post_qa_summary_if_needed(ticket_key, qa_history, "prd")

    jira = JiraClient()
    agent = ForgeAgent()
    spec_content = None
    jira_error = None

    try:
        await post_status_comment(
            jira,
            ticket_key,
            "📋 Forge is generating your specification — this may take a few minutes.",
        )

        # Fetch issue metadata (needed for project_key and summary)
        issue = await jira.get_issue(ticket_key)

        # If PRD not in state, fetch from Jira
        if not prd_content:
            prd_content = issue.description or ""

        if not prd_content.strip():
            logger.warning(f"No PRD content found for {ticket_key}")
            return {
                **state,
                "last_error": "No PRD content available for spec generation",
                "current_node": "generate_spec",
            }

        # Build context
        context: dict[str, Any] = {
            "ticket_key": ticket_key,
            "ticket_type": state.get("ticket_type", ""),
            "current_node": state.get("current_node", ""),
            "event_type": state.get("event_type", ""),
            "event_source": state.get("context", {}).get("source", ""),
            "retry_count": state.get("retry_count", 0),
        }

        # Generate specification using Claude - primary operation
        spec_content = await agent.generate_spec(prd_content, context)

        # Publish spec — either as GitHub PR or Jira update
        proposals_repo = await _resolve_prd_proposals_repo(issue.project_key, jira)
        spec_pr_result = None
        try:
            if proposals_repo:
                proposals_path = await _resolve_proposals_path(issue.project_key, jira)
                spec_pr_result = await _create_spec_proposal_pr(
                    ticket_key=ticket_key,
                    spec_content=spec_content,
                    summary=issue.summary,
                    proposals_repo=proposals_repo,
                    proposals_path=proposals_path,
                )
            else:
                settings = get_settings()
                if settings.jira_store_in_comments:
                    await jira.add_structured_comment(
                        ticket_key,
                        "Technical Specification",
                        spec_content,
                        comment_type="spec",
                    )
                elif settings.jira_spec_custom_field:
                    await jira.update_custom_field(
                        ticket_key,
                        settings.jira_spec_custom_field,
                        spec_content,
                    )
                else:
                    await jira.add_attachment(
                        ticket_key,
                        filename=f"{ticket_key}-spec.md",
                        content=spec_content,
                        content_type="text/markdown",
                    )
                await jira.set_workflow_label(ticket_key, ForgeLabel.SPEC_PENDING)
        except Exception as e:
            jira_error = str(e)
            logger.warning(f"Spec publish failed for {ticket_key}: {e}")

        logger.info(f"Spec generated for {ticket_key} ({len(spec_content)} chars)")

        # Store generation context for Q&A mode
        generation_context = state.get("generation_context", {})
        generation_context["spec"] = {
            "prd_content": prd_content,
            "generated_at": datetime.now(UTC).isoformat(),
        }

        result = update_state_timestamp(
            {
                **state,
                "spec_content": spec_content,
                "generation_context": generation_context,
                "current_node": "spec_approval_gate",
                "last_error": f"Spec publish pending: {jira_error}" if jira_error else None,
            }
        )
        if spec_pr_result:
            result.update(spec_pr_result)
        return result

    except Exception as e:
        logger.error(f"Spec generation failed for {ticket_key}: {e}")
        from forge.workflow.nodes.error_handler import notify_error

        await notify_error(state, str(e), "generate_spec")
        # If we have partial content, save it even on failure
        result_state = {
            **state,
            "last_error": str(e),
            "current_node": "generate_spec",
            "retry_count": state.get("retry_count", 0) + 1,
        }
        if spec_content:
            result_state["spec_content"] = spec_content
        return result_state
    finally:
        await jira.close()
        await agent.close()


async def regenerate_spec_with_feedback(state: WorkflowState) -> WorkflowState:
    """Regenerate specification incorporating user feedback.

    Args:
        state: Current workflow state with feedback_comment set.

    Returns:
        Updated state with new spec_content.
    """
    ticket_key = state["ticket_key"]
    feedback = state.get("feedback_comment", "")
    original_spec = state.get("spec_content", "")

    if not feedback:
        logger.warning(f"No feedback provided for spec regeneration on {ticket_key}")
        return state

    logger.info(f"Regenerating spec for {ticket_key} with feedback")

    jira = JiraClient()
    agent = ForgeAgent()

    try:
        # Regenerate spec with feedback
        new_spec = await agent.regenerate_with_feedback(
            original_content=original_spec,
            feedback=feedback,
            content_type="spec",
            ticket_key=ticket_key,
            context={
                "ticket_type": state.get("ticket_type", ""),
                "current_node": state.get("current_node", ""),
                "event_type": state.get("event_type", ""),
                "event_source": state.get("context", {}).get("source", ""),
                "retry_count": state.get("retry_count", 0),
            },
        )

        # Publish revised spec
        if state.get("spec_pr_number"):
            await _update_spec_proposal_pr(ticket_key, new_spec, state)
        else:
            settings = get_settings()
            if settings.jira_store_in_comments:
                await jira.add_structured_comment(
                    ticket_key,
                    "Technical Specification (Revised)",
                    new_spec,
                    comment_type="spec",
                )
            elif settings.jira_spec_custom_field:
                await jira.update_custom_field(
                    ticket_key,
                    settings.jira_spec_custom_field,
                    new_spec,
                )
            else:
                old_filename = f"{ticket_key}-spec.md"
                deleted = await jira.delete_attachments_by_name(ticket_key, old_filename)
                if deleted:
                    logger.info(f"Deleted {deleted} old spec attachment(s) for {ticket_key}")
                await jira.add_attachment(
                    ticket_key,
                    filename=old_filename,
                    content=new_spec,
                    content_type="text/markdown",
                )
            await jira.add_comment(
                ticket_key,
                "Specification has been revised based on feedback. Please review.",
            )

        logger.info(f"Spec regenerated for {ticket_key} ({len(new_spec)} chars)")

        return update_state_timestamp(
            {
                **state,
                "spec_content": new_spec,
                "feedback_comment": None,
                "revision_requested": False,
                "current_node": "spec_approval_gate",
                "last_error": None,
            }
        )

    except Exception as e:
        logger.error(f"Spec regeneration failed for {ticket_key}: {e}")
        from forge.workflow.nodes.error_handler import notify_error

        await notify_error(state, str(e), "regenerate_spec")
        return {
            **state,
            "last_error": str(e),
            "current_node": "regenerate_spec",
            "retry_count": state.get("retry_count", 0) + 1,
        }
    finally:
        await jira.close()
        await agent.close()
