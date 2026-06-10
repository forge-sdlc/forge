"""Post-merge summary node: posts fix summary and release note to Jira after merge."""

import logging

from forge.config import get_settings
from forge.integrations.jira.client import JiraClient
from forge.workflow.bug.state import BugState

logger = logging.getLogger(__name__)


async def post_merge_summary(state: BugState) -> BugState:
    """Post a fix summary and release note to the Jira bug ticket after merge.

    Non-blocking: any exception is caught and logged. The workflow always
    proceeds regardless of outcome.

    Args:
        state: Current bug workflow state with rca_content and plan_content.

    Returns:
        State unchanged (no node transition — caller routes to END).
    """
    ticket_key = state["ticket_key"]
    rca_content = state.get("rca_content") or ""
    plan_content = state.get("plan_content") or ""
    fix_approach = state.get("selected_fix_approach") or {}
    current_repo = state.get("current_repo", "")
    pr_urls = state.get("pr_urls", [])

    settings = get_settings()
    jira = JiraClient(settings)

    try:
        comment = _build_summary_comment(
            rca_content=rca_content,
            plan_content=plan_content,
            fix_approach=fix_approach,
            current_repo=current_repo,
            pr_urls=pr_urls,
        )
        await jira.add_comment(ticket_key, comment)
        logger.info(f"Posted post-merge summary to {ticket_key}")
    except Exception as e:
        logger.error(f"post_merge_summary failed for {ticket_key} (non-blocking): {e}")
    finally:
        await jira.close()

    return state


def _extract_impact(rca_content: str) -> str:
    """Extract an impact summary from the first non-empty sentence of the RCA."""
    for line in rca_content.split("\n"):
        stripped = line.strip()
        if stripped and not stripped.startswith("#"):
            return stripped[:200]
    return "Users who experienced this bug."


def _build_summary_comment(
    rca_content: str,
    plan_content: str,  # noqa: ARG001
    fix_approach: dict,
    current_repo: str,
    pr_urls: list[str],
) -> str:
    """Build the post-merge Jira comment with fix summary and release note."""
    parts = ["## Fix Merged"]

    if fix_approach.get("title"):
        parts.append(
            f"**Approach applied:** {fix_approach['title']} — {fix_approach.get('description', '')}"
        )

    if rca_content:
        summary_line = rca_content.split("\n")[0][:200]
        parts.append(f"**Root cause:** {summary_line}")

    if pr_urls:
        pr_links = ", ".join(pr_urls)
        parts.append(f"**Pull request(s):** {pr_links}")

    impact = _extract_impact(rca_content)
    parts.extend(
        [
            "",
            "## Release Note",
            "",
            f"**Component:** {current_repo}",
            f"**Fix:** {fix_approach.get('description', '')}",
            f"**Root cause:** {rca_content[:200]}",
            f"**Impact:** {impact}",
        ]
    )

    return "\n".join(parts)
