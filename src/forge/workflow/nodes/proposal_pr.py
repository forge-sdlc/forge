"""Shared publication helpers for PRD and specification proposal PRs."""

import logging
from dataclasses import dataclass, replace
from typing import Any

from forge.integrations.jira.client import JiraClient, pr_interaction_options
from forge.integrations.source_control.errors import NotFoundError
from forge.models.workflow import ForgeLabel
from forge.orchestrator.checkpointer import set_pr_ticket_index
from forge.workflow.utils.jira_status import post_status_comment
from forge.workflow.utils.source_control import get_adapter, identity_for

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ProposalArtifact:
    """Artifact-specific text and state fields for the shared PR flow."""

    state_prefix: str
    branch_segment: str
    file_name: str
    title_name: str
    document_name: str
    pending_label: ForgeLabel
    published_name: str


PRD_PROPOSAL = ProposalArtifact(
    state_prefix="prd",
    branch_segment="prd",
    file_name="prd.md",
    title_name="PRD",
    document_name="PRD document",
    pending_label=ForgeLabel.PRD_PENDING,
    published_name="PRD",
)

SPEC_PROPOSAL = ProposalArtifact(
    state_prefix="spec",
    branch_segment="spec",
    file_name="design.md",
    title_name="Spec",
    document_name="specification",
    pending_label=ForgeLabel.SPEC_PENDING,
    published_name="Specification",
)


async def create_proposal_pr(
    *,
    artifact: ProposalArtifact,
    ticket_key: str,
    content: str,
    summary: str,
    proposals_repo: str,
    proposals_path: str,
) -> dict[str, Any]:
    """Publish an artifact branch to a fork and open its upstream PR."""
    branch = f"forge/{artifact.branch_segment}/{ticket_key.lower()}"
    file_path = "/".join(filter(None, [proposals_path, ticket_key, artifact.file_name]))

    repo_ref, adapter = get_adapter(proposals_repo)
    jira = JiraClient()
    try:
        default_branch = await adapter.resolve_default_branch(repo_ref)
        target = await adapter.ensure_write_target(repo_ref)
        fork_owner = target.fork_owner or ""
        fork_repo = target.fork_repo or ""
        fork_ref = (
            replace(
                repo_ref,
                id=f"{fork_owner}/{fork_repo}",
                namespace=f"{fork_owner}/{fork_repo}",
                change_request_mode="direct",
            )
            if fork_owner and fork_repo
            else repo_ref
        )

        await adapter.create_branch(fork_ref, branch, default_branch)
        await adapter.put_file(
            fork_ref,
            file_path,
            content,
            f"Add {artifact.title_name} for {ticket_key}",
            branch,
        )
        pr_body = (
            f"**{artifact.title_name} for [{ticket_key}]"
            f"(https://redhat.atlassian.net/browse/{ticket_key})**\n\n"
            f"The {artifact.document_name} is in [`{file_path}`](/{file_path}) "
            "on this branch.\n\n"
            "Review the file changes for the latest version. "
            "Leave comments on this PR to provide feedback — "
            f"Forge will regenerate the {artifact.title_name} and push updated commits."
        )
        change_request = await adapter.create_change_request(
            repo_ref,
            replace(target, head_ref=branch, base_branch=default_branch),
            title=f"[{ticket_key}] {artifact.title_name}: {summary}",
            body=pr_body,
        )

        pr_url = change_request.url
        native_id = change_request.identity.native_id
        pr_number = int(native_id) if native_id is not None else None
        await set_pr_ticket_index(pr_url, ticket_key)
        await jira.set_workflow_label(ticket_key, artifact.pending_label)
        await post_status_comment(
            jira,
            ticket_key,
            f"{artifact.published_name} published for review: "
            f"[GitHub PR]({pr_url})\n\n{pr_interaction_options(pr_url)}",
        )

        prefix = artifact.state_prefix
        return {
            f"{prefix}_pr_url": pr_url,
            f"{prefix}_pr_number": pr_number,
            # Canonical namespace, not the raw (possibly repos.yaml-alias)
            # proposals_repo -- webhook matching (worker._is_prd_pr_event /
            # _is_spec_pr_event) compares this against event.repo_ref.namespace,
            # which is always canonical.
            f"{prefix}_pr_repo": repo_ref.namespace,
            f"{prefix}_pr_fork_owner": fork_owner,
            f"{prefix}_pr_fork_repo": fork_repo,
            f"{prefix}_pr_branch": branch,
            f"{prefix}_pr_file_path": file_path,
        }
    finally:
        await jira.close()


async def update_proposal_pr(
    *,
    artifact: ProposalArtifact,
    ticket_key: str,
    content: str,
    state: dict[str, Any],
) -> bool:
    """Update a fork proposal branch, with fallback for legacy upstream state.

    Returns:
        True if the file was updated, False if the content was unchanged.
    """
    prefix = artifact.state_prefix
    upstream_repo = state[f"{prefix}_pr_repo"]
    fork_owner = state.get(f"{prefix}_pr_fork_owner")
    fork_repo = state.get(f"{prefix}_pr_fork_repo")
    branch = state[f"{prefix}_pr_branch"]
    pr_number = state[f"{prefix}_pr_number"]
    file_path = state[f"{prefix}_pr_file_path"]

    repo_ref, adapter = get_adapter(upstream_repo)
    write_ref = (
        replace(
            repo_ref,
            id=f"{fork_owner}/{fork_repo}",
            namespace=f"{fork_owner}/{fork_repo}",
            change_request_mode="direct",
        )
        if fork_owner and fork_repo
        else repo_ref
    )
    identity = identity_for(repo_ref, pr_number)

    try:
        existing_content = await adapter.get_file(write_ref, file_path, branch)
    except NotFoundError:
        logger.warning(
            "Could not find %s file %s on branch %s",
            artifact.title_name,
            file_path,
            branch,
        )
        return False

    if existing_content == content:
        logger.warning(
            "Regenerated %s for %s is unchanged — skipping commit",
            artifact.title_name,
            ticket_key,
        )
        await adapter.create_comment(
            repo_ref,
            identity,
            f"Forge reviewed the feedback but the regenerated "
            f"{artifact.published_name} was unchanged. The feedback may "
            f"require manual revision, or it may have already been "
            f"addressed in a previous revision.",
        )
        return False

    await adapter.put_file(
        write_ref,
        file_path,
        content,
        f"Revise {artifact.title_name} for {ticket_key} based on feedback",
        branch,
    )
    await adapter.create_comment(
        repo_ref,
        identity,
        f"{artifact.published_name} has been revised based on feedback. "
        "Please review the updated version.",
    )
    return True
