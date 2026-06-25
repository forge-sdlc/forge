"""Q&A handler node for answering questions about generated artifacts."""

import contextlib
import logging
from datetime import UTC, datetime

from forge.integrations.agents import ForgeAgent
from forge.integrations.github.client import GitHubClient
from forge.integrations.jira.client import JiraClient
from forge.workflow.feature.state import FeatureState as WorkflowState
from forge.workflow.utils import update_state_timestamp

logger = logging.getLogger(__name__)


def _artifact_pr_target(state: WorkflowState, artifact_type: str) -> tuple[str, int] | None:
    """Return the proposals PR target for an artifact, if review is on GitHub."""
    if artifact_type == "prd" and state.get("prd_pr_number") and state.get("prd_pr_repo"):
        return state["prd_pr_repo"], state["prd_pr_number"]
    if artifact_type == "spec" and state.get("spec_pr_number") and state.get("spec_pr_repo"):
        return state["spec_pr_repo"], state["spec_pr_number"]
    return None


async def _post_qa_response(
    jira: JiraClient,
    ticket_key: str,
    state: WorkflowState,
    artifact_type: str,
    body: str,
) -> None:
    pr_target = _artifact_pr_target(state, artifact_type)
    if pr_target:
        repo_full, pr_number = pr_target
        owner, repo_name = repo_full.split("/", 1)
        gh = GitHubClient()
        try:
            await gh.create_issue_comment(owner, repo_name, pr_number, body)
        finally:
            await gh.close()
    else:
        await jira.add_comment(ticket_key, body)


def extract_question_text(comment: str) -> str:
    """Extract the actual question from a comment with Q&A prefix.

    Removes ? or @forge ask prefix.

    Args:
        comment: Raw comment text with Q&A prefix.

    Returns:
        The question text without the prefix.
    """
    text = comment.strip()
    if text.startswith("?"):
        return text[1:].strip()
    lower = text.lower()
    if lower.startswith("@forge ask"):
        return text[10:].strip()
    return text


async def answer_question(state: WorkflowState) -> WorkflowState:
    """Answer a question about a generated artifact without advancing workflow.

    This node:
    1. Extracts the question from feedback_comment
    2. Loads generation context and current artifact
    3. Uses ForgeAgent to generate an answer
    4. Posts answer as Jira comment
    5. Records Q&A in state history
    6. Returns to the same gate (stays paused)

    Args:
        state: Current workflow state with is_question=True and feedback_comment set.

    Returns:
        Updated state with qa_history appended and feedback cleared.
    """
    ticket_key = state["ticket_key"]
    current_node = state.get("current_node", "")
    question_raw = state.get("feedback_comment", "")

    if not question_raw:
        logger.warning(f"No question found for {ticket_key}")
        return state

    question = extract_question_text(question_raw)
    logger.info(f"Answering question for {ticket_key}: {question[:100]}...")

    jira = JiraClient()
    agent = ForgeAgent()

    try:
        # Determine artifact type from current node
        artifact_type = _determine_artifact_type(current_node)
        artifact_content = _get_artifact_content(state, artifact_type)
        generation_context = state.get("generation_context", {}).get(artifact_type, {})

        # Generate answer using agent
        answer = await agent.answer_question(
            question=question,
            artifact_content=artifact_content,
            context={
                "ticket_key": ticket_key,
                "ticket_type": state.get("ticket_type", ""),
                "current_node": state.get("current_node", ""),
                "event_type": state.get("event_type", ""),
                "event_source": state.get("context", {}).get("source", ""),
                "retry_count": state.get("retry_count", 0),
                "artifact_type": artifact_type,
                "generation_context": generation_context,
            },
        )

        # Post answer to the right channel
        formatted_answer = f"*Q: {question}*\n\n{answer}"
        await _post_qa_response(jira, ticket_key, state, artifact_type, formatted_answer)

        # Record in Q&A history
        qa_history = list(state.get("qa_history", []))
        qa_history.append(
            {
                "question": question,
                "answer": answer,
                "artifact_type": artifact_type,
                "timestamp": datetime.now(UTC).isoformat(),
            }
        )

        logger.info(f"Answered question for {ticket_key}")

        # Stay at current gate, remain paused
        return update_state_timestamp(
            {
                **state,
                "qa_history": qa_history,
                "feedback_comment": None,
                "is_question": False,
                "revision_requested": False,
                "is_paused": True,
                "current_node": current_node,
            }
        )

    except Exception as e:
        logger.error(f"Failed to answer question for {ticket_key}: {e}")
        with contextlib.suppress(Exception):
            error_msg = (
                f"I wasn't able to answer that question. Error: {e}\n\n"
                "Please try rephrasing or ask a different question."
            )
            await _post_qa_response(jira, ticket_key, state, artifact_type, error_msg)

        return update_state_timestamp(
            {
                **state,
                "feedback_comment": None,
                "is_question": False,
                "revision_requested": False,
                "is_paused": True,
                "current_node": current_node,
            }
        )
    finally:
        await jira.close()
        await agent.close()


def _determine_artifact_type(current_node: str) -> str:
    """Determine artifact type from current node name.

    Args:
        current_node: Name of the current workflow node (e.g., 'prd_approval_gate').

    Returns:
        Artifact type string: 'prd', 'spec', 'rca', 'plan', or 'unknown'.
    """
    node_lower = current_node.lower()
    if "prd" in node_lower:
        return "prd"
    elif "spec" in node_lower:
        return "spec"
    elif "triage" in node_lower:
        return "triage"
    elif "rca" in node_lower:
        return "rca"
    elif "plan" in node_lower:
        return "plan"
    return "unknown"


def _get_artifact_content(state: WorkflowState, artifact_type: str) -> str:
    """Get artifact content from state.

    Args:
        state: Current workflow state.
        artifact_type: Type of artifact to retrieve ('prd', 'spec', 'rca').

    Returns:
        The artifact content string, or empty string if not found.
    """
    # Triage: assemble ticket context from summary, description, comments
    if artifact_type == "triage":
        summary = state.get("summary", "")
        description = state.get("description", "")
        comments = state.get("comments", [])
        parts = [
            p
            for p in [
                f"Summary: {summary}" if summary else "",
                f"Description: {description}" if description else "",
            ]
            if p
        ]
        if comments:
            parts.append("Comments:\n" + "\n---\n".join(str(c) for c in comments))
        return "\n\n".join(parts)

    mapping = {
        "prd": "prd_content",
        "spec": "spec_content",
        "rca": "rca_content",
    }
    field = mapping.get(artifact_type)
    if field:
        return state.get(field, "")

    # Plan: check plan_content first (bug workflow), fall back to generation_context (feature workflow)
    if artifact_type == "plan":
        plan_content = state.get("plan_content")
        if plan_content is not None:
            return plan_content
        return state.get("generation_context", {}).get("plan", "")

    return ""
